"""Drena a fila local entregando ao juridico.

Roda fora da conversa. Se falhar, ninguém no atendimento percebe — o item volta
para a fila e é tentado de novo.

A regra que mais importa aqui é a **ordem por lead**: um agendamento não pode
ser enviado antes de o lead existir do outro lado. Se o envio do lead falhar,
os itens seguintes daquele lead esperam a próxima rodada; os de outros leads
seguem normalmente, porque uma pessoa não deve travar a fila de outra.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations.juridico.cliente import ClienteJuridico, ErroJuridico
from app.integrations.juridico.mapeamento import (
    payload_negociacao,
    payload_tarefa,
    texto_qualificacao,
)
from app.logging import obter_logger
from app.models import CrmOutbox, Lead
from app.outbox import Backoff, TipoEnvio, marcar_entregue, marcar_falha, pendentes

logger = obter_logger("juridico.entregador")


class LeadAindaNaoEnviado(ErroJuridico):
    """O lead ainda não existe no juridico; este item precisa esperar."""

    def __init__(self) -> None:
        super().__init__("lead ainda nao foi criado no juridico", status=None)


class EntregadorJuridico:
    def __init__(
        self,
        criar_sessao: async_sessionmaker[AsyncSession],
        cliente: ClienteJuridico,
        max_tentativas: int = 5,
        canal_contato: str = "WHATSAPP",
        backoff: Backoff | None = None,
        relogio=None,
    ) -> None:
        self._criar_sessao = criar_sessao
        self._cliente = cliente
        self._max_tentativas = max_tentativas
        self._canal = canal_contato
        self._backoff = backoff or Backoff()
        self._relogio = relogio or (lambda: datetime.now(UTC))

    async def drenar(self, limite: int = 100) -> dict[str, int]:
        """Tenta entregar tudo o que está pendente. Devolve o resumo."""
        agora = self._relogio()
        resumo = {"entregues": 0, "falhas": 0, "adiados": 0}
        # Leads cujo envio falhou nesta rodada: os itens seguintes deles ficam
        # para a proxima, senao mandariamos agendamento de um lead inexistente.
        travados: set[uuid.UUID] = set()

        async with self._criar_sessao() as sessao:
            fila = await pendentes(sessao, agora, self._max_tentativas, limite)

            for item in fila:
                if item.lead_id and item.lead_id in travados:
                    resumo["adiados"] += 1
                    continue
                try:
                    await self._entregar(sessao, item)
                    marcar_entregue(item, agora)
                    resumo["entregues"] += 1
                except LeadAindaNaoEnviado:
                    # NAO e falha: e ordem. O lead esta sendo criado por outra
                    # rodada que ainda nao commitou. Contar como tentativa faria
                    # o item esperar o backoff a toa, e ele so precisa da rodada
                    # seguinte — que acontece em milissegundos.
                    resumo["adiados"] += 1
                    if item.lead_id:
                        travados.add(item.lead_id)
                    continue
                except ErroJuridico as erro:
                    marcar_falha(
                        item,
                        str(erro),
                        agora,
                        temporario=erro.temporario,
                        max_tentativas=self._max_tentativas,
                        backoff=self._backoff,
                    )
                    resumo["falhas"] += 1
                    if item.lead_id:
                        travados.add(item.lead_id)
                    logger.warning(
                        "crm_entrega_falhou",
                        tipo=item.tipo,
                        temporario=erro.temporario,
                        tentativas=item.tentativas,
                        erro=str(erro),
                    )
            await sessao.commit()

        if resumo["entregues"] or resumo["falhas"]:
            logger.info("crm_fila_drenada", **resumo)
        return resumo

    # --- por tipo -----------------------------------------------------------

    async def _entregar(self, sessao: AsyncSession, item: CrmOutbox) -> None:
        if item.tipo == TipoEnvio.LEAD.value:
            await self._entregar_lead(sessao, item)
        elif item.tipo == TipoEnvio.QUALIFICACAO.value:
            await self._entregar_qualificacao(sessao, item)
        elif item.tipo == TipoEnvio.AGENDAMENTO.value:
            await self._entregar_agendamento(sessao, item)
        else:
            raise ErroJuridico(f"tipo de envio desconhecido: {item.tipo}", status=400)

    async def _lead_local(self, sessao: AsyncSession, item: CrmOutbox) -> Lead | None:
        if not item.lead_id:
            return None
        return await sessao.get(Lead, item.lead_id)

    async def _entregar_lead(self, sessao: AsyncSession, item: CrmOutbox) -> None:
        """Cria a negociação do lead. `crm_lead_id` guarda o id dela.

        Duas defesas contra duplicata, e elas cobrem coisas diferentes: aqui, a
        guarda local impede reenviar o que já foi; do outro lado, a
        `referencia_externa` cobre a janela entre o commit remoto e o local — se
        a resposta se perder no caminho, o juridico reconhece o reenvio e
        devolve a mesma negociação em vez de criar outra.
        """
        lead = await self._lead_local(sessao, item)
        if lead is None:
            raise ErroJuridico("lead local nao encontrado", status=404)
        if lead.crm_lead_id:
            return  # ja enviado antes; entregar de novo criaria duplicata

        lead.crm_lead_id = await self._cliente.criar_negociacao(
            payload_negociacao(item.payload or {}, canal=self._canal)
        )
        await sessao.flush()

    async def _entregar_qualificacao(
        self, sessao: AsyncSession, item: CrmOutbox
    ) -> None:
        lead = await self._lead_local(sessao, item)
        if lead is None or not lead.crm_lead_id:
            raise LeadAindaNaoEnviado()
        await self._cliente.registrar_observacao(
            lead.crm_lead_id,
            texto_qualificacao(item.payload or {}, canal=self._canal),
        )

    async def _entregar_agendamento(
        self, sessao: AsyncSession, item: CrmOutbox
    ) -> None:
        """A reunião vai como TAREFA em aberto, presa à negociação do lead.

        Depende da negociação existir — não só pelo texto: a tarefa é criada
        *dentro* dela, então sem o id sequer existe endereço para onde enviar.
        """
        lead = await self._lead_local(sessao, item)
        if lead is None or not lead.crm_lead_id:
            raise LeadAindaNaoEnviado()

        await self._cliente.criar_tarefa(
            lead.crm_lead_id,
            payload_tarefa(
                item.payload or {},
                lead={
                    "nome": lead.nome,
                    "telefone_e164": lead.telefone_e164,
                    "email": lead.email,
                    "fonte": lead.fonte,
                    "fonte_raw": lead.fonte_raw,
                    "tipo_contrato": lead.tipo_contrato,
                    "possui_contrato": lead.possui_contrato,
                },
            ),
        )
