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
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agenda.disponibilidade import gerar_candidatos, montar_ofertas
from app.agenda.reserva import ResultadoReserva, reservar
from app.agenda.calendario import PostgresCalendarProvider
from app.channels.base import ChannelAdapter, InboundMessage, MensagemSaida
from app.config import Configuracao, obter_configuracao
from app.crm import CrmSink, CrmSinkNoop
from app.datas import data_extenso, data_iso, hora
from app.empresas import EmpresaCtx
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
from app.models import Conversa, Documento, Lead, Mensagem, OptOut
from app.parsers import telefone_do_canal
from app.agenda.carga import carga_futura
from app.storage.local import ArmazenamentoDocumento
from app.templates import CatalogoMensagens, obter_catalogo

logger = obter_logger("engine")

#: Chaves que vivem so na memoria da execucao. Nao vao para o JSONB porque
#: carregam objetos ricos (datetime, dataclass) e porque sao resultado de I/O
#: recem-feito — persistir daria a impressao de que continuam validas depois.
CHAVES_TRANSITORIAS = frozenset(
    {
        "agora",
        "politicas",
        "reconhecimento",
        "reserva",
        "canal",
        # Vem da identidade da conversa, que ja esta no banco: gravar de novo seria
        # um segundo lugar onde a mesma verdade pode divergir.
        "telefone_do_canal",
    }
)

#: Numero maximo de reentradas seguidas. Um fluxo saudavel usa 1 ou 2; o limite
#: existe so para que um bug futuro vire erro visivel em vez de laco infinito.
MAX_REENTRADAS = 8

#: Os dois estados que leem `ctx["reconhecimento"]` DEPOIS de esperar o lead
#: responder. Ver `Engine._reconhecimento`.
_ESTADOS_DE_RETOMADA = frozenset(
    {Estado.AGUARDANDO_RETOMADA_OPT_OUT, Estado.AGUARDANDO_RETOMADA_CONVERSA}
)


