"""A Marina não pode cair junto com o juridico.

Este arquivo existe por um motivo só: a integração acontece DURANTE o
atendimento. Se um problema do outro sistema puder travar a conversa, a
integração deixa a Marina pior do que ela era sem integração nenhuma.
"""

from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.channels.base import InboundMessage
from app.channels.terminal import ScriptedAdapter
from app.crm import CrmSinkOutbox
from app.engine import Engine
from app.enums import Estado
from app.integrations.juridico.cliente import ClienteJuridico
from app.integrations.juridico.entregador import EntregadorJuridico
from app.models import Agendamento, Consultor, Conversa, CrmOutbox, Lead, RegraDisponibilidade
from app.outbox import Backoff

AGORA = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

ROTEIRO = [
    "Pedro Guerra",
    "11 98765-4321",
    "pedro@exemplo.com.br",
    "5",
    "1",
    "2",
    "1",
    "1",
]


async def _semear(sessao):
    consultor = Consultor(
        nome="Ana Ribeiro",
        email="ana@exemplo.com.br",
        ativo=True,
        crm_usuario_id="usuario-juridico-1",
    )
    sessao.add(consultor)
    await sessao.flush()
    for dia in range(0, 5):
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


def _transporte(handler):
    return httpx.MockTransport(handler)


