"""Interface com o CRM externo.

A Marina vai atender mais de um sistema. Este arquivo define o CONTRATO; cada
backend implementa o seu em `app/integrations/<nome>/`.

Duas implementações vivem aqui porque nenhuma delas é específica de backend:

- `CrmSinkNoop` — não envia nada. É o padrão quando a integração está desligada,
  e é o que mantém o MVP funcionando sozinho.
- `CrmSinkOutbox` — grava a intenção na fila local e devolve na hora. A entrega
  de verdade acontece à parte, por um entregador específico do backend.

O engine só conhece esta interface. Ele nunca sabe qual sistema está do outro
lado, nem se existe algum.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.logging import obter_logger
from app.outbox import TipoEnvio, enfileirar

logger = obter_logger("crm")


class CrmSink(Protocol):
    async def registrar_lead(self, lead: dict) -> None: ...

    async def registrar_qualificacao(self, lead: dict) -> None: ...

    async def registrar_agendamento(self, agendamento: dict) -> None: ...


class CrmSinkNoop:
    """Não envia nada para lugar nenhum. Só deixa rastro no log."""

    async def registrar_lead(self, lead: dict) -> None:
        logger.info("crm_desligado_lead", lead_id=lead.get("id"))

    async def registrar_qualificacao(self, lead: dict) -> None:
        logger.info("crm_desligado_qualificacao", lead_id=lead.get("id"))

    async def registrar_agendamento(self, agendamento: dict) -> None:
        logger.info(
            "crm_desligado_agendamento",
            agendamento_id=agendamento.get("agendamento_id"),
        )


class CrmSinkOutbox:
    """Enfileira localmente. NUNCA levanta erro para o engine.

    Se a gravação na fila falhar, o erro é registrado no log e a conversa segue.
    A alternativa — deixar a exceção subir — faria um problema de integração
    derrubar um atendimento em andamento, que é exatamente o que não pode
    acontecer.
    """

    def __init__(
        self,
        criar_sessao: async_sessionmaker[AsyncSession],
        relogio=None,
    ) -> None:
        self._criar_sessao = criar_sessao
        #: Sem relógio, a linha nasce com o `now()` do Postgres, que é o certo em
        #: produção. Quem controla o tempo — os testes — precisa poder informá-lo:
        #: senão o item nasce na hora real e uma drenagem com relógio congelado
        #: nunca o enxerga como pendente.
        self._relogio = relogio

    async def _enfileirar(self, tipo: TipoEnvio, payload: dict) -> None:
        lead_id = payload.get("lead_id")
        try:
            async with self._criar_sessao() as sessao:
                await enfileirar(
                    sessao,
                    tipo,
                    payload,
                    lead_id=uuid.UUID(str(lead_id)) if lead_id else None,
                    agora=self._relogio() if self._relogio else None,
                )
                await sessao.commit()
        except Exception as erro:  # noqa: BLE001 - a conversa nao pode cair por isso
            logger.warning(
                "crm_enfileiramento_falhou", tipo=tipo.value, erro=str(erro)
            )

    async def registrar_lead(self, lead: dict) -> None:
        await self._enfileirar(TipoEnvio.LEAD, dict(lead))

    async def registrar_qualificacao(self, lead: dict) -> None:
        await self._enfileirar(TipoEnvio.QUALIFICACAO, dict(lead))

    async def registrar_agendamento(self, agendamento: dict) -> None:
        await self._enfileirar(TipoEnvio.AGENDAMENTO, dict(agendamento))
