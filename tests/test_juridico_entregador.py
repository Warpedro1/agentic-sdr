"""A fila de entrega ao juridico, contra o banco real e um juridico falso."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.integrations.juridico.cliente import ClienteJuridico
from app.integrations.juridico.entregador import EntregadorJuridico
from app.models import CrmOutbox, Lead
from app.outbox import Backoff, TipoEnvio, enfileirar

AGORA = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

NEGOCIACOES = "/api/integracao/negociacoes"


def transporte(**rotas):
    """Servidor de mentira. As rotas casam por prefixo do caminho.

    A ordem de checagem vai do caminho mais longo para o mais curto, senão
    `/negociacoes` engoliria `/negociacoes/1/tarefas`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        for chave in sorted(rotas, key=len, reverse=True):
            if caminho.startswith(chave):
                resposta = rotas[chave]
                return resposta(request) if callable(resposta) else resposta
        return httpx.Response(404, json={"message": "nao mapeada"})

    return httpx.MockTransport(handler)


def montar(criar_sessao, transport, max_tentativas=5, agora=AGORA):
    cliente = ClienteJuridico(
        base_url="https://juridico.exemplo", token="token-de-teste", transport=transport
    )
    return (
        EntregadorJuridico(
            criar_sessao=criar_sessao,
            cliente=cliente,
            max_tentativas=max_tentativas,
            relogio=lambda: agora,
            backoff=Backoff(base_segundos=30, teto_segundos=1800),
        ),
        cliente,
    )


async def _lead(sessao, empresa, telefone="+5511987654321", nome="Pedro Guerra"):
    lead = Lead(
        empresa_id=empresa.id,
        telefone_e164=telefone,
        nome=nome,
        email="pedro@exemplo.com.br",
        fonte="indicacao",
        tipo_contrato="consignado",
        possui_contrato=True,
        canal="terminal",
    )
    sessao.add(lead)
    await sessao.commit()
    return lead


def _dados(lead):
    return {
        "lead_id": str(lead.id),
        "nome": lead.nome,
        "telefone_e164": lead.telefone_e164,
        "email": lead.email,
        "fonte": lead.fonte,
        "tipo_contrato": lead.tipo_contrato,
        "possui_contrato": lead.possui_contrato,
    }


async def _enfileirar(sessao, empresa, tipo, dados, lead_id):
    # `agora=AGORA` importa: sem isso a linha nasce com o now() do Postgres, que
    # e a hora real, e a drenagem — que roda com relogio congelado no passado —
    # nao enxergaria nada como pendente.
    return await enfileirar(
        sessao, tipo, dados, empresa_id=empresa.id, lead_id=lead_id, agora=AGORA
    )


# --- caminho feliz ----------------------------------------------------------


@pytest.mark.asyncio
async def test_entrega_lead_qualificacao_e_agendamento(sessao_db, criar_sessao_db, empresa):
    lead = await _lead(sessao_db, empresa)
    dados = _dados(lead)

    chamadas: list[str] = []

    def rota(request):
        chamadas.append(request.url.path)
        return httpx.Response(201, json={"id": 42})

    await _enfileirar(sessao_db, empresa, TipoEnvio.LEAD, dados, lead.id)
    await _enfileirar(sessao_db, empresa, TipoEnvio.QUALIFICACAO, dados, lead.id)
    await _enfileirar(sessao_db, empresa,
        TipoEnvio.AGENDAMENTO,
        {**dados, "data_iso": "2026-09-03", "hora": "14:00"},
        lead.id,
    )
    await sessao_db.commit()

    entregador, cliente = montar(criar_sessao_db, transporte(**{NEGOCIACOES: rota}))
    resumo = await entregador.drenar()
    await cliente.fechar()

    assert resumo["entregues"] == 3
    assert resumo["falhas"] == 0

    await sessao_db.refresh(lead)
    assert lead.crm_lead_id == "42"
    # A ordem importa: criar a negociacao, depois a observacao da qualificacao,
    # depois a reuniao — que vai como tarefa em aberto dentro da negociacao.
    assert chamadas[0] == NEGOCIACOES
    assert chamadas[1] == f"{NEGOCIACOES}/42/observacoes"
    assert chamadas[2] == f"{NEGOCIACOES}/42/tarefas"


