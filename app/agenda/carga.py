"""Distribuicao de leads entre consultores.

Round-robin POR CARGA, nao por ponteiro: entre os que estao livres naquele
horario, ganha quem tem menos agendamentos futuros; empate desempata por quem
esta ha mais tempo sem receber.

A diferenca importa quando alguem sai de ferias. Um ponteiro circular continua
entregando a vez do ausente e a fila trava; a carga se reequilibra sozinha.

(O sistema juridico usa a estrategia oposta, por ponteiro. Quando as duas bases
forem integradas, uma das duas tera de ceder — anotado no README.)
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import StatusAgendamento
from app.models import Agendamento, Consultor


async def carga_futura(
    sessao: AsyncSession, agora: datetime, empresa_id: uuid.UUID
) -> dict[uuid.UUID, tuple[int, datetime | None]]:
    """Por consultor DESTA EMPRESA: quantos agendamentos futuros e o mais distante.

    O escopo vem por juncao com `consultores`, e nao por coluna propria em
    `agendamentos`: a tabela e sempre alcancada pelo consultor, entao a empresa ja
    esta a uma juncao de distancia e uma coluna a mais seria um segundo lugar onde
    a verdade pode divergir.
    """
    linhas = await sessao.execute(
        select(
            Agendamento.consultor_id,
            func.count(Agendamento.id),
            func.max(Agendamento.inicio_utc),
        )
        .join(Consultor, Consultor.id == Agendamento.consultor_id)
        .where(
            Consultor.empresa_id == empresa_id,
            Agendamento.status == StatusAgendamento.AGENDADO.value,
            Agendamento.inicio_utc >= agora,
        )
        .group_by(Agendamento.consultor_id)
    )
    return {
        consultor_id: (quantidade, ultimo)
        for consultor_id, quantidade, ultimo in linhas.all()
    }


def ordenar_por_carga(
    candidatos: Sequence,
    cargas: dict[uuid.UUID, tuple[int, datetime | None]],
) -> list:
    """Os livres naquele horario, do menos carregado para o mais carregado.

    `candidatos` sao objetos com `consultor_id` e `consultor_nome`, todos para o
    MESMO horario. O nome entra na chave de ordenacao so para o desempate ser
    estavel — sem isso, dois consultores empatados alternariam a cada execucao e
    os testes ficariam intermitentes.

    A lista inteira importa, e nao so o primeiro: a reuniao nasce sem dono, entao
    a reserva percorre esta ordem ate uma dar certo. O horario so e perdido quando
    TODOS acabam.
    """

    def chave(candidato):
        quantidade, ultimo = cargas.get(candidato.consultor_id, (0, None))
        # `ultimo is not None` ordena o consultor sem nenhum agendamento futuro
        # na frente: quem esta parado ha mais tempo recebe primeiro.
        return (
            quantidade,
            ultimo is not None,
            ultimo or datetime.min.replace(tzinfo=candidato.inicio_utc.tzinfo),
            candidato.consultor_nome,
        )

    return sorted(candidatos, key=chave)


def escolher_consultor(
    candidatos: Sequence,
    cargas: dict[uuid.UUID, tuple[int, datetime | None]],
):
    """Entre os livres naquele horario, o de menor carga."""
    ordenados = ordenar_por_carga(candidatos, cargas)
    return ordenados[0] if ordenados else None
