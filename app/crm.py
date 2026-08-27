"""Costura para o CRM externo (o sistema `juridico`).

A integracao esta FORA do MVP e hoje esta bloqueada: o `juridico` nao tem API
maquina-a-maquina. O painel dele anuncia `POST /api/public/oauth/token`, mas essa
rota nao existe no servidor.

Entao aqui fica so o contrato, com uma implementacao que apenas registra no log.
Nao ha cliente HTTP e nao ha contrato de API inventado — quando a porta existir,
basta uma segunda implementacao desta mesma interface.

Dois pontos de atrito conhecidos para quando esse dia chegar (detalhados no
README): o `juridico` distribui leads por ponteiro circular, e nao por carga como
aqui; e o `Lead` de la tem chave unica em CPF e e-mail, mas nao em telefone, que
e a chave de deduplicacao daqui.
"""

from __future__ import annotations

from typing import Protocol

from app.logging import obter_logger

logger = obter_logger("crm")


class CrmSink(Protocol):
    async def registrar_lead(self, lead: dict) -> None: ...

    async def registrar_agendamento(self, agendamento: dict) -> None: ...


class CrmSinkNoop:
    """Nao envia nada para lugar nenhum. Só deixa rastro no log."""

    async def registrar_lead(self, lead: dict) -> None:
        logger.info("crm_lead_pendente", lead_id=lead.get("id"))

    async def registrar_agendamento(self, agendamento: dict) -> None:
        logger.info(
            "crm_agendamento_pendente",
            agendamento_id=agendamento.get("agendamento_id"),
        )
