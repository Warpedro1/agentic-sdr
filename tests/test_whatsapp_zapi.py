"""O adaptador da Z-API, sem banco e sem rede.

A Z-API entrega JSON plano, com o telefone sem `+`, e mistura na mesma entrada o que
o proprio numero manda, grupos e status — a conexao e de aparelho do WhatsApp Web,
nao da API oficial. Estes testes provam a traducao nas duas direcoes, o que e
descartado, e que o token da instancia, que viaja NA URL, nao escapa para o log.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.channels import whatsapp_zapi
from app.channels.base import MensagemSaida
from app.channels.whatsapp_zapi import (
    ZApiWhatsApp,
    destino_de,
    falha_da_resposta,
    identidade_de,
    motivo_para_ignorar,
)
from app.config import Configuracao
from app.parsers import telefone_do_canal

INSTANCIA = "INSTANCIA-TESTE"
TOKEN = "token-da-instancia-que-nao-pode-vazar"
CLIENT_TOKEN = "token-da-conta"
#: Como a Z-API entrega um celular brasileiro de DDD alto: sem `+` e sem o nono digito.
DE = "558191234567"

CONFIG = Configuracao(
    zapi_client_token=CLIENT_TOKEN,
    zapi_webhook_segredo="segredo",
    zapi_url_base="https://api.z-api.io",
)


def payload_texto(texto: str = "oi", message_id: str = "3EB0A1", phone: str = DE, **extra) -> dict:
    return {
        "isStatusReply": False,
        "connectedPhone": "5581900000000",
        "waitingMessage": False,
        "isEdit": False,
        "isGroup": False,
        "isNewsletter": False,
        "instanceId": INSTANCIA,
        "messageId": message_id,
        "phone": phone,
        "fromMe": False,
        "momment": 1789140000000,
        "status": "RECEIVED",
        "chatName": "Pedro",
        "senderName": "Pedro",
        "broadcast": False,
        "type": "ReceivedCallback",
        "text": {"message": texto},
        **extra,
    }


def sem_rede(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"nao devia chamar a rede: {request.url}")


def zapi_com(handler, config: Configuracao = CONFIG) -> ZApiWhatsApp:
    return ZApiWhatsApp(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class LogFalso:
    """Guarda o que o adaptador registraria, para conferir o CONTEUDO do log."""

    def __init__(self) -> None:
        self.eventos: list[tuple[str, str, dict]] = []

    def __getattr__(self, nivel: str):
        def registrar(evento: str, **campos) -> None:
            self.eventos.append((nivel, evento, campos))

        return registrar

    def nomes(self) -> list[str]:
        return [evento for _, evento, _ in self.eventos]


@pytest.fixture
def log(monkeypatch) -> LogFalso:
    falso = LogFalso()
    monkeypatch.setattr(whatsapp_zapi, "logger", falso)
    return falso


# --- formato ----------------------------------------------------------------


def test_identidade_ganha_o_mais_e_o_lid_fica_como_veio():
    assert identidade_de(DE) == "+558191234567"
    assert identidade_de("12345678901234@lid") == "12345678901234@lid"
    assert identidade_de("") == ""


def test_destino_volta_ao_formato_da_z_api():
    assert destino_de("+558191234567") == DE
    assert destino_de("12345678901234@lid") == "12345678901234@lid"


# --- o que e descartado -----------------------------------------------------


@pytest.mark.parametrize(
    "marcador",
    ["fromMe", "isGroup", "isNewsletter", "broadcast", "isStatusReply", "waitingMessage"],
)
def test_o_que_nao_e_lead_falando_com_a_empresa_e_ignorado(marcador):
    """`fromMe` e o que mais importa: sem ele, o eco de cada resposta da Marina
    viraria mensagem nova, e a conversa responderia a si mesma."""
    assert motivo_para_ignorar(payload_texto(**{marcador: True})) is not None


def test_mensagem_de_lead_nao_e_ignorada():
    assert motivo_para_ignorar(payload_texto()) is None


# --- entrada ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_mensagem_de_texto_vira_entrada_tipada():
    recebida = await zapi_com(sem_rede).receber_payload(payload_texto("Pedro Guerra"))

    assert recebida is not None
    assert recebida.identidade == "+558191234567"
    assert recebida.entrada.texto == "Pedro Guerra"
    assert recebida.entrada.anexo is None
    assert recebida.message_sid == "3EB0A1"
    assert recebida.destinatario == INSTANCIA
    assert recebida.profile_name == "Pedro"


@pytest.mark.asyncio
async def test_contato_com_numero_oculto_nao_vira_telefone():
    """Sem telefone de verdade, a Marina precisa perguntar — e nao gravar os digitos
    do identificador como se fossem um numero."""
    recebida = await zapi_com(sem_rede).receber_payload(
        payload_texto(phone="12345678901234@lid")
    )

    assert recebida is not None
    assert recebida.identidade == "12345678901234@lid"
    assert telefone_do_canal(recebida.identidade) is None


@pytest.mark.parametrize(
    "chave,bloco",
    [
        ("reaction", {"value": "❤️", "referencedMessage": {"messageId": "X"}}),
        ("sticker", {"stickerUrl": "https://z/fig.webp", "mimeType": "image/webp"}),
        ("location", {"latitude": -8.0, "longitude": -34.9}),
    ],
)
@pytest.mark.asyncio
async def test_reacao_figurinha_e_localizacao_nao_viram_mensagem(chave, bloco):
    """Nenhuma responde a pergunta que a Marina fez; passar adiante contaria tentativa."""
    payload = payload_texto()
    del payload["text"]
    payload[chave] = bloco

    assert await zapi_com(sem_rede).receber_payload(payload) is None


@pytest.mark.asyncio
async def test_documento_e_baixado_numa_chamada_so():
    chamadas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-1.4 contrato")

    payload = payload_texto()
    del payload["text"]
    payload["document"] = {
        "documentUrl": "https://storage.z-api.io/arquivo",
        "mimeType": "application/pdf",
        "fileName": "contrato.pdf",
    }
    recebida = await zapi_com(handler).receber_payload(payload)

    assert recebida is not None
    try:
        anexo = recebida.entrada.anexo
        assert anexo is not None
        assert anexo.mime == "application/pdf"
        assert anexo.nome_arquivo.endswith(".pdf")
        assert anexo.tamanho_bytes == len(b"%PDF-1.4 contrato")
        assert recebida.temporario is not None and recebida.temporario.is_file()
        # A URL da midia e direta: nada da dupla chamada da Meta.
        assert chamadas == ["https://storage.z-api.io/arquivo"]
    finally:
        if recebida.temporario is not None:
            recebida.temporario.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_midia_de_tipo_nao_aceito_nem_chega_a_ser_baixada():
    payload = payload_texto()
    del payload["text"]
    payload["audio"] = {"audioUrl": "https://z/audio.ogg", "mimeType": "audio/ogg; codecs=opus"}

    recebida = await zapi_com(sem_rede).receber_payload(payload)

    assert recebida is not None
    assert recebida.entrada.anexo is None


# --- saida ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_envio_monta_a_chamada_da_z_api():
    capturadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        capturadas.append(request)
        return httpx.Response(200, json={"zaapId": "Z1", "messageId": "M1", "id": "M1"})

    await zapi_com(handler).enviar_para(
        "+558191234567", MensagemSaida("boas_vindas", "Olá!"), INSTANCIA, TOKEN
    )

    (pedido,) = capturadas
    assert pedido.method == "POST"
    assert pedido.url.path == f"/instances/{INSTANCIA}/token/{TOKEN}/send-text"
    assert pedido.headers["Client-Token"] == CLIENT_TOKEN
    # Responde para o `phone` como chegou: so digitos, sem reescrever o numero.
    assert json.loads(pedido.content) == {"phone": DE, "message": "Olá!"}


@pytest.mark.asyncio
async def test_sem_token_da_conta_o_cabecalho_nao_vai():
    capturadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        capturadas.append(request)
        return httpx.Response(200, json={"messageId": "M1"})

    config = CONFIG.model_copy(update={"zapi_client_token": ""})
    await zapi_com(handler, config).enviar_para(
        "+558191234567", MensagemSaida("x", "y"), INSTANCIA, TOKEN
    )

    assert "Client-Token" not in capturadas[0].headers


@pytest.mark.asyncio
async def test_sem_instancia_ou_token_nao_ha_para_onde_enviar(log):
    zapi = zapi_com(sem_rede)
    await zapi.enviar_para("+558191234567", MensagemSaida("x", "y"), "", TOKEN)
    await zapi.enviar_para("+558191234567", MensagemSaida("x", "y"), INSTANCIA, "")

    assert log.nomes() == ["envio_sem_instancia", "envio_sem_instancia"]


@pytest.mark.asyncio
async def test_resposta_200_com_erro_e_falha(log):
    """A Z-API responde 200 com `error` em alguns casos; tratar como sucesso esconderia
    que a mensagem nao saiu."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "You are not connected"})

    await zapi_com(handler).enviar_para(
        "+558191234567", MensagemSaida("boas_vindas", "Olá!"), INSTANCIA, TOKEN
    )

    assert log.nomes() == ["envio_falhou"]
    _, _, campos = log.eventos[0]
    assert campos["zapi_erro"] == "You are not connected"