@pytest.mark.asyncio
async def test_lead_ja_enviado_nao_e_enviado_de_novo(sessao_db, criar_sessao_db, empresa):
    lead = await _lead(sessao_db, empresa)
    lead.crm_lead_id = "ja-existe"
    await sessao_db.commit()

    criacoes = {"n": 0}

    def rota(request):
        criacoes["n"] += 1
        return httpx.Response(201, json={"id": 99})

    await _enfileirar(sessao_db, empresa, TipoEnvio.LEAD, _dados(lead), lead.id)
    await sessao_db.commit()

    entregador, cliente = montar(criar_sessao_db, transporte(**{NEGOCIACOES: rota}))
    await entregador.drenar()
    await cliente.fechar()

    # Reentregar criaria uma negociacao duplicada do outro lado.
    assert criacoes["n"] == 0
    await sessao_db.refresh(lead)
    assert lead.crm_lead_id == "ja-existe"


@pytest.mark.asyncio
async def test_negociacao_ja_conhecida_conta_como_entrega(sessao_db, criar_sessao_db, empresa):
    """O juridico devolve 200 quando reconhece a `referencia_externa`.

    Acontece quando a resposta anterior se perdeu no caminho: para ele o envio
    ja tinha chegado, e devolver a negociacao existente e a resposta certa. Aqui
    isso e entrega normal, nao falha — e o id gravado e o mesmo de antes.
    """
    lead = await _lead(sessao_db, empresa)
    await _enfileirar(sessao_db, empresa, TipoEnvio.LEAD, _dados(lead), lead.id)
    await sessao_db.commit()

    entregador, cliente = montar(
        criar_sessao_db,
        transporte(**{NEGOCIACOES: httpx.Response(200, json={"id": 7, "criada": False})}),
    )
    resumo = await entregador.drenar()
    await cliente.fechar()

    assert resumo["entregues"] == 1
    assert resumo["falhas"] == 0
    await sessao_db.refresh(lead)
    assert lead.crm_lead_id == "7"


# --- ordem e resiliencia ----------------------------------------------------


@pytest.mark.asyncio
async def test_falha_no_lead_segura_os_itens_seguintes_dele(sessao_db, criar_sessao_db, empresa):
    lead = await _lead(sessao_db, empresa)
    dados = _dados(lead)

    chamadas: list[str] = []

    def rota(request):
        chamadas.append(request.url.path)
        return httpx.Response(500, text="boom")

    await _enfileirar(sessao_db, empresa, TipoEnvio.LEAD, dados, lead.id)
    await _enfileirar(sessao_db, empresa, TipoEnvio.AGENDAMENTO, dados, lead.id)
    await sessao_db.commit()

    entregador, cliente = montar(criar_sessao_db, transporte(**{NEGOCIACOES: rota}))
    resumo = await entregador.drenar()
    await cliente.fechar()

    assert resumo["falhas"] == 1
    assert resumo["adiados"] == 1
    # O agendamento nem foi tentado: sem a negociacao nem existe endereco para
    # onde mandar a tarefa.
    assert len(chamadas) == 1


@pytest.mark.asyncio
async def test_um_lead_travado_nao_trava_a_fila_de_outro(sessao_db, criar_sessao_db, empresa):
    ruim = await _lead(sessao_db, empresa, telefone="+5511900000001", nome="Falha Certa")
    bom = await _lead(sessao_db, empresa, telefone="+5511900000002", nome="Deve Passar")

    def rota(request):
        corpo = request.read().decode()
        if "Falha Certa" in corpo:
            return httpx.Response(500, text="boom")
        return httpx.Response(201, json={"id": 11})

    await _enfileirar(sessao_db, empresa, TipoEnvio.LEAD, _dados(ruim), ruim.id)
    await _enfileirar(sessao_db, empresa, TipoEnvio.LEAD, _dados(bom), bom.id)
    await sessao_db.commit()

    entregador, cliente = montar(criar_sessao_db, transporte(**{NEGOCIACOES: rota}))
    resumo = await entregador.drenar()
    await cliente.fechar()

    assert resumo["entregues"] == 1
    assert resumo["falhas"] == 1
    await sessao_db.refresh(bom)
    assert bom.crm_lead_id == "11"


