"""Reserva e concorrencia.

O teste que importa aqui e o ultimo: dois leads disputando o mesmo horario.
Isso acontece na primeira semana de uso e precisa nascer resolvido.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.booking import reservar
from app.enums import StatusAgendamento
from app.models import Agendamento, Consultor, Lead

INICIO = datetime(2026, 8, 27, 17, 0, tzinfo=UTC)
FIM = INICIO + timedelta(hours=1)


async def _cenario(sessao):
    consultor = Consultor(nome="Ana", email="ana@exemplo.com.br")
    primeiro = Lead(telefone_e164="+5511900000001", canal="terminal")
    segundo = Lead(telefone_e164="+5511900000002", canal="terminal")
    sessao.add_all([consultor, primeiro, segundo])
    await sessao.commit()
    return consultor.id, primeiro.id, segundo.id


@pytest.mark.asyncio
async def test_reserva_simples(sessao_db):
    consultor_id, lead_id, _ = await _cenario(sessao_db)
    resultado = await reservar(sessao_db, lead_id, consultor_id, INICIO, FIM)
    assert resultado.ok
    assert resultado.agendamento_id is not None


@pytest.mark.asyncio
async def test_segunda_reserva_no_mesmo_horario_e_recusada(sessao_db):
    consultor_id, primeiro, segundo = await _cenario(sessao_db)

    assert (await reservar(sessao_db, primeiro, consultor_id, INICIO, FIM)).ok
    recusada = await reservar(sessao_db, segundo, consultor_id, INICIO, FIM)

    assert not recusada.ok
    assert recusada.motivo == "horario ocupado"


@pytest.mark.asyncio
async def test_sobreposicao_parcial_tambem_e_recusada(sessao_db):
    consultor_id, primeiro, segundo = await _cenario(sessao_db)
    await reservar(sessao_db, primeiro, consultor_id, INICIO, FIM)

    # Comeca 30 min depois: o indice unique nao pegaria (inicio diferente), mas a
    # revalidacao por intervalo pega.
    resultado = await reservar(
        sessao_db,
        segundo,
        consultor_id,
        INICIO + timedelta(minutes=30),
        FIM + timedelta(minutes=30),
    )
    assert not resultado.ok


@pytest.mark.asyncio
async def test_horario_cancelado_volta_a_ficar_livre(sessao_db):
    consultor_id, primeiro, segundo = await _cenario(sessao_db)
    reserva = await reservar(sessao_db, primeiro, consultor_id, INICIO, FIM)

    agendamento = await sessao_db.get(Agendamento, reserva.agendamento_id)
    agendamento.status = StatusAgendamento.CANCELADO.value
    await sessao_db.commit()

    # O indice e PARCIAL de proposito: so vale para status 'agendado'.
    segunda = await reservar(sessao_db, segundo, consultor_id, INICIO, FIM)
    assert segunda.ok


@pytest.mark.asyncio
async def test_dois_leads_ao_mesmo_tempo_so_um_agenda(sessao_db, criar_sessao_db):
    """A corrida de verdade: duas sessoes independentes, simultaneas."""
    consultor_id, primeiro, segundo = await _cenario(sessao_db)

    async def tentar(lead_id):
        async with criar_sessao_db() as sessao:
            return await reservar(sessao, lead_id, consultor_id, INICIO, FIM)

    resultados = await asyncio.gather(tentar(primeiro), tentar(segundo))

    vencedores = [r for r in resultados if r.ok]
    perdedores = [r for r in resultados if not r.ok]
    assert len(vencedores) == 1
    assert len(perdedores) == 1
    assert perdedores[0].motivo == "horario ocupado"

    total = await sessao_db.scalar(
        select(func.count(Agendamento.id)).where(
            Agendamento.status == StatusAgendamento.AGENDADO.value
        )
    )
    assert total == 1