@pytest.mark.asyncio
async def test_falha_de_rede_nao_vaza_o_token(log):
    """O texto de um erro do httpx costuma trazer a URL — e a URL traz o token."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"sem conexao com {request.url}", request=request)

    await zapi_com(handler).enviar_para(
        "+558191234567", MensagemSaida("x", "y"), INSTANCIA, TOKEN
    )

    assert log.nomes() == ["envio_falhou"]
    assert TOKEN not in repr(log.eventos)


def test_detalhe_da_falha_vem_do_corpo_e_nunca_da_url():
    pedido = httpx.Request(
        "POST", f"https://api.z-api.io/instances/{INSTANCIA}/token/{TOKEN}/send-text"
    )
    resposta = httpx.Response(
        400, request=pedido, json={"error": "Invalid phone", "message": "detalhe"}
    )

    detalhe = falha_da_resposta(resposta)

    assert detalhe == {"http": 400, "zapi_erro": "Invalid phone", "zapi_mensagem": "detalhe"}
    assert TOKEN not in repr(detalhe)
    assert falha_da_resposta(httpx.Response(200, json={"messageId": "M1"})) is None


@pytest.mark.asyncio
async def test_apontar_webhooks_grava_o_endereco_em_todos():
    capturadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        capturadas.append(request)
        return httpx.Response(200, json={"value": True})

    resultado = await zapi_com(handler).apontar_webhooks(
        INSTANCIA, TOKEN, "https://marina.exemplo/webhooks/zapi/segredo"
    )

    (pedido,) = capturadas
    assert pedido.method == "PUT"
    assert pedido.url.path == f"/instances/{INSTANCIA}/token/{TOKEN}/update-every-webhooks"
    assert pedido.headers["Client-Token"] == CLIENT_TOKEN
    assert json.loads(pedido.content) == {
        "value": "https://marina.exemplo/webhooks/zapi/segredo",
        "notifySentByMe": False,
    }
    assert resultado == (200, {"value": True})
