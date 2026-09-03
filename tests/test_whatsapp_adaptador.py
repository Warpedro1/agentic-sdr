"""O adaptador de WhatsApp, contra uma Twilio de mentira.

Nenhum teste aqui precisa de banco nem de rede. O que se verifica e a traducao:
formulario do webhook -> InboundMessage, e MensagemSaida -> chamada HTTP.

A midia tem tratamento proprio porque e onde o canal mais difere do terminal: la
o adaptador olha o disco, aqui ele baixa um arquivo e precisa recusar o que a
Marina nao aceita ANTES de o armazenamento ver.
"""

from __future__ import annotations

import httpx
import pytest

from app.channels.base import MensagemSaida
from app.channels.whatsapp_twilio import TwilioWhatsApp, como_endereco, so_numero
from app.config import Configuracao

TELEFONE = "+5511987654321"

CONFIG = Configuracao(
    twilio_account_sid="ACtest",
    twilio_auth_token="segredo",
    twilio_whatsapp_from="+14155238886",
    twilio_url_publica="https://marina.exemplo/",
    juridico_ativo=False,
)


def form(**extras) -> dict[str, str]:
    base = {
        "MessageSid": "SM1",
        "From": f"whatsapp:{TELEFONE}",
        "To": "whatsapp:+14155238886",
        "Body": "",
        "NumMedia": "0",
    }
    base.update(extras)
    return base


def montar(handler, config: Configuracao = CONFIG) -> TwilioWhatsApp:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TwilioWhatsApp(config, http)


def sem_rede(_: httpx.Request) -> httpx.Response:
    raise AssertionError("nao deveria fazer chamada nenhuma")


# --- enderecos --------------------------------------------------------------


def test_prefixo_vai_e_volta():
    assert so_numero(f"whatsapp:{TELEFONE}") == TELEFONE
    assert so_numero(TELEFONE) == TELEFONE
    assert como_endereco(TELEFONE) == f"whatsapp:{TELEFONE}"
    assert como_endereco(f"whatsapp:{TELEFONE}") == f"whatsapp:{TELEFONE}"


# --- entrada ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_texto_vira_mensagem_com_a_identidade():
    twilio = montar(sem_rede)
    recebida = await twilio.receber_form(form(Body="Pedro Guerra", ProfileName="Pedro"))

    assert recebida.identidade == TELEFONE
    assert recebida.entrada.texto == "Pedro Guerra"
    assert recebida.entrada.anexo is None
    assert recebida.message_sid == "SM1"
    # O nome do perfil e registrado, mas a Marina pergunta o nome assim mesmo: o
    # cadastro e o que ela apurou, nao o que o WhatsApp mostra.
    assert recebida.profile_name == "Pedro"


@pytest.mark.asyncio
async def test_mensagem_vazia_nao_vira_texto_em_branco():
    twilio = montar(sem_rede)
    recebida = await twilio.receber_form(form(Body="   "))
    assert recebida.entrada.texto is None
    assert recebida.entrada.vazia


# --- midia ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_vira_anexo_baixado_no_disco():
    conteudo = b"%PDF-1.4 conteudo de mentira"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/Media/ME1")
        # A URL da Twilio exige a autenticacao da propria conta.
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, content=conteudo)

    twilio = montar(handler)
    recebida = await twilio.receber_form(
        form(
            NumMedia="1",
            MediaUrl0="https://api.twilio.com/Media/ME1",
            MediaContentType0="application/pdf",
        )
    )

    anexo = recebida.entrada.anexo
    assert anexo is not None
    assert anexo.mime == "application/pdf"
    assert anexo.tamanho_bytes == len(conteudo)
    # `origem` e um caminho local, e nao a URL: e o que mantem o
    # LocalDocumentStorage intacto, que valida extensao e le o arquivo.
    assert recebida.temporario is not None
    assert recebida.temporario.is_file()
    assert recebida.temporario.suffix == ".pdf"
    assert anexo.origem == str(recebida.temporario)

    recebida.temporario.unlink()


@pytest.mark.asyncio
async def test_tipo_nao_aceito_nao_baixa_nada():
    twilio = montar(sem_rede)
    recebida = await twilio.receber_form(
        form(
            NumMedia="1",
            MediaUrl0="https://api.twilio.com/Media/ME1",
            MediaContentType0="audio/ogg",
        )
    )
    # Audio nao esta em DOC_EXTENSOES. A recusa acontece antes do download.
    assert recebida.entrada.anexo is None
    assert recebida.temporario is None


@pytest.mark.asyncio
async def test_arquivo_acima_do_limite_e_abortado_no_meio():
    grande = b"x" * 5000

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=grande)

    twilio = montar(handler, CONFIG.model_copy(update={"doc_max_bytes": 1000}))
    recebida = await twilio.receber_form(
        form(
            NumMedia="1",
            MediaUrl0="https://api.twilio.com/Media/ME1",
            MediaContentType0="application/pdf",
        )
    )

    assert recebida.entrada.anexo is None
    assert recebida.temporario is None


@pytest.mark.asyncio
async def test_midia_que_nao_baixa_nao_derruba_a_mensagem():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    twilio = montar(handler)
    recebida = await twilio.receber_form(
        form(
            NumMedia="1",
            MediaUrl0="https://api.twilio.com/Media/ME1",
            MediaContentType0="image/png",
        )
    )
    assert recebida.entrada.anexo is None
    assert recebida.identidade == TELEFONE


# --- saida ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_envio_monta_a_chamada_da_api():
    chamadas: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/2010-04-01/Accounts/ACtest/Messages.json"
        chamadas.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(201, json={"sid": "SMresposta"})

    twilio = montar(handler)
    await twilio.adaptador(TELEFONE).enviar(
        MensagemSaida(template_key="boas_vindas", conteudo="Olá!")
    )

    assert len(chamadas) == 1
    enviado = chamadas[0]
    assert enviado["From"] == "whatsapp:+14155238886"
    assert enviado["To"] == f"whatsapp:{TELEFONE}"
    assert enviado["Body"] == "Olá!"
    # O status callback e o que transforma "nao chegou" em log, em vez de silencio.
    assert enviado["StatusCallback"] == (
        "https://marina.exemplo/webhooks/twilio/status"
    )


@pytest.mark.asyncio
async def test_falha_de_envio_nao_derruba_o_turno():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    twilio = montar(handler)
    # O estado ja foi persistido quando o envio acontece: estourar aqui deixaria
    # a conversa inconsistente. O erro vira log.
    await twilio.adaptador(TELEFONE).enviar(
        MensagemSaida(template_key="boas_vindas", conteudo="Olá!")
    )


@pytest.mark.asyncio
async def test_receber_nao_puxa_mensagem():
    twilio = montar(sem_rede)
    assert await twilio.adaptador(TELEFONE).receber() is None
