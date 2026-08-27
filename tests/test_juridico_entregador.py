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


def transporte(**rotas):
    def handler(request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        if caminho == "/api/auth/login":
            return httpx.Response(
                200, json={"token": "t", "refreshToken": "r", "accessTokenExpiresIn": 900}
            )
        for chave, resposta in rotas.items():
            if caminho.startswith(chave):
                return resposta(request) if callable(resposta) else resposta
        return httpx.Response(404, json={"message": "nao mapeada"})

    return httpx.MockTransport(handler)


def montar(criar_sessao, transport, max_tentativas=5, agora=AGORA):
    cliente = ClienteJuridico(
        base_url="https://juridico.exemplo",
        login="marina",
        senha="x",
        transport=transport,
        relogio=lambda: agora,
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


async def _lead(sessao, telefone="+5511987654321", nome="Pedro Guerra"):
    lead = Lead(
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


# --- caminho feliz ----------------------------------------------------------


@pytest.mark.asyncio
async def test_entrega_lead_qualificacao_e_agendamento(sessao_db, criar_sessao_db):
    lead = await _lead(sessao_db)
    dados = _dados(lead)

    chamadas: list[str] = []

    def rota(request):
        chamadas.append(request.url.path)
        if request.url.path.endswith("/agendamentos"):
            return httpx.Response(201, json={"id": "ag-1"})
        if request.url.path.endswith("/primeiro-contato"):
            return httpx.Response(200, json={"id": "lead-ext"})
        return httpx.Response(201, json={"id": "lead-ext"})

    await enfileirar(sessao_db, TipoEnvio.LEAD, dados, lead_id=lead.id)
    await enfileirar(sessao_db, TipoEnvio.QUALIFICACAO, dados, lead_id=lead.id)
    await enfileirar(
        sessao_db,
        TipoEnvio.AGENDAMENTO,
        {**dados, "inicio_utc": "2026-09-03T14:00:00+00:00"},
        lead_id=lead.id,
    )
    await sessao_db.commit()

    entregador, cliente = montar(criar_sessao_db, transporte(**{"/api/admin/leads": rota}))
    resumo = await entregador.drenar()
    await cliente.fechar()

    assert resumo["entregues"] == 3
    assert resumo["falhas"] == 0

    await sessao_db.refresh(lead)
    assert lead.crm_lead_id == "lead-ext"
    # A ordem importa: criar o lead, depois qualificar, depois agendar.
    assert chamadas[0].endswith("/api/admin/leads")
    assert "primeiro-contato" in chamadas[1]
    assert "agendamentos" in chamadas[2]


@pytest.mark.asyncio
async def test_lead_ja_enviado_nao_e_enviado_de_novo(sessao_db, criar_sessao_db):
    lead = await _lead(sessao_db)
    lead.crm_lead_id = "ja-existe"
    await sessao_db.commit()

    criacoes = {"n": 0}

    def rota(request):
        criacoes["n"] += 1
        return httpx.Response(201, json={"id": "outro"})

    await enfileirar(sessao_db, TipoEnvio.LEAD, _dados(lead), lead_id=lead.id)
    await sessao_db.commit()

    entregador, cliente = montar(criar_sessao_db, transporte(**{"/api/admin/leads": rota}))
    await entregador.drenar()
    await cliente.fechar()

    # Reentregar criaria um lead duplicado do outro lado.
    assert criacoes["n"] == 0
    await sessao_db.refresh(lead)
    assert lead.crm_lead_id == "ja-existe"


# --- duplicidade ------------------------------------------------------------


@pytest.mark.asyncio
async def test_lead_que_ja_existe_no_juridico_e_reaproveitado(sessao_db, criar_sessao_db):
    lead = await _lead(sessao_db)

    def rota(request):
        if request.method == "POST":
            return httpx.Response(
                400, json={"message": "TELEFONE já cadastrado para outro lead."}
            )
        return httpx.Response(
            200, json={"items": [{"id": "lead-antigo", "telefoneCelular": "11987654321"}]}
        )

    await enfileirar(sessao_db, TipoEnvio.LEAD, _dados(lead), lead_id=lead.id)
    await sessao_db.commit()

    entregador, cliente = montar(criar_sessao_db, transporte(**{"/api/admin/leads": rota}))
    resumo = await entregador.drenar()
    await cliente.fechar()

    # "Já cadastrado" não é falha: é a pessoa que já estava lá.
    assert resumo["entregues"] == 1
    await sessao_db.refresh(lead)
    assert lead.crm_lead_id == "lead-antigo"


@pytest.mark.asyncio
async def test_duplicado_sem_achar_pelo_telefone_vira_falha_para_humano(
    sessao_db, criar_sessao_db
):
    # Acontece quando a pessoa já existe no juridico cadastrada só por CPF ou
    # e-mail — a Marina não coleta CPF, então não há como casar sozinha.
    lead = await _lead(sessao_db)

    def rota(request):
        if request.method == "POST":
            return httpx.Response(400, json={"message": "CPF já cadastrado para outro lead."})
        return httpx.Response(200, json={"items": []})

    await enfileirar(sessao_db, TipoEnvio.LEAD, _dados(lead), lead_id=lead.id)
    await sessao_db.commit()

    entregador, cliente = montar(criar_sessao_db, transporte(**{"/api/admin/leads": rota}))
    resumo = await entregador.drenar()
    await cliente.fechar()

    assert resumo["falhas"] == 1
    item = (await sessao_db.execute(select(CrmOutbox))).scalar_one()
    assert "nao foi encontrado" in item.ultimo_erro


# --- ordem e resiliencia ----------------------------------------------------


@pytest.mark.asyncio
async def test_falha_no_lead_segura_os_itens_seguintes_dele(sessao_db, criar_sessao_db):
    lead = await _lead(sessao_db)
    dados = _dados(lead)

    chamadas: list[str] = []

    def rota(request):
        chamadas.append(request.url.path)
        return httpx.Response(500, text="boom")

    await enfileirar(sessao_db, TipoEnvio.LEAD, dados, lead_id=lead.id)
    await enfileirar(sessao_db, TipoEnvio.AGENDAMENTO, dados, lead_id=lead.id)
    await sessao_db.commit()

    entregador, cliente = montar(criar_sessao_db, transporte(**{"/api/admin/leads": rota}))
    resumo = await entregador.drenar()
    await cliente.fechar()

    assert resumo["falhas"] == 1
    assert resumo["adiados"] == 1
    # O agendamento nem foi tentado: mandá-lo sem o lead existir seria erro certo.
    assert len(chamadas) == 1


@pytest.mark.asyncio
async def test_um_lead_travado_nao_trava_a_fila_de_outro(sessao_db, criar_sessao_db):
    ruim = await _lead(sessao_db, telefone="+5511900000001", nome="Falha Certa")
    bom = await _lead(sessao_db, telefone="+5511900000002", nome="Deve Passar")

    def rota(request):
        corpo = request.read().decode()
        if "Falha Certa" in corpo:
            return httpx.Response(500, text="boom")
        return httpx.Response(201, json={"id": "lead-ok"})

    await enfileirar(sessao_db, TipoEnvio.LEAD, _dados(ruim), lead_id=ruim.id)
    await enfileirar(sessao_db, TipoEnvio.LEAD, _dados(bom), lead_id=bom.id)
    await sessao_db.commit()

    entregador, cliente = montar(criar_sessao_db, transporte(**{"/api/admin/leads": rota}))
    resumo = await entregador.drenar()
    await cliente.fechar()

    assert resumo["entregues"] == 1
    assert resumo["falhas"] == 1
    await sessao_db.refresh(bom)
    assert bom.crm_lead_id == "lead-ok"


@pytest.mark.asyncio
async def test_erro_temporario_agenda_nova_tentativa(sessao_db, criar_sessao_db):
    lead = await _lead(sessao_db)
    await enfileirar(sessao_db, TipoEnvio.LEAD, _dados(lead), lead_id=lead.id)
    await sessao_db.commit()

    entregador, cliente = montar(
        criar_sessao_db, transporte(**{"/api/admin/leads": httpx.Response(503, text="off")})
    )
    await entregador.drenar()
    await cliente.fechar()

    item = (await sessao_db.execute(select(CrmOutbox))).scalar_one()
    await sessao_db.refresh(item)
    assert item.tentativas == 1
    assert item.entregue_em is None
    assert item.proxima_tentativa_em > AGORA


@pytest.mark.asyncio
async def test_erro_permanente_nao_fica_tentando_para_sempre(sessao_db, criar_sessao_db):
    # Um 400 de payload invalido nao melhora com repeticao: queima as tentativas
    # de uma vez e vira algo que uma pessoa precisa olhar.
    lead = await _lead(sessao_db)
    await enfileirar(sessao_db, TipoEnvio.LEAD, _dados(lead), lead_id=lead.id)
    await sessao_db.commit()

    entregador, cliente = montar(
        criar_sessao_db,
        transporte(
            **{
                "/api/admin/leads": httpx.Response(
                    400, json={"message": "Origem do lead invalida."}
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


@pytest.mark.asyncio
async def test_juridico_volta_e_a_fila_entrega_o_que_ficou(sessao_db, criar_sessao_db):
    lead = await _lead(sessao_db)
    await enfileirar(sessao_db, TipoEnvio.LEAD, _dados(lead), lead_id=lead.id)
    await sessao_db.commit()

    fora, cliente = montar(
        criar_sessao_db, transporte(**{"/api/admin/leads": httpx.Response(503, text="off")})
    )
    await fora.drenar()
    await cliente.fechar()
    await sessao_db.refresh(lead)
    assert lead.crm_lead_id is None

    # Passa o tempo do backoff e o juridico volta.
    depois = AGORA + timedelta(minutes=10)
    voltou, cliente2 = montar(
        criar_sessao_db,
        transporte(**{"/api/admin/leads": httpx.Response(201, json={"id": "lead-ok"})}),
        agora=depois,
    )
    resumo = await voltou.drenar()
    await cliente2.fechar()

    assert resumo["entregues"] == 1
    await sessao_db.refresh(lead)
    assert lead.crm_lead_id == "lead-ok"
