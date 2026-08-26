"""Interface de calendario.

O fluxo so conhece este contrato. A implementacao de hoje e o proprio Postgres;
quando o Google Calendar entrar, ele implementa a mesma interface e a maquina de
estados nao muda uma linha.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import StatusAgendamento
from app.models import Agendamento


@dataclass(frozen=True)
class Ocupacao:
    consultor_id: uuid.UUID
    inicio_utc: datetime
    fim_utc: datetime


class CalendarProvider(Protocol):
    async def listar_ocupados(
        self, de: datetime, ate: datetime
    ) -> list[Ocupacao]: ...

    async def criar_evento(
        self,
        lead_id: uuid.UUID,
        consultor_id: uuid.UUID,
        inicio_utc: datetime,
        fim_utc: datetime,
    ) -> uuid.UUID: ...

    async def cancelar_evento(self, agendamento_id: uuid.UUID) -> None: ...


class PostgresCalendarProvider:
    """Implementacao sobre a tabela `agendamentos`."""

    def __init__(self, sessao: AsyncSession) -> None:
        self._sessao = sessao

    async def listar_ocupados(self, de: datetime, ate: datetime) -> list[Ocupacao]:
        linhas = await self._sessao.execute(
            select(
                Agendamento.consultor_id, Agendamento.inicio_utc, Agendamento.fim_utc
            ).where(
                Agendamento.status == StatusAgendamento.AGENDADO.value,
                Agendamento.fim_utc > de,
                Agendamento.inicio_utc < ate,
            )
        )
        return [Ocupacao(*linha) for linha in linhas.all()]

    async def criar_evento(
        self,
        lead_id: uuid.UUID,
        consultor_id: uuid.UUID,
        inicio_utc: datetime,
        fim_utc: datetime,
    ) -> uuid.UUID:
        agendamento = Agendamento(
            lead_id=lead_id,
            consultor_id=consultor_id,
            inicio_utc=inicio_utc,
            fim_utc=fim_utc,
            status=StatusAgendamento.AGENDADO.value,
        )
        self._sessao.add(agendamento)
        await self._sessao.flush()
        return agendamento.id

    async def cancelar_evento(self, agendamento_id: uuid.UUID) -> None:
        agendamento = await self._sessao.get(Agendamento, agendamento_id)
        if agendamento is not None:
            agendamento.status = StatusAgendamento.CANCELADO.value
            await self._sessao.flush()
