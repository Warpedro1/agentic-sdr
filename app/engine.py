"""O engine: tudo o que a maquina de estados nao pode fazer.

Divisao de trabalho:
- `machine.handle` DECIDE (funcao pura, sem I/O);
- o engine EXECUTA — banco, disco, e o registro da conversa;
- o adaptador de canal so converte mundo <-> mensagem.

Duas responsabilidades do engine que sustentam o desenho:

1. **Reentrada.** Quando a maquina cai num estado interno (RESOLVENDO_IDENTIDADE,
   CARREGANDO_HORARIOS, RESERVANDO), ela emitiu um efeito e parou. O engine
   executa o efeito, grava o resultado no ctx e chama `handle` de novo com
   entrada vazia. E isso que deixa a maquina pura mesmo precisando de banco no
   meio do fluxo.

2. **Transcricao.** Toda mensagem, de entrada E de saida, vai para `mensagens`
   com o template usado. Nao e opcional: e o dataset contra o qual uma versao
   futura com IA vai ser comparada.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.availability import gerar_candidatos, montar_ofertas
from app.booking import reservar
from app.calendar import PostgresCalendarProvider
from app.channels.base import ChannelAdapter, InboundMessage, MensagemSaida
from app.config import Configuracao, obter_configuracao
from app.crm import CrmSink, CrmSinkNoop
from app.datas import data_extenso, hora
from app.enums import ESTADOS_INTERNOS, ESTADOS_TERMINAIS, Direcao, Estado
from app.logging import obter_logger
from app.machine import (
    EncerrarConversa,
    EscalarParaHumano,
    GerarSlots,
    Politicas,
    RegistrarOptOut,
    ReservarSlot,
    ResolverIdentidade,
    RevogarOptOut,
    SalvarDocumento,
    Transicao,
    handle,
)
from app.models import Consultor, Conversa, Documento, Lead, Mensagem, OptOut
from app.routing import carga_futura
from app.storage.local import ArmazenamentoDocumento
from app.templates import CatalogoMensagens, obter_catalogo

logger = obter_logger("engine")

#: Chaves que vivem so na memoria da execucao. Nao vao para o JSONB porque
#: carregam objetos ricos (datetime, dataclass) e porque sao resultado de I/O
#: recem-feito — persistir daria a impressao de que continuam validas depois.
CHAVES_TRANSITORIAS = frozenset(
    {"agora", "politicas", "reconhecimento", "reserva", "canal"}
)

#: Numero maximo de reentradas seguidas. Um fluxo saudavel usa 1 ou 2; o limite
#: existe so para que um bug futuro vire erro visivel em vez de laco infinito.
MAX_REENTRADAS = 8


@dataclass
class Execucao:
    conversa_id: uuid.UUID
    estado: Estado
    lead_id: uuid.UUID | None = None
    lead: dict = field(default_factory=dict)
    ctx: dict = field(default_factory=dict)
    #: Contador da transcricao. Ver o comentario em models.Mensagem.sequencia.
    proxima_sequencia: int = 0

    def sequencia(self) -> int:
        self.proxima_sequencia += 1
        return self.proxima_sequencia


class Engine:
    def __init__(
        self,
        criar_sessao: async_sessionmaker[AsyncSession],
        adaptador: ChannelAdapter,
        catalogo: CatalogoMensagens | None = None,
        config: Configuracao | None = None,
        armazenamento: ArmazenamentoDocumento | None = None,
        crm: CrmSink | None = None,
        relogio: Callable[[], datetime] | None = None,
        entregador: Any | None = None,
    ) -> None:
        self._criar_sessao = criar_sessao
        self._adaptador = adaptador
        self._catalogo = catalogo or obter_catalogo()
        self._config = config or obter_configuracao()
        self._armazenamento = armazenamento
        self._crm = crm or CrmSinkNoop()
        # Injetavel para o teste e2e poder congelar o tempo: a transcricao cita
        # datas de reuniao, e sem relogio fixo o snapshot mudaria todo dia.
        self._relogio = relogio or (lambda: datetime.now(UTC))
        # Opcional. Quando existe, a fila do CRM e drenada em segundo plano
        # durante a conversa, para o lead aparecer no outro sistema logo — sem
        # que ninguem no atendimento espere por isso.
        self._entregador = entregador
        self._entregas: set[asyncio.Task] = set()

    def _agendar_entrega(self) -> None:
        """Dispara a drenagem sem bloquear a conversa.

        Falha aqui nunca sobe: se o CRM estiver fora do ar, os itens ficam na
        fila e a proxima tentativa resolve. O atendimento nao pode parar por
        causa de outro sistema.
        """
        if self._entregador is None:
            return

        async def tentar() -> None:
            try:
                await self._entregador.drenar()
            except Exception as erro:  # noqa: BLE001
                logger.warning("crm_drenagem_falhou", erro=str(erro))

        tarefa = asyncio.create_task(tentar())
        self._entregas.add(tarefa)
        tarefa.add_done_callback(self._entregas.discard)

    async def _aguardar_entregas(self) -> None:
        if self._entregas:
            await asyncio.gather(*list(self._entregas), return_exceptions=True)

    # --- ciclo de vida ------------------------------------------------------

    def _agora(self) -> datetime:
        return self._relogio()

    def _politicas(self) -> Politicas:
        return Politicas(
            max_tentativas=self._config.max_tentativas,
            janela_retomada_horas=self._config.janela_retomada_horas,
            horizonte_dias=self._config.horizonte_dias,
        )

    async def rodar(self) -> uuid.UUID:
        """Conduz a conversa ate um estado terminal. Devolve o id da conversa."""
        execucao = await self._abrir()

        # A saudacao nao espera entrada: o INICIO e disparado pelo engine.
        await self._processar(execucao, InboundMessage())

        while execucao.estado not in ESTADOS_TERMINAIS:
            entrada = await self._adaptador.receber()
            if entrada is None:
                await self._encerrar(execucao, motivo="canal encerrado")
                break
            await self._registrar_entrada(execucao, entrada)
            await self._processar(execucao, entrada)

        # Mesmo numa conversa abandonada no meio, o que ja foi enfileirado
        # precisa ter a chance de chegar ao CRM antes do processo terminar.
        await self._aguardar_entregas()
        return execucao.conversa_id

    async def _abrir(self) -> Execucao:
        async with self._criar_sessao() as sessao:
            conversa = Conversa(
                canal=self._adaptador.canal,
                estado=Estado.INICIO.value,
                tentativas=0,
                contexto={},
            )
            sessao.add(conversa)
            await sessao.commit()
            return Execucao(conversa_id=conversa.id, estado=Estado.INICIO)

    # --- passo --------------------------------------------------------------

    async def _processar(self, execucao: Execucao, entrada: InboundMessage) -> None:
        for _ in range(MAX_REENTRADAS):
            execucao.ctx["agora"] = self._agora()
            execucao.ctx["politicas"] = self._politicas()
            execucao.ctx.setdefault("canal", self._adaptador.canal)

            transicao = handle(
                execucao.estado, execucao.lead, execucao.ctx, entrada, self._catalogo
            )
            anterior = execucao.estado
            await self._aplicar(execucao, transicao, anterior)
            await self._executar_efeitos(execucao, transicao)

            if execucao.estado not in ESTADOS_INTERNOS:
                return
            # Estado interno: o efeito ja preencheu o ctx, seguimos sem esperar
            # o lead digitar nada.
            entrada = InboundMessage()

        raise RuntimeError(
            f"conversa {execucao.conversa_id} nao saiu dos estados internos "
            f"em {MAX_REENTRADAS} reentradas (estado={execucao.estado})"
        )

    async def _aplicar(
        self, execucao: Execucao, transicao: Transicao, anterior: Estado
    ) -> None:
        execucao.lead.update(transicao.lead_updates)
        execucao.ctx.update(transicao.ctx_updates)
        execucao.estado = transicao.novo_estado

        # `estado_anterior` e mantido pelo engine, e nao pela maquina: assim
        # "voltar" funciona em qualquer transicao, sem cada ramo ter que lembrar.
        if (
            anterior not in ESTADOS_INTERNOS
            and anterior is not transicao.novo_estado
            and transicao.novo_estado not in ESTADOS_INTERNOS
        ):
            execucao.ctx["estado_anterior"] = anterior.value

        async with self._criar_sessao() as sessao:
            if execucao.lead_id and transicao.lead_updates:
                lead = await sessao.get(Lead, execucao.lead_id)
                for campo, valor in transicao.lead_updates.items():
                    setattr(lead, campo, valor)

            conversa = await sessao.get(Conversa, execucao.conversa_id)
            conversa.estado = execucao.estado.value
            conversa.tentativas = int(execucao.ctx.get("tentativas", 0))
            conversa.contexto = self._ctx_persistivel(execucao.ctx)
            conversa.ultimo_contato_em = self._agora()
            if execucao.lead_id:
                conversa.lead_id = execucao.lead_id

            for mensagem in transicao.mensagens:
                sessao.add(
                    Mensagem(
                        conversa_id=execucao.conversa_id,
                        sequencia=execucao.sequencia(),
                        direcao=Direcao.SAIDA.value,
                        template_key=mensagem.template_key,
                        conteudo=mensagem.conteudo,
                    )
                )
            await sessao.commit()

        # `possui_contrato` e o ultimo campo da sequencia de coleta: quando ele
        # e preenchido, a qualificacao esta completa. E o marco certo para
        # mandar ao CRM o que a conversa apurou.
        if "possui_contrato" in transicao.lead_updates and execucao.lead_id:
            await self._crm.registrar_qualificacao(
                {**execucao.lead, "lead_id": str(execucao.lead_id)}
            )
            self._agendar_entrega()

        for mensagem in transicao.mensagens:
            await self._adaptador.enviar(mensagem)

    def _ctx_persistivel(self, ctx: dict) -> dict[str, Any]:
        def normalizar(valor: Any) -> Any:
            if isinstance(valor, datetime):
                return valor.isoformat()
            if isinstance(valor, dict):
                return {c: normalizar(v) for c, v in valor.items()}
            if isinstance(valor, list):
                return [normalizar(item) for item in valor]
            return valor

        return {
            chave: normalizar(valor)
            for chave, valor in ctx.items()
            if chave not in CHAVES_TRANSITORIAS
        }

    async def _registrar_entrada(
        self, execucao: Execucao, entrada: InboundMessage
    ) -> None:
        conteudo = entrada.texto
        if conteudo is None and entrada.anexo is not None:
            conteudo = f"[anexo] {entrada.anexo.nome_arquivo}"
        async with self._criar_sessao() as sessao:
            sessao.add(
                Mensagem(
                    conversa_id=execucao.conversa_id,
                    sequencia=execucao.sequencia(),
                    direcao=Direcao.ENTRADA.value,
                    template_key=None,
                    conteudo=conteudo or "",
                )
            )
            await sessao.commit()

    # --- efeitos ------------------------------------------------------------

    async def _executar_efeitos(self, execucao: Execucao, transicao: Transicao) -> None:
        for efeito in transicao.efeitos:
            if isinstance(efeito, ResolverIdentidade):
                await self._resolver_identidade(execucao, efeito)
            elif isinstance(efeito, GerarSlots):
                await self._gerar_slots(execucao)
            elif isinstance(efeito, ReservarSlot):
                await self._reservar(execucao)
            elif isinstance(efeito, SalvarDocumento):
                await self._salvar_documento(execucao, efeito)
            elif isinstance(efeito, RegistrarOptOut):
                await self._registrar_opt_out(execucao, efeito)
            elif isinstance(efeito, RevogarOptOut):
                await self._revogar_opt_out(execucao)
            elif isinstance(efeito, EscalarParaHumano):
                await self._escalar(execucao, efeito)
            elif isinstance(efeito, EncerrarConversa):
                await self._encerrar(execucao, motivo="fim de fluxo")

    async def _resolver_identidade(
        self, execucao: Execucao, efeito: ResolverIdentidade
    ) -> None:
        async with self._criar_sessao() as sessao:
            existente = (
                await sessao.execute(
                    select(Lead).where(Lead.telefone_e164 == efeito.telefone)
                )
            ).scalar_one_or_none()

            conhecido: dict | None = None
            ultima_em: datetime | None = None
            ultimo_estado: str | None = None

            if existente is None:
                # Lead novo: nasce agora, com o consentimento carimbado na hora em
                # que foi dado (na saudacao), nao neste instante.
                lead = Lead(
                    telefone_e164=efeito.telefone,
                    nome=efeito.nome,
                    email=efeito.email,
                    consentimento_em=efeito.consentimento_em,
                    canal=efeito.canal,
                )
                sessao.add(lead)
                await sessao.flush()
            else:
                lead = existente
                conhecido = self._lead_como_dict(lead)
                if efeito.nome and lead.nome and efeito.nome != lead.nome:
                    # O cadastro vence. Sobrescrever por um erro de digitacao no
                    # meio da conversa e pior do que ignorar a divergencia.
                    execucao.ctx["nome_informado_divergente"] = efeito.nome
                elif efeito.nome and not lead.nome:
                    lead.nome = efeito.nome

                anterior = (
                    await sessao.execute(
                        select(Conversa)
                        .where(
                            Conversa.lead_id == lead.id,
                            Conversa.id != execucao.conversa_id,
                        )
                        .order_by(desc(Conversa.ultimo_contato_em))
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if anterior is not None:
                    ultima_em = anterior.ultimo_contato_em
                    ultimo_estado = anterior.estado

            opt_out = (
                await sessao.execute(
                    select(OptOut)
                    .where(
                        OptOut.telefone_e164 == efeito.telefone,
                        OptOut.revogado_em.is_(None),
                    )
                    .order_by(desc(OptOut.criado_em))
                    .limit(1)
                )
            ).scalar_one_or_none()

            conversa = await sessao.get(Conversa, execucao.conversa_id)
            conversa.lead_id = lead.id

            execucao.lead_id = lead.id
            execucao.lead = self._lead_como_dict(lead)
            await sessao.commit()

        execucao.ctx["reconhecimento"] = {
            "lead_conhecido": conhecido,
            "opt_out_ativo": opt_out is not None,
            "ultima_conversa_em": ultima_em,
            "ultimo_estado": ultimo_estado,
        }
        await self._crm.registrar_lead(
            {**execucao.lead, "lead_id": str(execucao.lead_id)}
        )
        self._agendar_entrega()
        logger.info(
            "identidade_resolvida",
            conversa_id=str(execucao.conversa_id),
            lead_novo=conhecido is None,
            opt_out_ativo=opt_out is not None,
        )

    @staticmethod
    def _lead_como_dict(lead: Lead) -> dict:
        return {
            "id": str(lead.id),
            "nome": lead.nome,
            "telefone_e164": lead.telefone_e164,
            "email": lead.email,
            "fonte": lead.fonte,
            "tipo_contrato": lead.tipo_contrato,
            "possui_contrato": lead.possui_contrato,
        }

    async def _gerar_slots(self, execucao: Execucao) -> None:
        agora = self._agora()
        async with self._criar_sessao() as sessao:
            calendario = PostgresCalendarProvider(sessao)
            candidatos = await gerar_candidatos(
                sessao,
                calendario,
                de=agora,
                ate=agora + timedelta(days=self._config.horizonte_dias),
                agora=agora,
                antecedencia_minima_horas=self._config.antecedencia_minima_horas,
                timezone_exibicao=self._config.timezone_exibicao,
            )
            cargas = await carga_futura(sessao, agora)

        ofertas = montar_ofertas(candidatos, cargas, self._config.slots_ofertados)
        fuso = self._config.timezone_exibicao
        execucao.ctx["slots_ofertados"] = [
            {
                "indice": indice,
                "inicio_utc": oferta.inicio_utc.isoformat(),
                "fim_utc": oferta.fim_utc.isoformat(),
                "consultor_id": str(oferta.consultor_id),
                "consultor_nome": oferta.consultor_nome,
                "data_extenso": data_extenso(oferta.inicio_utc, fuso),
                "hora": hora(oferta.inicio_utc, fuso),
            }
            for indice, oferta in enumerate(ofertas, start=1)
        ]

    async def _reservar(self, execucao: Execucao) -> None:
        slot = execucao.ctx.get("slot_escolhido") or {}
        async with self._criar_sessao() as sessao:
            resultado = await reservar(
                sessao,
                lead_id=uuid.UUID(str(execucao.lead_id)),
                consultor_id=uuid.UUID(slot["consultor_id"]),
                inicio_utc=datetime.fromisoformat(slot["inicio_utc"]),
                fim_utc=datetime.fromisoformat(slot["fim_utc"]),
            )
        execucao.ctx["reserva"] = {"ok": resultado.ok, "motivo": resultado.motivo}
        if resultado.ok:
            crm_consultor = await self._crm_usuario_do_consultor(slot.get("consultor_id"))
            await self._crm.registrar_agendamento(
                {
                    **slot,
                    "agendamento_id": str(resultado.agendamento_id),
                    "lead_id": str(execucao.lead_id),
                    "juridico_consultor_id": crm_consultor,
                }
            )
            self._agendar_entrega()
        logger.info(
            "reserva_tentada",
            conversa_id=str(execucao.conversa_id),
            ok=resultado.ok,
            motivo=resultado.motivo,
        )

    async def _crm_usuario_do_consultor(self, consultor_id: str | None) -> str | None:
        if not consultor_id:
            return None
        async with self._criar_sessao() as sessao:
            consultor = await sessao.get(Consultor, uuid.UUID(str(consultor_id)))
            return consultor.crm_usuario_id if consultor else None

    async def _salvar_documento(
        self, execucao: Execucao, efeito: SalvarDocumento
    ) -> None:
        if self._armazenamento is None:
            logger.warning("documento_ignorado_sem_armazenamento")
            return
        guardado = await self._armazenamento.salvar(
            efeito.anexo, str(execucao.lead_id)
        )
        async with self._criar_sessao() as sessao:
            sessao.add(
                Documento(
                    lead_id=uuid.UUID(str(execucao.lead_id)),
                    caminho=guardado.caminho,
                    mime=guardado.mime,
                    tamanho_bytes=guardado.tamanho_bytes,
                )
            )
            await sessao.commit()
        logger.info("documento_arquivado", caminho=guardado.caminho)

    async def _registrar_opt_out(
        self, execucao: Execucao, efeito: RegistrarOptOut
    ) -> None:
        telefone = execucao.lead.get("telefone_e164") or execucao.ctx.get("telefone")
        email = execucao.lead.get("email") or execucao.ctx.get("email")
        if not telefone and not email:
            return
        async with self._criar_sessao() as sessao:
            sessao.add(
                OptOut(telefone_e164=telefone, email=email, motivo=efeito.motivo)
            )
            await sessao.commit()
        logger.info("opt_out_registrado", conversa_id=str(execucao.conversa_id))

    async def _revogar_opt_out(self, execucao: Execucao) -> None:
        telefone = execucao.lead.get("telefone_e164") or execucao.ctx.get("telefone")
        if not telefone:
            return
        async with self._criar_sessao() as sessao:
            ativos = (
                await sessao.execute(
                    select(OptOut).where(
                        OptOut.telefone_e164 == telefone, OptOut.revogado_em.is_(None)
                    )
                )
            ).scalars()
            for registro in ativos:
                registro.revogado_em = self._agora()
            await sessao.commit()
        logger.info("opt_out_revogado", conversa_id=str(execucao.conversa_id))

    async def _escalar(self, execucao: Execucao, efeito: EscalarParaHumano) -> None:
        logger.info(
            "escalado_para_humano",
            conversa_id=str(execucao.conversa_id),
            lead_id=str(execucao.lead_id) if execucao.lead_id else None,
            motivo=efeito.motivo,
        )

    async def _encerrar(self, execucao: Execucao, motivo: str) -> None:
        async with self._criar_sessao() as sessao:
            conversa = await sessao.get(Conversa, execucao.conversa_id)
            if conversa is not None and conversa.encerrada_em is None:
                conversa.encerrada_em = self._agora()
                await sessao.commit()
        logger.info(
            "conversa_encerrada",
            conversa_id=str(execucao.conversa_id),
            estado=execucao.estado.value,
            motivo=motivo,
        )
