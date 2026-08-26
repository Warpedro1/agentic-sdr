"""Geracao de horarios.

A primeira metade nao toca banco: `expandir_regra` e pura e da para exercitar
sozinha. A segunda precisa de Postgres.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.availability import expandir_regra, gerar_candidatos
from app.calendar import PostgresCalendarProvider
from app.models import Consultor, RegraDisponibilidade

SP = ZoneInfo("America/Sao_Paulo")


# --- expansao (sem banco) ---------------------------------------------------


def test_janela_de_9_as_18_rende_sete_horarios():
    # Passo = 60 de reuniao + 15 de intervalo = 75 min. O ultimo horario so entra
    # se a reuniao inteira couber antes das 18h.
    dia = datetime(2026, 8, 27, tzinfo=SP)
    horarios = expandir_regra(dia, time(9, 0), time(18, 0), 60, 15, SP)
    assert len(horarios) == 7

    primeiros = [inicio.astimezone(SP).strftime("%H:%M") for inicio, _ in horarios]
    assert primeiros == ["09:00", "10:15", "11:30", "12:45", "14:00", "15:15", "16:30"]


def test_horario_que_nao_cabe_inteiro_fica_de_fora():
    dia = datetime(2026, 8, 27, tzinfo=SP)
    horarios = expandir_regra(dia, time(9, 0), time(10, 30), 60, 15, SP)
    assert len(horarios) == 1  # 09:00; o de 10:15 terminaria 11:15, fora da janela


def test_expansao_devolve_utc():
    dia = datetime(2026, 8, 27, tzinfo=SP)
    (inicio, fim), *_ = expandir_regra(dia, time(9, 0), time(18, 0), 60, 15, SP)
    assert inicio.tzinfo is UTC
    # Sao Paulo e UTC-3: 9h local = 12h UTC.
    assert inicio.hour == 12
    assert (fim - inicio) == timedelta(minutes=60)


def test_sem_buffer_os_horarios_ficam_colados():
    dia = datetime(2026, 8, 27, tzinfo=SP)
    horarios = expandir_regra(dia, time(9, 0), time(12, 0), 60, 0, SP)
    assert [i.astimezone(SP).strftime("%H:%M") for i, _ in horarios] == [
        "09:00",
        "10:00",
        "11:00",
    ]


# --- geracao com banco ------------------------------------------------------


async def _consultor_com_agenda(sessao, nome, email, dias=range(0, 5)):
    consultor = Consultor(nome=nome, email=email, ativo=True)
    sessao.add(consultor)
    await sessao.flush()
    for dia in dias:
        sessao.add(
            RegraDisponibilidade(
                consultor_id=consultor.id,
                dia_semana=dia,
                hora_inicio=time(9, 0),
                hora_fim=time(18, 0),
                duracao_min=60,
                buffer_min=15,
            )
        )
    await sessao.commit()
    return consultor


@pytest.mark.asyncio
async def test_gera_candidatos_para_consultor_ativo(sessao_db):
    await _consultor_com_agenda(sessao_db, "Ana", "ana@exemplo.com.br")

    agora = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)  # quarta, 9h em SP
    candidatos = await gerar_candidatos(
        sessao_db,
        PostgresCalendarProvider(sessao_db),
        de=agora,
        ate=agora + timedelta(days=2),
        agora=agora,
        antecedencia_minima_horas=2,
    )

    assert candidatos
    assert all(c.consultor_nome == "Ana" for c in candidatos)
    assert candidatos == sorted(candidatos, key=lambda c: c.inicio_utc)


@pytest.mark.asyncio
async def test_antecedencia_minima_corta_o_que_e_cedo_demais(sessao_db):
    await _consultor_com_agenda(sessao_db, "Ana", "ana@exemplo.com.br")

    agora = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    candidatos = await gerar_candidatos(
        sessao_db,
        PostgresCalendarProvider(sessao_db),
        de=agora,
        ate=agora + timedelta(days=2),
        agora=agora,
        antecedencia_minima_horas=2,
    )
    assert all(c.inicio_utc >= agora + timedelta(hours=2) for c in candidatos)


@pytest.mark.asyncio
async def test_consultor_inativo_nao_gera_horario(sessao_db):
    consultor = await _consultor_com_agenda(sessao_db, "Ana", "ana@exemplo.com.br")
    consultor.ativo = False
    await sessao_db.commit()

    agora = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    candidatos = await gerar_candidatos(
        sessao_db,
        PostgresCalendarProvider(sessao_db),
        de=agora,
        ate=agora + timedelta(days=2),
        agora=agora,
        antecedencia_minima_horas=2,
    )
    assert candidatos == []


@pytest.mark.asyncio
async def test_horario_ja_ocupado_some_da_lista(sessao_db):
    from app.models import Lead

    consultor = await _consultor_com_agenda(sessao_db, "Ana", "ana@exemplo.com.br")
    lead = Lead(telefone_e164="+5511987654321", canal="terminal")
    sessao_db.add(lead)
    await sessao_db.commit()

    agora = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    calendario = PostgresCalendarProvider(sessao_db)

    antes = await gerar_candidatos(
        sessao_db, calendario, agora, agora + timedelta(days=2), agora, 2
    )
    alvo = antes[0]

    await calendario.criar_evento(
        lead.id, consultor.id, alvo.inicio_utc, alvo.fim_utc
    )
    await sessao_db.commit()

    depois = await gerar_candidatos(
        sessao_db, calendario, agora, agora + timedelta(days=2), agora, 2
    )
    assert alvo.inicio_utc not in [c.inicio_utc for c in depois]
    assert len(depois) == len(antes) - 1


@pytest.mark.asyncio
async def test_fim_de_semana_nao_tem_horario(sessao_db):
    # Agenda so de sabado (5) — para provar que o dia da semana e respeitado.
    await _consultor_com_agenda(sessao_db, "Ana", "ana@exemplo.com.br", dias=[5])

    agora = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)  # quarta
    candidatos = await gerar_candidatos(
        sessao_db,
        PostgresCalendarProvider(sessao_db),
        de=agora,
        ate=agora + timedelta(days=2),  # quarta a sexta
        agora=agora,
        antecedencia_minima_horas=2,
    )
    assert candidatos == []
