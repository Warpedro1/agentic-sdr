"""O webhook da Z-API: segredo, descarte e o caminho ate o engine.

Mesma fronteira dos outros caminhos. Como a Z-API nao assina, a primeira porta e o
segredo no endereco — e, como nos outros, os testes provam nao so o 403, mas que
NADA acontece antes dele. O descarte do que nao e lead (eco do proprio numero,
avisos de status) tambem acontece sem tocar no banco.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.channels.whatsapp_meta import MetaWhatsApp
from app.channels.whatsapp_twilio import TwilioWhatsApp
from app.channels.whatsapp_zapi import ZApiWhatsApp
from app.composicao import Dependencias, RegistroIntegracoes
from app.config import Configuracao
from app.empresas import por_id
from app.models import Conversa, Empresa, Lead, Mensagem, MensagemRecebida
from app.storage.local import LocalDocumentStorage
from app.web.webhook import criar_app

SEGREDO = "segredo-longo-do-webhook-da-zapi"
CAMINHO = f"/webhooks/zapi/{SEGREDO}"
INSTANCIA = "INSTANCIA-TESTE"
TOKEN = "token-da-instancia"
CLIENT_TOKEN = "token-da-conta"
#: Como a Z-API entrega um celular brasileiro de DDD alto: sem `+` e sem o nono digito.
DE = "558191234567"

CONFIG = Configuracao(
    zapi_client_token=CLIENT_TOKEN,
    zapi_webhook_segredo=SEGREDO,
    juridico_ativo=False,
)


def payload(
    texto: str = "oi", message_id: str = "3EB0MSG1", instancia: str = INSTANCIA, **extra
) -> dict:
    return {
        "isStatusReply": False,
        "connectedPhone": "5581900000000",
        "waitingMessage": False,
        "isGroup": False,
        "isNewsletter": False,
        "instanceId": instancia,
        "messageId": message_id,
        "phone": DE,
        "fromMe": False,
        "momment": 1789140000000,
        "status": "RECEIVED",
        "senderName": "Pedro",
        "broadcast": False,
        "type": "ReceivedCallback",
        "text": {"message": texto},
        **extra,
    }


class BancoProibido:
    """Sessionmaker que denuncia qualquer toque no banco."""

    def __call__(self):
        raise AssertionError("o banco não podia ter sido tocado")


def sem_rede(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"este provedor nao devia ser chamado: {request.url}")


def montar_app(criar_sessao, config: Configuracao = CONFIG):
    """App + a Z-API de mentira que guarda o que a Marina respondeu, e por onde."""
    enviadas: list[tuple[str, dict, str | None]] = []

    def padrao(request: httpx.Request) -> httpx.Response:
        enviadas.append(
            (request.url.path, json.loads(request.content), request.headers.get("Client-Token"))
        )
        return httpx.Response(200, json={"zaapId": "Z", "messageId": f"SAIDA{len(enviadas)}"})

    deps = Dependencias(
        config=config,
        criar_sessao=criar_sessao,
        armazenamento=LocalDocumentStorage(config),
        integracoes=RegistroIntegracoes(criar_sessao, config),
    )
    # Twilio e Meta que explodem provam, de quebra, que a resposta sai por onde a
    # mensagem entrou.
    twilio = TwilioWhatsApp(config, httpx.AsyncClient(transport=httpx.MockTransport(sem_rede)))
    meta = MetaWhatsApp(config, httpx.AsyncClient(transport=httpx.MockTransport(sem_rede)))
    zapi = ZApiWhatsApp(config, httpx.AsyncClient(transport=httpx.MockTransport(padrao)))
    return criar_app(deps, twilio, meta, zapi), enviadas


def cliente_de(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://teste")


async def esperar_atendimento(app) -> None:
    """O webhook responde antes de atender; o teste precisa esperar o resto."""
    if app.state.em_voo:
        await asyncio.gather(*list(app.state.em_voo))


@pytest_asyncio.fixture
async def empresa_zapi(sessao_db, empresa, monkeypatch):
    """A empresa dos testes, agora com instancia da Z-API e o token no ambiente."""
    monkeypatch.setenv("ZAPI_TOKEN_LEVE", TOKEN)
    await sessao_db.execute(
        update(Empresa).where(Empresa.id == empresa.id).values(zapi_instancia_id=INSTANCIA)
    )
    await sessao_db.commit()
    return await por_id(sessao_db, empresa.id)


# --- a porta, sem banco -----------------------------------------------------


@pytest.mark.asyncio
async def test_segredo_errado_e_403_e_nao_toca_no_banco():
    app, enviadas = montar_app(BancoProibido())
    async with cliente_de(app) as cliente:
        resposta = await cliente.post("/webhooks/zapi/chute", json=payload())
    assert resposta.status_code == 403
    assert enviadas == []


@pytest.mark.asyncio
async def test_sem_segredo_configurado_tudo_e_recusado():
    """Sem segredo nao ha como provar a origem, e aceitar seria abrir para todos."""
    config = CONFIG.model_copy(update={"zapi_webhook_segredo": ""})
    app, _ = montar_app(BancoProibido(), config)
    async with cliente_de(app) as cliente:
        resposta = await cliente.post("/webhooks/zapi/qualquer", json=payload())
    assert resposta.status_code == 403


@pytest.mark.asyncio
async def test_eco_do_proprio_numero_e_ignorado_sem_tocar_no_banco():
    app, enviadas = montar_app(BancoProibido())
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, json=payload(fromMe=True))
    assert resposta.status_code == 204
    assert enviadas == []


@pytest.mark.asyncio
async def test_aviso_de_status_nao_abre_conversa_nem_toca_no_banco():
    app, enviadas = montar_app(BancoProibido())
    aviso = {
        "instanceId": INSTANCIA,
        "status": "READ",
        "ids": ["SAIDA1"],
        "phone": DE,
        "type": "MessageStatusCallback",
    }
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, json=aviso)
    assert resposta.status_code == 204
    assert enviadas == []


# --- o caminho ate o engine -------------------------------------------------


@pytest.mark.asyncio
async def test_primeira_mensagem_abre_a_conversa_e_responde_pela_instancia(
    sessao_db, criar_sessao_db, empresa_zapi
):
    app, enviadas = montar_app(criar_sessao_db)
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, json=payload("oi"))
        await esperar_atendimento(app)

    assert resposta.status_code == 204
    conversa = (await sessao_db.execute(select(Conversa))).scalar_one()
    assert conversa.identidade_externa == "+558191234567"
    assert conversa.empresa_id == empresa_zapi.id
    assert enviadas, "a Marina nao respondeu"
    for caminho, enviado, client_token in enviadas:
        assert caminho == f"/instances/{INSTANCIA}/token/{TOKEN}/send-text"
        assert enviado["phone"] == DE
        assert client_token == CLIENT_TOKEN


@pytest.mark.asyncio
async def test_pelo_whatsapp_o_telefone_vem_da_mensagem_com_o_nono_digito(
    sessao_db, criar_sessao_db, empresa_zapi
):
    """Depois do nome ela nao pergunta telefone, e o lead nasce com o nono digito."""
    app, _ = montar_app(criar_sessao_db)
    async with cliente_de(app) as cliente:
        for indice, texto in enumerate(["oi", "Pedro Guerra"]):
            await cliente.post(CAMINHO, json=payload(texto, message_id=f"3EB0{indice}"))
            await esperar_atendimento(app)

    conversa = (await sessao_db.execute(select(Conversa))).scalar_one()
    assert conversa.estado == "AGUARDANDO_FONTE"
    lead = (await sessao_db.execute(select(Lead))).scalar_one()
    assert lead.telefone_e164 == "+5581991234567"


@pytest.mark.asyncio
async def test_mensagem_repetida_nao_vira_dois_passos(sessao_db, criar_sessao_db, empresa_zapi):
    app, _ = montar_app(criar_sessao_db)
    async with cliente_de(app) as cliente:
        for _ in range(2):
            await cliente.post(CAMINHO, json=payload("oi", message_id="3EB0REPETIDA"))
            await esperar_atendimento(app)

    entradas = await sessao_db.scalar(
        select(func.count(Mensagem.id)).where(Mensagem.direcao == "in")
    )
    assert entradas == 1


@pytest.mark.asyncio
async def test_instancia_nao_cadastrada_nao_deixa_rastro(
    sessao_db, criar_sessao_db, empresa_zapi
):
    app, enviadas = montar_app(criar_sessao_db)
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, json=payload("oi", instancia="DE-NINGUEM"))
        await esperar_atendimento(app)

    assert resposta.status_code == 204
    assert await sessao_db.scalar(select(func.count(Conversa.id))) == 0
    # Nem a marca de idempotencia: cadastrar a instancia depois nao pode encontrar a
    # mensagem ja dada como consumida.
    assert await sessao_db.scalar(select(func.count(MensagemRecebida.id))) == 0
    assert enviadas == []