def _juridico_morto(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


def _juridico_vivo(request: httpx.Request) -> httpx.Response:
    caminho = request.url.path
    if caminho == "/api/auth/login":
        return httpx.Response(
            200, json={"token": "t", "refreshToken": "r", "accessTokenExpiresIn": 900}
        )
    if caminho.endswith("/primeiro-contato"):
        return httpx.Response(200, json={"id": "lead-ext"})
    if caminho.endswith("/agendamentos"):
        return httpx.Response(201, json={"id": "ag-ext"})
    return httpx.Response(201, json={"id": "lead-ext"})


def _montar_motor(criar_sessao, handler, agora=AGORA):
    cliente = ClienteJuridico(
        base_url="https://juridico.exemplo",
        login="marina",
        senha="x",
        transport=_transporte(handler),
        relogio=lambda: agora,
    )
    entregador = EntregadorJuridico(
        criar_sessao=criar_sessao,
        cliente=cliente,
        max_tentativas=5,
        relogio=lambda: agora,
        backoff=Backoff(base_segundos=30, teto_segundos=1800),
    )
    motor = Engine(
        criar_sessao=criar_sessao,
        adaptador=ScriptedAdapter([InboundMessage(texto=t) for t in ROTEIRO]),
        crm=CrmSinkOutbox(criar_sessao),
        entregador=entregador,
        relogio=lambda: agora,
    )
    return motor, cliente


@pytest.mark.asyncio
async def test_conversa_termina_normalmente_com_o_juridico_fora_do_ar(
    sessao_db, criar_sessao_db
):
    """O teste mais importante deste arquivo."""
    await _semear(sessao_db)
    motor, cliente = _montar_motor(criar_sessao_db, _juridico_morto)

    conversa_id = await motor.rodar()
    await cliente.fechar()

    conversa = await sessao_db.get(Conversa, conversa_id)
    assert conversa.estado == Estado.AGENDADO.value
    assert conversa.encerrada_em is not None

    # O lead foi cadastrado e a reuniao marcada no banco da Marina, mesmo sem o
    # outro sistema responder.
    lead = (await sessao_db.execute(select(Lead))).scalar_one()
    assert lead.crm_lead_id is None
    assert (await sessao_db.execute(select(Agendamento))).scalar_one() is not None

    # E nada se perdeu: ficou tudo na fila esperando o juridico voltar.
    pendentes = (
        (await sessao_db.execute(select(CrmOutbox).where(CrmOutbox.entregue_em.is_(None))))
        .scalars()
        .all()
    )
    assert {item.tipo for item in pendentes} == {"lead", "qualificacao", "agendamento"}


@pytest.mark.asyncio
async def test_quando_o_juridico_volta_a_fila_entrega_na_ordem(
    sessao_db, criar_sessao_db
):
    await _semear(sessao_db)
    motor, cliente = _montar_motor(criar_sessao_db, _juridico_morto)
    await motor.rodar()
    await cliente.fechar()

    chamadas: list[str] = []

    def vivo_registrando(request):
        if request.url.path != "/api/auth/login":
            chamadas.append(request.url.path)
        return _juridico_vivo(request)

    depois = AGORA + timedelta(hours=1)
    cliente2 = ClienteJuridico(
        base_url="https://juridico.exemplo",
        login="marina",
        senha="x",
        transport=_transporte(vivo_registrando),
        relogio=lambda: depois,
    )
    entregador = EntregadorJuridico(
        criar_sessao=criar_sessao_db, cliente=cliente2, relogio=lambda: depois
    )
    resumo = await entregador.drenar()
    await cliente2.fechar()

    assert resumo["entregues"] == 3
    assert chamadas[0].endswith("/api/admin/leads")
    assert "primeiro-contato" in chamadas[1]
    assert "agendamentos" in chamadas[2]

    lead = (await sessao_db.execute(select(Lead))).scalar_one()
    assert lead.crm_lead_id == "lead-ext"


@pytest.mark.asyncio
async def test_com_o_juridico_no_ar_tudo_chega_durante_a_conversa(
    sessao_db, criar_sessao_db
):
    await _semear(sessao_db)
    motor, cliente = _montar_motor(criar_sessao_db, _juridico_vivo)

    await motor.rodar()
    await cliente.fechar()

    lead = (await sessao_db.execute(select(Lead))).scalar_one()
    assert lead.crm_lead_id == "lead-ext"

    pendentes = (
        (await sessao_db.execute(select(CrmOutbox).where(CrmOutbox.entregue_em.is_(None))))
        .scalars()
        .all()
    )
    assert pendentes == []


@pytest.mark.asyncio
async def test_agendamento_leva_o_consultor_mapeado(sessao_db, criar_sessao_db):
    # Sem o mapeamento, o juridico escolheria outra pessoa e o nome que a Marina
    # prometeu ao lead nao bateria com o da agenda.
    await _semear(sessao_db)
    corpos: list[dict] = []

    def registrando(request):
        if request.url.path.endswith("/agendamentos"):
            import json

            corpos.append(json.loads(request.read().decode()))
        return _juridico_vivo(request)

    motor, cliente = _montar_motor(criar_sessao_db, registrando)
    await motor.rodar()
    await cliente.fechar()

    assert corpos
    assert corpos[0]["consultorId"] == "usuario-juridico-1"


@pytest.mark.asyncio
async def test_qualificacao_leva_o_que_nao_tem_campo_no_juridico(
    sessao_db, criar_sessao_db
):
    await _semear(sessao_db)
    observacoes: list[str] = []

    def registrando(request):
        if request.url.path.endswith("/primeiro-contato"):
            import json

            observacoes.append(json.loads(request.read().decode())["observacao"])
        return _juridico_vivo(request)

    motor, cliente = _montar_motor(criar_sessao_db, registrando)
    await motor.rodar()
    await cliente.fechar()

    assert observacoes
    texto = observacoes[0]
    assert "consignado" in texto
    assert "contrato em mãos" in texto


# --- fiacao do CLI ----------------------------------------------------------
#
# Nao precisam de banco: so verificam quais pecas o CLI monta para cada
# configuracao. Sem isso a fiacao nao e exercitada por teste nenhum — e ela ja
# quebrou uma vez em silencio.


def _sessionmaker_de_mentira():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    return async_sessionmaker(
        create_async_engine("postgresql+psycopg://x:x@localhost:1/x")
    )


def test_cli_desligado_usa_o_noop():
    from app.cli import montar_motor
    from app.config import Configuracao
    from app.crm import CrmSinkNoop

    motor, cliente = montar_motor(
        _sessionmaker_de_mentira(), ScriptedAdapter([]), Configuracao(juridico_ativo=False)
    )
    assert cliente is None
    assert isinstance(motor._crm, CrmSinkNoop)
    assert motor._entregador is None


def test_cli_ligado_usa_a_fila():
    from app.cli import montar_motor
    from app.config import Configuracao

    motor, cliente = montar_motor(
        _sessionmaker_de_mentira(),
        ScriptedAdapter([]),
        Configuracao(
            juridico_ativo=True,
            juridico_login="marina",
            juridico_senha="x",
            juridico_base_url="https://juridico.exemplo",
        ),
    )
    assert cliente is not None
    assert isinstance(motor._crm, CrmSinkOutbox)
    assert motor._entregador is not None


def test_cli_ligado_sem_credencial_cai_no_noop():
    # Marcar ativo=true e esquecer login/senha nao pode virar erro no meio de um
    # atendimento: vira integracao desligada, e a Marina segue funcionando.
    from app.cli import montar_motor
    from app.config import Configuracao
    from app.crm import CrmSinkNoop

    motor, cliente = montar_motor(
        _sessionmaker_de_mentira(), ScriptedAdapter([]), Configuracao(juridico_ativo=True)
    )
    assert cliente is None
    assert isinstance(motor._crm, CrmSinkNoop)
