"""O adaptador da Meta e a assinatura dela, sem banco e sem rede.

A Meta entrega um formato bem diferente do da Twilio — JSON aninhado, telefone sem
o `+`, midia em duas chamadas —, e o adaptador existe para que nada disso passe
daqui. Estes testes provam a traducao nas duas direcoes e a primeira porta do
caminho: a assinatura.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from app.channels.base import MensagemSaida
from app.channels.whatsapp_meta import (
    MetaWhatsApp,
    _detalhe_meta,
    blocos_de,
    com_mais,
    id_da_mensagem,
    numero_id_de,
)
from app.config import Configuracao
from app.web.assinatura import valida_meta

SEGREDO = "segredo-do-app"
TOKEN = "token-meta"
NUMERO_ID = "1278570248676916"
#: Como a Meta entrega um celular brasileiro de DDD alto: sem `+` e sem o nono digito.
DE = "558191234567"

CONFIG = Configuracao(
    meta_access_token=TOKEN,
    meta_app_secret=SEGREDO,
    meta_verify_token="verificar",
    meta_api_versao="v25.0",
    meta_validar_assinatura=True,
)


def valor_de_texto(texto: str = "oi", wamid: str = "wamid.1") -> dict:
    return {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15556715798", "phone_number_id": NUMERO_ID},
        "contacts": [{"profile": {"name": "Pedro"}, "wa_id": DE}],
        "messages": [
            {
                "from": DE,
                "id": wamid,
                "timestamp": "1757590000",
                "type": "text",
                "text": {"body": texto},
            }
        ],
    }


def sem_rede(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"nao devia chamar a rede: {request.url}")


def meta_com(handler) -> MetaWhatsApp:
    return MetaWhatsApp(CONFIG, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def assinar(corpo: bytes, segredo: str = SEGREDO) -> str:
    return "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()


# --- formato ----------------------------------------------------------------


def test_telefone_da_meta_ganha_o_mais():
    assert com_mais(DE) == "+558191234567"
    assert com_mais("+558191234567") == "+558191234567"
    assert com_mais("") == ""


def test_todos_os_blocos_sao_percorridos_e_lixo_e_ignorado():
    """A Meta agrupa varias entradas numa chamada. Tratar como singular perderia
    mensagem em silencio no dia em que vierem duas."""
    payload = {
        "entry": [
            {"changes": [{"value": {"a": 1}}, {"value": {"b": 2}}]},
            "lixo",
            {"changes": [{"value": {"c": 3}}, {"sem_value": True}]},
        ]
    }
    assert blocos_de(payload) == [{"a": 1}, {"b": 2}, {"c": 3}]
    assert blocos_de({}) == []


def test_o_numero_que_recebeu_vem_dos_metadados():
    assert numero_id_de(valor_de_texto()) == NUMERO_ID
    assert numero_id_de({}) == ""


def test_o_id_da_mensagem_e_lido_sem_traduzir():
    assert id_da_mensagem(valor_de_texto(wamid="wamid.xyz")) == "wamid.xyz"
    assert id_da_mensagem({"statuses": [{"id": "wamid.saida"}]}) == ""


# --- entrada ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_mensagem_de_texto_vira_entrada_tipada():
    recebida = await meta_com(sem_rede).receber_valor(valor_de_texto("Pedro Guerra"))

    assert recebida is not None
    assert recebida.identidade == "+558191234567"
    assert recebida.entrada.texto == "Pedro Guerra"
    assert recebida.entrada.anexo is None
    assert recebida.message_sid == "wamid.1"
    assert recebida.destinatario == NUMERO_ID
    assert recebida.profile_name == "Pedro"


@pytest.mark.asyncio
async def test_aviso_de_entrega_nao_e_mensagem_de_lead():
    valor = {
        "metadata": {"phone_number_id": NUMERO_ID},
        "statuses": [{"id": "wamid.x", "status": "delivered", "recipient_id": DE}],
    }
    assert await meta_com(sem_rede).receber_valor(valor) is None


@pytest.mark.asyncio
async def test_reacao_nao_queima_tentativa_do_lead():
    """Um emoji de reacao nao responde pergunta nenhuma.

    Se passasse como mensagem vazia, a maquina contaria como tentativa falha — e o
    lead seria escalado para humano por reagir tres vezes.
    """
    valor = valor_de_texto()
    valor["messages"][0] = {
        "from": DE,
        "id": "wamid.2",
        "type": "reaction",
        "reaction": {"emoji": "👍"},
    }
    assert await meta_com(sem_rede).receber_valor(valor) is None


@pytest.mark.asyncio
async def test_documento_e_baixado_em_duas_chamadas():
    chamadas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request.url.path)
        # A URL de download tambem exige o token: e temporaria, mas nao publica.
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.url.path == "/v25.0/midia-123":
            return httpx.Response(200, json={"url": "https://lookaside.exemplo/arquivo"})
        return httpx.Response(200, content=b"%PDF-1.4 contrato")

    valor = valor_de_texto()
    valor["messages"][0] = {
        "from": DE,
        "id": "wamid.3",
        "type": "document",
        "document": {"id": "midia-123", "mime_type": "application/pdf"},
    }
    recebida = await meta_com(handler).receber_valor(valor)

    assert recebida is not None
    try:
        anexo = recebida.entrada.anexo
        assert anexo is not None
        assert anexo.mime == "application/pdf"
        assert anexo.nome_arquivo.endswith(".pdf")
        assert anexo.tamanho_bytes == len(b"%PDF-1.4 contrato")
        assert recebida.temporario is not None and recebida.temporario.is_file()
        assert chamadas == ["/v25.0/midia-123", "/arquivo"]
    finally:
        if recebida.temporario is not None:
            recebida.temporario.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_midia_de_tipo_nao_aceito_nem_chega_a_ser_baixada():
    valor = valor_de_texto()
    valor["messages"][0] = {
        "from": DE,
        "id": "wamid.4",
        "type": "image",
        "image": {"id": "midia-9", "mime_type": "image/gif"},
    }
    recebida = await meta_com(sem_rede).receber_valor(valor)

    assert recebida is not None
    assert recebida.entrada.anexo is None


# --- saida ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_envio_monta_a_chamada_da_graph_api():
    capturadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        capturadas.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.saida"}]})

    await meta_com(handler).enviar_para(
        "+558191234567", MensagemSaida("boas_vindas", "Olá!"), NUMERO_ID
    )

    (pedido,) = capturadas
    assert pedido.method == "POST"
    # O envio e enderecado pelo id do numero, nao pelo numero.
    assert pedido.url.path == f"/v25.0/{NUMERO_ID}/messages"
    assert pedido.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(pedido.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "+558191234567",
        "type": "text",
        "text": {"preview_url": False, "body": "Olá!"},
    }


@pytest.mark.asyncio
async def test_sem_id_do_numero_nao_ha_para_onde_enviar():
    await meta_com(sem_rede).enviar_para("+558191234567", MensagemSaida("x", "y"), "")


@pytest.mark.asyncio
async def test_falha_de_envio_nao_derruba_o_turno():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "falhou", "code": 131047}})

    await meta_com(handler).enviar_para(
        "+558191234567", MensagemSaida("x", "y"), NUMERO_ID
    )


def test_o_motivo_da_meta_chega_ao_log():
    """O texto do httpx diz so '400 Bad Request'. Quem explica e o corpo."""
    pedido = httpx.Request("POST", "https://graph.facebook.com/v25.0/1/messages")
    resposta = httpx.Response(
        400,
        request=pedido,
        json={
            "error": {
                "message": "Re-engagement message",
                "code": 131047,
                "error_subcode": 2494010,
                "error_data": {"details": "More than 24 hours have passed"},
            }
        },
    )
    erro = httpx.HTTPStatusError("400", request=pedido, response=resposta)

    assert _detalhe_meta(erro) == {
        "http": 400,
        "codigo": 131047,
        "meta_mensagem": "Re-engagement message",
        "subcodigo": 2494010,
        "meta_detalhe": "More than 24 hours have passed",
    }


def test_falha_sem_resposta_nao_estoura():
    assert _detalhe_meta(httpx.ConnectError("recusada")) == {}


# --- assinatura -------------------------------------------------------------


def test_assinatura_da_meta_confere_o_corpo_cru():
    corpo = json.dumps({"entry": []}).encode()
    assert valida_meta(CONFIG, corpo, assinar(corpo))


def test_corpo_trocado_derruba_a_assinatura():
    """O ataque que a assinatura existe para impedir: aproveitar uma assinatura
    valida com outro conteudo."""
    original = b'{"texto": "oi"}'
    trocado = b'{"texto": "me agenda amanha"}'
    assert not valida_meta(CONFIG, trocado, assinar(original))


def test_sem_cabecalho_e_recusado():
    assert not valida_meta(CONFIG, b"{}", None)


def test_sem_chave_do_app_recusa_ate_assinatura_calculada_sem_chave():
    """Sem a chave, aceitar seria pior do que recusar: HMAC com chave vazia
    qualquer um calcula."""
    config = Configuracao(meta_app_secret="", meta_validar_assinatura=True)
    corpo = b"{}"
    assert not valida_meta(config, corpo, assinar(corpo, segredo=""))
