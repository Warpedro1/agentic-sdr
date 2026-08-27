"""Geracao de horarios livres.

O juridico (o outro sistema da casa) nao tem nocao nenhuma de disponibilidade:
la um Agendamento exige uma data que um humano digitou. Tudo neste modulo e
novo, e e a peca mais delicada do MVP.

As regras de disponibilidade sao escritas em horario LOCAL (o consultor atende
das 9h as 18h de Sao Paulo, nao de UTC). A expansao monta o horario local e so
entao converte para UTC, que e como tudo e gravado.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar import CalendarProvider
from app.models import Consultor, RegraDisponibilidade


@dataclass(frozen=True)
class Candidato:
    """Um horario livre de um consultor especifico."""

    consultor_id: uuid.UUID
    consultor_nome: str
    inicio_utc: datetime
    fim_utc: datetime


def expandir_regra(
    dia: datetime,
    hora_inicio,
    hora_fim,
    duracao_min: int,
    buffer_min: int,
    fuso: ZoneInfo,
) -> list[tuple[datetime, datetime]]:
    """Fatia uma janela de atendimento em horarios de `duracao_min`.

    O passo e duracao + buffer: o intervalo entre reunioes e do consultor, nao
    do lead. Um horario so entra se couber inteiro dentro da janela.
    """
    passo = timedelta(minutes=duracao_min + buffer_min)
    duracao = timedelta(minutes=duracao_min)

    inicio_local = datetime.combine(dia.date(), hora_inicio, tzinfo=fuso)
    limite_local = datetime.combine(dia.date(), hora_fim, tzinfo=fuso)

    horarios: list[tuple[datetime, datetime]] = []
    atual = inicio_local
    while atual + duracao <= limite_local:
        horarios.append((atual.astimezone(UTC), (atual + duracao).astimezone(UTC)))
        atual += passo
    return horarios


def _colide(inicio: datetime, fim: datetime, ocupados) -> bool:
    return any(inicio < ocupado.fim_utc and fim > ocupado.inicio_utc for ocupado in ocupados)


async def gerar_candidatos(
    sessao: AsyncSession,
    calendario: CalendarProvider,
    de: datetime,
    ate: datetime,
    agora: datetime,
    antecedencia_minima_horas: int,
    timezone_exibicao: str = "America/Sao_Paulo",
) -> list[Candidato]:
    """Horarios livres de todos os consultores ativos, ordenados por horario."""
    fuso = ZoneInfo(timezone_exibicao)
    minimo = agora + timedelta(hours=antecedencia_minima_horas)

    linhas = await sessao.execute(
        select(Consultor, RegraDisponibilidade)
        .join(RegraDisponibilidade, RegraDisponibilidade.consultor_id == Consultor.id)
        .where(Consultor.ativo.is_(True))
    )
    regras_por_consultor = linhas.all()
    if not regras_por_consultor:
        return []

    ocupados = await calendario.listar_ocupados(de, ate)
    ocupados_por_consultor: dict[uuid.UUID, list] = {}
    for ocupacao in ocupados:
        ocupados_por_consultor.setdefault(ocupacao.consultor_id, []).append(ocupacao)

    candidatos: list[Candidato] = []
    dias = (ate.date() - de.date()).days + 1

    for consultor, regra in regras_por_consultor:
        for offset in range(dias):
            # O dia da semana da regra e LOCAL: "segunda a sexta" e a semana do
            # consultor em Sao Paulo, nao em UTC. Perto da meia-noite os dois
            # divergem, e usar UTC aqui geraria horario em dia errado.
            dia = (de + timedelta(days=offset)).astimezone(fuso)
            if dia.weekday() != regra.dia_semana:
                continue

            for inicio, fim in expandir_regra(
                dia,
                regra.hora_inicio,
                regra.hora_fim,
                regra.duracao_min,
                regra.buffer_min,
                fuso,
            ):
                if inicio < minimo or inicio > ate:
                    continue
                if _colide(inicio, fim, ocupados_por_consultor.get(consultor.id, ())):
                    continue
                candidatos.append(
                    Candidato(
                        consultor_id=consultor.id,
                        consultor_nome=consultor.nome,
                        inicio_utc=inicio,
                        fim_utc=fim,
                    )
                )

    candidatos.sort(key=lambda c: (c.inicio_utc, c.consultor_nome))
    return candidatos


def montar_ofertas(
    candidatos: list[Candidato],
    cargas: dict[uuid.UUID, tuple[int, datetime | None]],
    limite: int,
) -> list[Candidato]:
    """Mescla os consultores num unico menu de horarios.

    O lead escolhe um HORARIO, nao um consultor. Para cada horario distinto,
    quem atende e decidido aqui pelo round-robin por carga — e ja fica definido,
    porque a mensagem de confirmacao diz o nome antes de reservar.
    """
    from app.routing import escolher_consultor

    por_horario: dict[datetime, list[Candidato]] = {}
    for candidato in candidatos:
        por_horario.setdefault(candidato.inicio_utc, []).append(candidato)

    ofertas: list[Candidato] = []
    for inicio in sorted(por_horario)[:limite]:
        escolhido = escolher_consultor(por_horario[inicio], cargas)
        if escolhido is not None:
            ofertas.append(escolhido)
    return ofertas