@dataclass
class Execucao:
    conversa_id: uuid.UUID
    estado: Estado
    lead_id: uuid.UUID | None = None
    lead: dict = field(default_factory=dict)
    ctx: dict = field(default_factory=dict)
    #: Quem escreveu, do jeito que o canal informa. Vazio no terminal, que nao tem
    #: endereco de origem. E o que permite a maquina nao perguntar um telefone que
    #: a conversa ja conhece.
    identidade: str | None = None
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
        *,
        empresa: EmpresaCtx,
        catalogo: CatalogoMensagens | None = None,
        config: Configuracao | None = None,
        armazenamento: ArmazenamentoDocumento | None = None,
        crm: CrmSink | None = None,
        relogio: Callable[[], datetime] | None = None,
        entregador: Any | None = None,
        engine_de_travas: AsyncEngine | None = None,
    ) -> None:
        self._criar_sessao = criar_sessao
        #: De onde sai a conexao que segura a trava do turno. No servidor e um pool
        #: proprio: a trava fica com a conexao pelo turno inteiro, e no mesmo pool das
        #: sessoes N turnos simultaneos prenderiam todas as conexoes nas travas e cada
        #: um esperaria para sempre pela conexao de trabalho. Sem ele (terminal,
        #: testes), usa o mesmo engine das sessoes.
        self._engine_de_travas = engine_de_travas or criar_sessao.kw["bind"]
        self._adaptador = adaptador
        #: Por qual empresa este atendimento acontece. Obrigatoria e sem padrao:
        #: "qualquer empresa" e o defeito que a separacao por empresa existe para
        #: eliminar, e um padrao o traria de volta com cara de conveniencia.
        self._empresa = empresa
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
        """Conduz a conversa ate um estado terminal. Devolve o id da conversa.

        E o laco de um canal que PUXA a mensagem — o terminal. Num canal em que a
        mensagem chega sozinha, a entrada e `atender`.
        """
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
        await self._drenagem_final()
        return execucao.conversa_id

    async def _drenagem_final(self) -> None:
        """Uma ultima rodada, depois que todas as outras terminaram.

        As rodadas disparadas durante a conversa correm em paralelo, e as que
        comecam antes do lead existir no CRM adiam o que depende dele. Sem esta
        rodada final, a qualificacao e a reuniao ficariam na fila esperando a
        proxima conversa — que pode nunca vir.
        """
        if self._entregador is None:
            return
        try:
            await self._entregador.drenar()
        except Exception as erro:  # noqa: BLE001 - a conversa ja terminou bem
            logger.warning("crm_drenagem_final_falhou", erro=str(erro))

    async def atender(self, identidade: str, entrada: InboundMessage) -> uuid.UUID:
        """Atende UMA mensagem que chegou sozinha. Devolve o id da conversa.

        E a entrada dos canais de push (WhatsApp): nao ha laco, nao ha espera, e o
        processo nao fica preso a ninguem. Cada chamada e um passo da conversa
        daquela identidade — milhares delas podem correr em paralelo.
        """
        # A chave inclui a empresa: a mesma pessoa falando com duas empresas tem
        # duas conversas independentes, e uma nao pode esperar pela outra.
        async with self._trava(f"{self._empresa.id}:{identidade}"):
            execucao, nova = await self._localizar_ou_abrir(identidade)
            execucao.identidade = identidade
            await self._registrar_entrada(execucao, entrada)
            # Conversa recem-nascida: a mensagem que a abriu dispara a saudacao e
            # NAO e consumida como resposta. Senao o "oi" viraria tentativa de
            # nome e queimaria uma das tres antes de a Marina ter perguntado
            # qualquer coisa. Ela continua na transcricao, registrada acima.
            await self._processar(execucao, InboundMessage() if nova else entrada)

        await self._aguardar_entregas()
        await self._drenagem_final()
        return execucao.conversa_id

    @asynccontextmanager
    async def _trava(self, chave: str) -> AsyncIterator[None]:
        """Serializa o atendimento de uma mesma identidade.

        Duas mensagens do mesmo lead chegando juntas nao podem avancar o estado ao
        mesmo tempo: uma sobrescreveria o que a outra gravou, e a conversa pularia
        ou repetiria uma pergunta.

        A trava e do Postgres, e nao um lock em memoria, para valer com mais de um
        worker. E de SESSAO, e nao de transacao, porque um turno abre varias
        transacoes curtas (`_aplicar` e cada efeito).
        """
        argumento = {"chave": chave}
        # Uma CONEXAO, e nao uma sessao. A trava pertence a conexao, e a sessao
        # devolve a conexao ao pool a cada commit: o unlock saia por outra conexao,
        # nao soltava nada, e a trava ficava esquecida numa conexao ociosa — a
        # mensagem seguinte daquele numero esperava para sempre.
        async with self._engine_de_travas.connect() as conexao:
            try:
                await conexao.execute(
                    text("SELECT pg_advisory_lock(hashtextextended(:chave, 0))"),
                    argumento,
                )
                # Fecha a transacao mantendo a trava e a conexao, para ela nao passar
                # o turno inteiro idle-in-transaction.
                await conexao.commit()
            except BaseException:
                # Mesmo motivo do unlock abaixo: cancelado ou quebrado entre pegar a
                # trava e entrar no turno, a conexao pode estar com a trava concedida
                # e nao pode voltar ao pool assim.
                await conexao.invalidate()
                raise
            try:
                yield
            finally:
                try:
                    await conexao.execute(
                        text("SELECT pg_advisory_unlock(hashtextextended(:chave, 0))"),
                        argumento,
                    )
                    await conexao.commit()
                except BaseException:
                    # Sem conseguir soltar (turno cancelado, conexao quebrada), a
                    # conexao nao pode voltar ao pool carregando a trava: descartada,
                    # o Postgres solta a trava junto.
                    await conexao.invalidate()
                    raise

    async def _localizar_ou_abrir(self, identidade: str) -> tuple[Execucao, bool]:
        """A conversa aberta desta identidade, ou uma nova. Diz qual dos dois."""
        agora = self._agora()
        limite = agora - timedelta(hours=self._config.janela_retomada_horas)

        async with self._criar_sessao() as sessao:
            conversa = (
                await sessao.execute(
                    select(Conversa).where(
                        Conversa.empresa_id == self._empresa.id,
                        Conversa.canal == self._adaptador.canal,
                        Conversa.identidade_externa == identidade,
                        Conversa.encerrada_em.is_(None),
                    )
                )
            ).scalar_one_or_none()

            if conversa is not None:
                vencida = conversa.ultimo_contato_em < limite
                # Terminal e aberta nao deveria existir (todo terminal emite
                # EncerrarConversa), mas se acontecer o lead ficaria mudo para
                # sempre: a maquina nao transiciona a partir dali.
                encalhada = Estado(conversa.estado) in ESTADOS_TERMINAIS
                if vencida or encalhada:
                    conversa.encerrada_em = agora
                    await sessao.commit()
                    logger.info(
                        "conversa_reciclada",
                        conversa_id=str(conversa.id),
                        motivo="vencida" if vencida else "terminal aberta",
                    )
                    conversa = None

            if conversa is not None:
                return await self._reidratar(sessao, conversa), False

        return await self._abrir(identidade), True

    async def _reidratar(self, sessao: AsyncSession, conversa: Conversa) -> Execucao:
        """Monta a Execucao a partir da linha do banco.

        No terminal a Execucao vive na memoria do laco. Aqui cada mensagem e um
        processo do zero, entao tudo o que o laco carregava precisa voltar da
        conversa — inclusive o contador da transcricao, que nao pode recomecar em
        zero (`mensagens.sequencia` e unico por conversa).
        """
        lead: dict = {}
        if conversa.lead_id is not None:
            registro = await sessao.get(Lead, conversa.lead_id)
            if registro is not None:
                lead = self._lead_como_dict(registro)

        ultima_sequencia = await sessao.scalar(
            select(func.max(Mensagem.sequencia)).where(
                Mensagem.conversa_id == conversa.id
            )
        )

        ctx = dict(conversa.contexto or {})
        # O JSONB devolve texto. `consentimento_em` volta a ser data porque vai
        # direto para uma coluna de data quando o lead nascer.
        consentimento = ctx.get("consentimento_em")
        if isinstance(consentimento, str):
            ctx["consentimento_em"] = datetime.fromisoformat(consentimento)

        estado = Estado(conversa.estado)
        if estado in _ESTADOS_DE_RETOMADA and lead:
            ctx["reconhecimento"] = await self._reconhecimento(sessao, conversa, lead)

        return Execucao(
            conversa_id=conversa.id,
            estado=estado,
            lead_id=conversa.lead_id,
            lead=lead,
            ctx=ctx,
            proxima_sequencia=int(ultima_sequencia or 0),
        )

    async def _reconhecimento(
        self, sessao: AsyncSession, conversa: Conversa, lead: dict
    ) -> dict:
        """Reconstroi o que a maquina precisa nos estados de retomada.

        `reconhecimento` e transitorio de proposito — e resultado de I/O, e
        persisti-lo daria a impressao de que continua valido. Mas os dois estados
        de retomada leem essa chave DEPOIS de esperar o lead responder, e no
        terminal ela sobrevivia na memoria do laco. Aqui ela e refeita do banco,
        que e a fonte, em vez de ressuscitada do JSONB.
        """
        anterior = (
            await sessao.execute(
                select(Conversa)
                .where(Conversa.lead_id == conversa.lead_id, Conversa.id != conversa.id)
                .order_by(desc(Conversa.ultimo_contato_em))
                .limit(1)
            )
        ).scalar_one_or_none()
        return {
            "lead_conhecido": lead,
            "ultimo_estado": anterior.estado if anterior is not None else None,
        }

    async def _abrir(self, identidade: str | None = None) -> Execucao:
        async with self._criar_sessao() as sessao:
            conversa = Conversa(
                empresa_id=self._empresa.id,
                canal=self._adaptador.canal,
                identidade_externa=identidade,
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
            # O numero de quem escreveu, ja em E.164 e com o nono digito reposto.
            # Sem isto a Marina perguntaria no WhatsApp um telefone que a propria
            # mensagem carrega — e recusaria a resposta certa, porque a operadora
            # entrega o celular de DDD alto sem o 9.
            telefone_canal = (
                telefone_do_canal(execucao.identidade) if execucao.identidade else None
            )
            if telefone_canal:
                execucao.ctx["telefone_do_canal"] = telefone_canal

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
            # Reconhecimento pelo contato que existir: telefone quando ha, e-mail
            # quando o telefone nao saiu. Sem nenhum dos dois este efeito nem
            # chega a ser emitido — a maquina escala antes.
            if efeito.telefone:
                criterio = Lead.telefone_e164 == efeito.telefone
            else:
                # Entre os SEM telefone: la o e-mail e a identidade. Um lead que
                # ja tem telefone e outra pessoa, ainda que compartilhe o e-mail.
                criterio = (Lead.email == efeito.email) & Lead.telefone_e164.is_(None)
            existente = (
                await sessao.execute(
                    # A empresa entra aqui: sem ela, a busca acharia o cadastro da
                    # outra empresa e o reaproveitaria, misturando os historicos.
                    select(Lead).where(criterio, Lead.empresa_id == self._empresa.id)
                )
            ).scalar_one_or_none()

            conhecido: dict | None = None
            ultima_em: datetime | None = None
            ultimo_estado: str | None = None

            if existente is None:
                # Lead novo: nasce agora, com o consentimento carimbado na hora em
                # que foi dado (na saudacao), nao neste instante.
                lead = Lead(
                    empresa_id=self._empresa.id,
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

            criterio_opt_out = (
                OptOut.telefone_e164 == efeito.telefone
                if efeito.telefone
                else OptOut.email == efeito.email
            )
            opt_out = (
                await sessao.execute(
                    select(OptOut)
                    .where(
                        criterio_opt_out,
                        OptOut.empresa_id == self._empresa.id,
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
            calendario = PostgresCalendarProvider(sessao, self._empresa.id)
            candidatos = await gerar_candidatos(
                sessao,
                calendario,
                self._empresa.id,
                de=agora,
                ate=agora + timedelta(days=self._config.horizonte_dias),
                agora=agora,
                antecedencia_minima_horas=self._config.antecedencia_minima_horas,
                timezone_exibicao=self._config.timezone_exibicao,
            )
            cargas = await carga_futura(sessao, agora, self._empresa.id)

        ofertas = montar_ofertas(candidatos, cargas, self._config.slots_ofertados)
        fuso = self._config.timezone_exibicao
        execucao.ctx["slots_ofertados"] = [
            {
                "indice": indice,
                "inicio_utc": oferta.inicio_utc.isoformat(),
                "fim_utc": oferta.fim_utc.isoformat(),
                # Quem pode atender, em ordem de carga. Nao e atribuicao: a reuniao
                # vai para o CRM em aberto. Serve de capacidade e de alternativa
                # para a reserva. O lead nunca ve estes nomes.
                "consultores": [str(c.consultor_id) for c in oferta.consultores],
                "data_extenso": data_extenso(oferta.inicio_utc, fuso),
                "hora": hora(oferta.inicio_utc, fuso),
                # Data e hora ja no fuso de exibicao, para o CRM externo receber
                # o dia certo sem ter que reconverter.
                "data_iso": data_iso(oferta.inicio_utc, fuso),
            }
            for indice, oferta in enumerate(ofertas, start=1)
        ]

    async def _reservar(self, execucao: Execucao) -> None:
        """Consome uma vaga do horario escolhido.

        A reuniao nao tem dono: o consultor reservado aqui e uma FICHA DE
        CAPACIDADE, nao uma atribuicao — o que vai para o CRM e uma tarefa em
        aberto. Por isso a reserva percorre todos os livres daquele horario: o
        lead so perde a vaga quando todos acabam, e nao quando o primeiro da lista
        e tomado no meio do caminho.
        """
        slot = execucao.ctx.get("slot_escolhido") or {}
        inicio = datetime.fromisoformat(slot["inicio_utc"])
        fim = datetime.fromisoformat(slot["fim_utc"])
        candidatos = slot.get("consultores") or []

        resultado = None
        for consultor_id in candidatos:
            async with self._criar_sessao() as sessao:
                resultado = await reservar(
                    sessao,
                    lead_id=uuid.UUID(str(execucao.lead_id)),
                    consultor_id=uuid.UUID(str(consultor_id)),
                    inicio_utc=inicio,
                    fim_utc=fim,
                    empresa_id=self._empresa.id,
                )
            if resultado.ok:
                break

        if resultado is None:
            resultado = ResultadoReserva(ok=False, motivo="sem consultor no horario")

        execucao.ctx["reserva"] = {"ok": resultado.ok, "motivo": resultado.motivo}
        if resultado.ok:
            await self._crm.registrar_agendamento(
                {
                    **slot,
                    "agendamento_id": str(resultado.agendamento_id),
                    "lead_id": str(execucao.lead_id),
                }
            )
            self._agendar_entrega()
        logger.info(
            "reserva_tentada",
            conversa_id=str(execucao.conversa_id),
            ok=resultado.ok,
            motivo=resultado.motivo,
            tentados=len(candidatos),
        )

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
                OptOut(
                    empresa_id=self._empresa.id,
                    telefone_e164=telefone,
                    email=email,
                    motivo=efeito.motivo,
                )
            )
            await sessao.commit()
        logger.info("opt_out_registrado", conversa_id=str(execucao.conversa_id))

    async def _revogar_opt_out(self, execucao: Execucao) -> None:
        telefone = execucao.lead.get("telefone_e164") or execucao.ctx.get("telefone")
        email = execucao.lead.get("email") or execucao.ctx.get("email")
        if not telefone and not email:
            return
        criterio = (
            OptOut.telefone_e164 == telefone if telefone else OptOut.email == email
        )
        async with self._criar_sessao() as sessao:
            ativos = (
                await sessao.execute(
                    select(OptOut).where(
                        criterio,
                        OptOut.empresa_id == self._empresa.id,
                        OptOut.revogado_em.is_(None),
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
