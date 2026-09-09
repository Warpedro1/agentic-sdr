"""Reserva de horario.

Dois leads escolhendo o mesmo horario ao mesmo tempo nao e caso raro: aparece na
primeira semana. Por isso a reserva tem duas defesas, e nao uma:

1. `SELECT ... FOR UPDATE` no consultor serializa as tentativas concorrentes
   para o MESMO consultor, e a revalidacao dentro da trava ve o estado real.
2. O indice parcial unique (consultor_id, inicio_utc) WHERE status='agendado'
   e a rede embaixo: se algo escapar da trava, o banco recusa.

Quando a segunda defesa dispara, isso NAO e erro — e o caminho normal do
segundo lead. Ele recebe `slot_perdido` e a lista recalculada.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import StatusAgendamento
from app.models import Agendamento, Consultor


@dataclass(frozen=True)
class ResultadoReserva:
    ok: bool
    agendamento_id: uuid.UUID | None = None
    motivo: str | None = None


async def reservar(
    sessao: AsyncSession,
    lead_id: uuid.UUID,
    consultor_id: uuid.UUID,
    inicio_utc: datetime,
    fim_utc: datetime,
    empresa_id: uuid.UUID,
) -> ResultadoReserva:
    try:
        # 1. Serializa as tentativas concorrentes para este consultor.
        # A empresa entra AQUI, e nao so na oferta: esta e a ultima linha antes de
        # gravar a reuniao. O horario escolhido vem do contexto, que veio do banco
        # como JSONB — mesmo corrompido, um consultor de outra empresa e recusado
        # em vez de confiado.
        travado = await sessao.execute(
            select(Consultor.id)
            .where(Consultor.id == consultor_id, Consultor.empresa_id == empresa_id)
            .with_for_update()
        )
        if travado.scalar_one_or_none() is None:
            return ResultadoReserva(ok=False, motivo="consultor inexistente")

        # 2. Revalida dentro da trava: o mundo pode ter mudado desde a oferta.
        conflito = await sessao.execute(
            select(Agendamento.id).where(
                Agendamento.consultor_id == consultor_id,
                Agendamento.status == StatusAgendamento.AGENDADO.value,
                Agendamento.inicio_utc < fim_utc,
                Agendamento.fim_utc > inicio_utc,
            )
        )
        if conflito.scalar_one_or_none() is not None:
            await sessao.rollback()
            return ResultadoReserva(ok=False, motivo="horario ocupado")

        agendamento = Agendamento(
            lead_id=lead_id,
            consultor_id=consultor_id,
            inicio_utc=inicio_utc,
            fim_utc=fim_utc,
            status=StatusAgendamento.AGENDADO.value,
        )
        sessao.add(agendamento)
        await sessao.commit()
        return ResultadoReserva(ok=True, agendamento_id=agendamento.id)

    except IntegrityError:
        # O indice parcial recusou. Caminho normal do segundo lead, nao falha.
        await sessao.rollback()
        return ResultadoReserva(ok=False, motivo="horario ocupado")
