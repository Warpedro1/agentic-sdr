"""Round-robin por carga. A escolha em si e pura; a leitura da carga usa banco."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.agenda.carga import carga_futura, escolher_consultor

AGORA = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class CandidatoFake:
    consultor_id: uuid.UUID
    consultor_nome: str
    inicio_utc: datetime = AGORA


ANA = CandidatoFake(uuid.uuid4(), "Ana")
BRUNO = CandidatoFake(uuid.uuid4(), "Bruno")
CARLA = CandidatoFake(uuid.uuid4(), "Carla")


def test_sem_candidato_devolve_nada():
    assert escolher_consultor([], {}) is None


def test_ganha_quem_tem_menos_agendamentos():
    cargas = {
        ANA.consultor_id: (5, AGORA),
        BRUNO.consultor_id: (1, AGORA),
        CARLA.consultor_id: (3, AGORA),
    }
    assert escolher_consultor([ANA, BRUNO, CARLA], cargas) is BRUNO


def test_quem_nunca_recebeu_entra_na_frente():
    cargas = {ANA.consultor_id: (2, AGORA)}
    assert escolher_consultor([ANA, BRUNO], cargas) is BRUNO


def test_empate_desempata_por_quem_esta_parado_ha_mais_tempo():
    cargas = {
        ANA.consultor_id: (2, AGORA + timedelta(days=5)),
        BRUNO.consultor_id: (2, AGORA + timedelta(days=1)),
    }
    assert escolher_consultor([ANA, BRUNO], cargas) is BRUNO


def test_empate_total_e_estavel():
    # Sem criterio final, dois empatados alternariam a cada execucao e os testes
    # ficariam intermitentes.
    cargas = {ANA.consultor_id: (2, AGORA), BRUNO.consultor_id: (2, AGORA)}
    escolhas = {escolher_consultor([ANA, BRUNO], cargas) for _ in range(20)}
    assert escolhas == {ANA}


def test_ferias_se_reequilibram_sozinhas():
    # Este e o motivo de nao usar ponteiro circular: com Carla fora, a carga
    # continua distribuida entre quem sobrou, sem fila travada na vez dela.
    cargas = {
        ANA.consultor_id: (4, AGORA),
        BRUNO.consultor_id: (4, AGORA),
        CARLA.consultor_id: (0, None),
    }
    assert escolher_consultor([ANA, BRUNO], cargas) is ANA


@pytest.mark.asyncio
async def test_carga_futura_ignora_o_passado_e_o_cancelado(sessao_db, empresa):
    from app.enums import StatusAgendamento
    from app.models import Agendamento, Consultor, Lead

    consultor = Consultor(
        empresa_id=empresa.id, nome="Ana", email="ana@exemplo.com.br"
    )
    lead = Lead(
        empresa_id=empresa.id, telefone_e164="+5511987654321", canal="terminal"
    )
    sessao_db.add_all([consultor, lead])
    await sessao_db.flush()

    def agendamento(inicio, status=StatusAgendamento.AGENDADO.value):
        return Agendamento(
            lead_id=lead.id,
            consultor_id=consultor.id,
            inicio_utc=inicio,
            fim_utc=inicio + timedelta(hours=1),
            status=status,
        )

    sessao_db.add_all(
        [
            agendamento(AGORA - timedelta(days=1)),  # passado
            agendamento(AGORA + timedelta(days=1)),  # conta
            agendamento(AGORA + timedelta(days=2)),  # conta
            agendamento(
                AGORA + timedelta(days=3), StatusAgendamento.CANCELADO.value
            ),  # cancelado
        ]
    )
    await sessao_db.commit()

    cargas = await carga_futura(sessao_db, AGORA, empresa.id)
    quantidade, ultimo = cargas[consultor.id]
    assert quantidade == 2
    assert ultimo == AGORA + timedelta(days=2)