@pytest.mark.asyncio
async def test_erro_temporario_agenda_nova_tentativa(sessao_db, criar_sessao_db, empresa):
    lead = await _lead(sessao_db, empresa)
    await _enfileirar(sessao_db, empresa, TipoEnvio.LEAD, _dados(lead), lead.id)
    await sessao_db.commit()

    entregador, cliente = montar(
        criar_sessao_db, transporte(**{NEGOCIACOES: httpx.Response(503, text="off")})
    )
    await entregador.drenar()
    await cliente.fechar()

    item = (await sessao_db.execute(select(CrmOutbox))).scalar_one()
    await sessao_db.refresh(item)
    assert item.tentativas == 1
    assert item.entregue_em is None
    assert item.proxima_tentativa_em > AGORA


@pytest.mark.asyncio
async def test_erro_permanente_nao_fica_tentando_para_sempre(sessao_db, criar_sessao_db, empresa):
    # Um 422 de payload invalido nao melhora com repeticao: queima as tentativas
    # de uma vez e vira algo que uma pessoa precisa olhar.
    lead = await _lead(sessao_db, empresa)
    await _enfileirar(sessao_db, empresa, TipoEnvio.LEAD, _dados(lead), lead.id)
    await sessao_db.commit()

    entregador, cliente = montar(
        criar_sessao_db,
        transporte(
            **{
                NEGOCIACOES: httpx.Response(
                    422,
                    json={
                        "message": "The given data was invalid.",
                        "errors": {"fonte": ["The selected fonte is invalid."]},
                    },
                )
            }
        ),
        max_tentativas=5,
    )
    await entregador.drenar()
    await cliente.fechar()

    item = (await sessao_db.execute(select(CrmOutbox))).scalar_one()
    await sessao_db.refresh(item)
    assert item.tentativas == 5
    # Quem for olhar a fila precisa saber qual campo consertar.
    assert "fonte" in item.ultimo_erro


@pytest.mark.asyncio
async def test_juridico_volta_e_a_fila_entrega_o_que_ficou(sessao_db, criar_sessao_db, empresa):
    lead = await _lead(sessao_db, empresa)
    await _enfileirar(sessao_db, empresa, TipoEnvio.LEAD, _dados(lead), lead.id)
    await sessao_db.commit()

    fora, cliente = montar(
        criar_sessao_db, transporte(**{NEGOCIACOES: httpx.Response(503, text="off")})
    )
    await fora.drenar()
    await cliente.fechar()
    await sessao_db.refresh(lead)
    assert lead.crm_lead_id is None

    # Passa o tempo do backoff e o juridico volta.
    depois = AGORA + timedelta(minutes=10)
    voltou, cliente2 = montar(
        criar_sessao_db,
        transporte(**{NEGOCIACOES: httpx.Response(201, json={"id": 5})}),
        agora=depois,
    )
    resumo = await voltou.drenar()
    await cliente2.fechar()

    assert resumo["entregues"] == 1
    await sessao_db.refresh(lead)
    assert lead.crm_lead_id == "5"


@pytest.mark.asyncio
async def test_drenagens_simultaneas_nao_duplicam_o_lead(sessao_db, criar_sessao_db, empresa):
    """Regressao de um defeito visto contra o juridico de verdade.

    A drenagem e disparada a cada marco da conversa, entao varias rodadas correm
    ao mesmo tempo. Sem reserva de linha, as tres liam o mesmo item pendente e as
    tres o entregavam: UMA conversa virou TRES cadastros no CRM, criados no mesmo
    milissegundo.
    """
    import asyncio

    lead = await _lead(sessao_db, empresa)
    await _enfileirar(sessao_db, empresa, TipoEnvio.LEAD, _dados(lead), lead.id)
    await sessao_db.commit()

    criacoes = {"n": 0}

    def conta(request):
        criacoes["n"] += 1
        return httpx.Response(201, json={"id": 3})

    drenagens = []
    clientes = []
    for _ in range(3):
        entregador, cliente = montar(criar_sessao_db, transporte(**{NEGOCIACOES: conta}))
        drenagens.append(entregador.drenar())
        clientes.append(cliente)

    resumos = await asyncio.gather(*drenagens)
    for cliente in clientes:
        await cliente.fechar()

    assert criacoes["n"] == 1
    assert sum(r["entregues"] for r in resumos) == 1

    await sessao_db.refresh(lead)
    assert lead.crm_lead_id == "3"
