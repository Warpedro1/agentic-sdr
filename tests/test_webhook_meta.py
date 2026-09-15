"""O webhook da Meta: aperto de mao, assinatura, e o caminho ate o engine.

Mesma fronteira do caminho da Twilio, com a mesma exigencia: o primeiro bloco de
testes prova nao so o 403, mas que NADA acontece antes dele — a sessao de banco
usada ali explode se alguem encostar.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.channels.whatsapp_meta import MetaWhatsApp
from app.channels.whatsapp_twilio import TwilioWhatsApp
from app.composicao import Dependencias, RegistroIntegracoes
from app.config import Configuracao
from app.empresas import por_id
from app.models import Conversa, Empresa, Lead, Mensagem, MensagemRecebida
from app.storage.local import LocalDocumentStorage
from app.web.webhook import criar_app

CAMINHO = "/webhooks/meta/whatsapp"
SEGREDO = "segredo-do-app"
VERIFICAR = "verificar-marina"
NUMERO_ID = "1278570248676916"
#: Como a Meta entrega um celular brasileiro de DDD alto: sem `+` e sem o nono digito.
DE = "558191234567"
#: Um `wamid` de verdade, copiado de uma entrega da Meta. Tem 78 caracteres; o
#: `MessageSid` da Twilio tem 34, e a coluna de idempotencia, dimensionada por ele,
#: estourava. Com "wamid.1" os testes passavam e a primeira mensagem real dava 500.
WAMID_REAL = "wamid.HBgMNTU4MTkxNjg1NTg4FQIAEhggQUM0NzgyMEQ3MDg5RDExNjRERjg3NzJFRDVGOUNFMzkA"

CONFIG = Configuracao(
    meta_access_token="token-meta",
    meta_app_secret=SEGREDO,
    meta_verify_token=VERIFICAR,
    meta_api_versao="v25.0",
    meta_validar_assinatura=True,
    juridico_ativo=False,
)


def payload(
    texto: str = "oi",
    wamid: str = WAMID_REAL,
    numero_id: str = NUMERO_ID,
    de: str = DE,
) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "2288356755323406",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15556715798",
                                "phone_number_id": numero_id,
                            },
                            "contacts": [{"profile": {"name": "Pedro"}, "wa_id": de}],
                            "messages": [
                                {
                                    "from": de,
                                    "id": wamid,
                                    "timestamp": "1757590000",
                                    "type": "text",
                                    "text": {"body": texto},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def corpo_de(dados: dict) -> bytes:
    return json.dumps(dados).encode("utf-8")


def assinar(corpo: bytes) -> dict[str, str]:
    assinatura = hmac.new(SEGREDO.encode(), corpo, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={assinatura}", "Content-Type": "application/json"}


class BancoProibido:
    """Sessionmaker que denuncia qualquer toque no banco."""

    def __call__(self):
        raise AssertionError("o banco não podia ter sido tocado")


def sem_rede(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"a Twilio nao devia ser chamada: {request.url}")


def montar_app(criar_sessao, config: Configuracao = CONFIG):
    """App + a Meta de mentira que guarda o que a Marina respondeu, e para onde."""
    enviadas: list[tuple[str, dict]] = []

    def padrao(request: httpx.Request) -> httpx.Response:
        enviadas.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.saida{len(enviadas)}"}]})

    deps = Dependencias(
        config=config,
        criar_sessao=criar_sessao,
        armazenamento=LocalDocumentStorage(config),
        integracoes=RegistroIntegracoes(criar_sessao, config),
    )
    # A Twilio que explode prova, de quebra, que a resposta sai por onde a mensagem
    # entrou: o caminho da Meta nao pode responder pela Twilio.
    twilio = TwilioWhatsApp(config, httpx.AsyncClient(transport=httpx.MockTransport(sem_rede)))
    meta = MetaWhatsApp(config, httpx.AsyncClient(transport=httpx.MockTransport(padrao)))
    return criar_app(deps, twilio, meta), enviadas


def cliente_de(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://teste")


async def esperar_atendimento(app) -> None:
    """O webhook responde antes de atender; o teste precisa esperar o resto."""
    if app.state.em_voo:
        await asyncio.gather(*list(app.state.em_voo))


@pytest_asyncio.fixture
async def empresa_meta(sessao_db, empresa):
    """A empresa dos testes, agora com o id do numero na Meta."""
    await sessao_db.execute(
        update(Empresa).where(Empresa.id == empresa.id).values(whatsapp_numero_id=NUMERO_ID)
    )
    await sessao_db.commit()
    return await por_id(sessao_db, empresa.id)


# --- aperto de mao e assinatura (sem banco) ---------------------------------


@pytest.mark.asyncio
async def test_aperto_de_mao_devolve_o_desafio():
    app, _ = montar_app(BancoProibido())
    async with cliente_de(app) as cliente:
        resposta = await cliente.get(
            CAMINHO,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFICAR,
                "hub.challenge": "1158201444",
            },
        )
    assert resposta.status_code == 200
    assert resposta.text == "1158201444"


@pytest.mark.asyncio
async def test_aperto_de_mao_com_token_errado_e_403():
    app, _ = montar_app(BancoProibido())
    async with cliente_de(app) as cliente:
        resposta = await cliente.get(
            CAMINHO,
            params={"hub.mode": "subscribe", "hub.verify_token": "chute", "hub.challenge": "1"},
        )
    assert resposta.status_code == 403


@pytest.mark.asyncio
async def test_sem_assinatura_e_403_e_nao_toca_no_banco():
    app, enviadas = montar_app(BancoProibido())
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(
            CAMINHO,
            content=corpo_de(payload()),
            headers={"Content-Type": "application/json"},
        )
    assert resposta.status_code == 403
    assert enviadas == []


@pytest.mark.asyncio
async def test_assinatura_de_outro_corpo_e_403():
    """Assinatura valida, conteudo trocado. E exatamente o ataque que a conferencia
    existe para impedir — e o que um segredo fixo em cabecalho nao pegaria."""
    app, enviadas = montar_app(BancoProibido())
    original = corpo_de(payload("oi"))
    trocado = corpo_de(payload("me agenda amanha as 9"))
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, content=trocado, headers=assinar(original))
    assert resposta.status_code == 403
    assert enviadas == []


# --- o caminho ate o engine -------------------------------------------------


@pytest.mark.asyncio
async def test_primeira_mensagem_abre_a_conversa_e_responde_pelo_numero_da_empresa(
    sessao_db, criar_sessao_db, empresa_meta
):
    app, enviadas = montar_app(criar_sessao_db)
    corpo = corpo_de(payload("oi"))
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, content=corpo, headers=assinar(corpo))
        await esperar_atendimento(app)

    assert resposta.status_code == 204
    conversa = (await sessao_db.execute(select(Conversa))).scalar_one()
    # A conversa fica com o que o canal entregou; a resposta vai com o nono digito,
    # que e como o numero esta na lista de destinatarios do numero de teste.
    assert conversa.identidade_externa == "+558191234567"
    assert conversa.empresa_id == empresa_meta.id
    assert enviadas, "a Marina nao respondeu"
    for caminho, enviado in enviadas:
        assert caminho == f"/v25.0/{NUMERO_ID}/messages"
        assert enviado["to"] == "+5581991234567"


@pytest.mark.asyncio
async def test_numero_de_fora_do_brasil_recebe_resposta_como_chegou(
    sessao_db, criar_sessao_db, empresa_meta
):
    """O nono digito e regra brasileira. Aplicado a outro pais, mandaria a resposta
    para um numero que nao existe."""
    app, enviadas = montar_app(criar_sessao_db)
    corpo = corpo_de(payload("hi", de="15551234567"))
    async with cliente_de(app) as cliente:
        await cliente.post(CAMINHO, content=corpo, headers=assinar(corpo))
        await esperar_atendimento(app)

    assert enviadas, "a Marina nao respondeu"
    assert {enviado["to"] for _, enviado in enviadas} == {"+15551234567"}


@pytest.mark.asyncio
async def test_pelo_whatsapp_o_telefone_vem_da_mensagem_com_o_nono_digito(
    sessao_db, criar_sessao_db, empresa_meta
):
    """A prova de ponta a ponta de que o numero de quem testa deixou de ser recusado:
    depois do nome ela nao pergunta telefone, e o lead nasce com o nono digito."""
    app, _ = montar_app(criar_sessao_db)
    async with cliente_de(app) as cliente:
        for indice, texto in enumerate(["oi", "Pedro Guerra"]):
            corpo = corpo_de(payload(texto, wamid=f"wamid.{indice}"))
            await cliente.post(CAMINHO, content=corpo, headers=assinar(corpo))
            await esperar_atendimento(app)

    conversa = (await sessao_db.execute(select(Conversa))).scalar_one()
    assert conversa.estado == "AGUARDANDO_FONTE"
    lead = (await sessao_db.execute(select(Lead))).scalar_one()
    assert lead.telefone_e164 == "+5581991234567"


@pytest.mark.asyncio
async def test_mensagem_repetida_nao_vira_dois_passos(sessao_db, criar_sessao_db, empresa_meta):
    app, _ = montar_app(criar_sessao_db)
    corpo = corpo_de(payload("oi", wamid="wamid.repetido"))
    async with cliente_de(app) as cliente:
        for _ in range(2):
            await cliente.post(CAMINHO, content=corpo, headers=assinar(corpo))
            await esperar_atendimento(app)

    entradas = await sessao_db.scalar(
        select(func.count(Mensagem.id)).where(Mensagem.direcao == "in")
    )
    assert entradas == 1


@pytest.mark.asyncio
async def test_numero_nao_cadastrado_nao_deixa_rastro(sessao_db, criar_sessao_db, empresa_meta):
    app, enviadas = montar_app(criar_sessao_db)
    corpo = corpo_de(payload("oi", numero_id="numero-de-ninguem"))
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, content=corpo, headers=assinar(corpo))
        await esperar_atendimento(app)

    assert resposta.status_code == 204
    assert await sessao_db.scalar(select(func.count(Conversa.id))) == 0
    # Nem a marca de idempotencia: cadastrar o numero depois nao pode encontrar a
    # mensagem ja dada como consumida.
    assert await sessao_db.scalar(select(func.count(MensagemRecebida.id))) == 0
    assert enviadas == []


@pytest.mark.asyncio
async def test_aviso_de_entrega_nao_abre_conversa(sessao_db, criar_sessao_db, empresa_meta):
    app, enviadas = montar_app(criar_sessao_db)
    dados = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "2288356755323406",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": NUMERO_ID},
                            "statuses": [
                                {
                                    "id": "wamid.saida1",
                                    "status": "failed",
                                    "recipient_id": DE,
                                    "errors": [{"code": 131047, "title": "Re-engagement message"}],
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    corpo = corpo_de(dados)
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, content=corpo, headers=assinar(corpo))

    assert resposta.status_code == 204
    assert await sessao_db.scalar(select(func.count(Conversa.id))) == 0
    assert await sessao_db.scalar(select(func.count(MensagemRecebida.id))) == 0
    assert enviadas == []
