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

# Sem o e-mail: com telefone valido a Marina nao pergunta o segundo contato.
ROTEIRO = [
    "Pedro Guerra",
    "11 98765-4321",
    "5",  # fonte
    "1",  # consignado
    "2",  # nao tem o contrato
    "1",  # primeiro horario
    "1",  # confirma
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
    if caminho.endswith("/observacoes"):
        return httpx.Response(201, json={"id": 100})
    if caminho.endswith("/tarefas"):
        return httpx.Response(201, json={"id": 200})
    return httpx.Response(201, json={"id": 42})


def _montar_motor(criar_sessao, handler, agora=AGORA):
    cliente = ClienteJuridico(
        base_url="https://juridico.exemplo",
        token="token-de-teste",
        transport=_transporte(handler),
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
        # O mesmo relogio do motor: sem isso o item nasce com a hora real do
        # Postgres e a drenagem, congelada em AGORA, nao o veria como pendente.
        crm=CrmSinkOutbox(criar_sessao, relogio=lambda: agora),
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
        chamadas.append(request.url.path)
        return _juridico_vivo(request)

    depois = AGORA + timedelta(hours=1)
    cliente2 = ClienteJuridico(
        base_url="https://juridico.exemplo",
        token="token-de-teste",
        transport=_transporte(vivo_registrando),
    )
    entregador = EntregadorJuridico(
        criar_sessao=criar_sessao_db, cliente=cliente2, relogio=lambda: depois
    )
    resumo = await entregador.drenar()
    await cliente2.fechar()

    assert resumo["entregues"] == 3
    assert chamadas[0] == "/api/integracao/negociacoes"
    assert chamadas[1] == "/api/integracao/negociacoes/42/observacoes"
    assert chamadas[2] == "/api/integracao/negociacoes/42/tarefas"

    lead = (await sessao_db.execute(select(Lead))).scalar_one()
    assert lead.crm_lead_id == "42"


@pytest.mark.asyncio
async def test_com_o_juridico_no_ar_tudo_chega_durante_a_conversa(
    sessao_db, criar_sessao_db
):
    await _semear(sessao_db)
    motor, cliente = _montar_motor(criar_sessao_db, _juridico_vivo)

    await motor.rodar()
    await cliente.fechar()

    lead = (await sessao_db.execute(select(Lead))).scalar_one()
    assert lead.crm_lead_id == "42"

    pendentes = (
        (await sessao_db.execute(select(CrmOutbox).where(CrmOutbox.entregue_em.is_(None))))
        .scalars()
        .all()
    )
    assert pendentes == []


@pytest.mark.asyncio
async def test_reuniao_vai_como_tarefa_em_aberto(sessao_db, criar_sessao_db):
    """A regra mudou em 27/08/2026: ninguem e escolhido.

    A reuniao vira tarefa parada no nome do proprio usuario de servico, e quem
    tiver disponibilidade assume. Indicar um consultor aqui voltaria a prometer
    uma pessoa que pode nao ser quem vai aparecer.
    """
    await _semear(sessao_db)
    corpos: list[dict] = []

    def registrando(request):
        if request.url.path.endswith("/tarefas"):
            import json

            corpos.append(json.loads(request.read().decode()))
        return _juridico_vivo(request)

    motor, cliente = _montar_motor(criar_sessao_db, registrando)
    await motor.rodar()
    await cliente.fechar()

    assert corpos
    tarefa = corpos[0]
    # Quem recebe define o responsavel a partir do token: a Marina nao opina.
    assert "responsavel_id" not in tarefa
    assert "consultor_id" not in tarefa
    assert tarefa["tipo"] == "reuniao"
    # Quem pegar a tarefa precisa saber com quem vai falar.
    assert "Pedro Guerra" in tarefa["assunto"]


@pytest.mark.asyncio
async def test_qualificacao_leva_o_que_nao_tem_campo_no_juridico(
    sessao_db, criar_sessao_db
):
    await _semear(sessao_db)
    observacoes: list[str] = []

    def registrando(request):
        if request.url.path.endswith("/observacoes"):
            import json

            observacoes.append(json.loads(request.read().decode())["conteudo"])
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
            juridico_token="token-de-teste",
            juridico_base_url="https://juridico.exemplo",
        ),
    )
    assert cliente is not None
    assert isinstance(motor._crm, CrmSinkOutbox)
    assert motor._entregador is not None


def test_cli_ligado_sem_credencial_cai_no_noop():
    # Marcar ativo=true e esquecer o token nao pode virar erro no meio de um
    # atendimento: vira integracao desligada, e a Marina segue funcionando.
    #
    # O token vazio e passado EXPLICITAMENTE: sem isso o teste lia o .env da
    # maquina e passava ou falhava conforme quem estivesse rodando.
    from app.cli import montar_motor
    from app.config import Configuracao
    from app.crm import CrmSinkNoop

    motor, cliente = montar_motor(
        _sessionmaker_de_mentira(),
        ScriptedAdapter([]),
        Configuracao(juridico_ativo=True, juridico_token=""),
    )
    assert cliente is None
    assert isinstance(motor._crm, CrmSinkNoop)
