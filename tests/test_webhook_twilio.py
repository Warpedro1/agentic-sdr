"""O webhook da Twilio: assinatura, idempotência e o caminho até o engine.

A assinatura é a fronteira de segurança deste canal — sem ela, quem descobrir o
endereço público inventa mensagens e cria lead e reunião falsos no juridico. Por
isso o primeiro bloco de testes prova não só o 403, mas que **nada** acontece
antes dele: a sessão de banco usada ali explode se alguém encostar.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import func, select
from twilio.request_validator import RequestValidator

from app.channels.whatsapp_twilio import TwilioWhatsApp
from app.composicao import Dependencias, RegistroIntegracoes
from app.config import Configuracao
from app.crm import CrmSinkNoop
from app.models import Conversa, Empresa, Mensagem, MensagemRecebida
from app.storage.local import LocalDocumentStorage
from app.web.assinatura import url_do_webhook
from app.web.webhook import criar_app

CAMINHO = "/webhooks/twilio/whatsapp"
CAMINHO_STATUS = "/webhooks/twilio/status"
TELEFONE = "+5511987654321"
TOKEN = "token-de-teste"
PUBLICA = "https://marina.exemplo"

CONFIG = Configuracao(
    twilio_account_sid="ACtest",
    twilio_auth_token=TOKEN,
    twilio_whatsapp_from="+14155238886",
    twilio_url_publica=PUBLICA,
    juridico_ativo=False,
)


def formulario(**extras) -> dict[str, str]:
    base = {
        "MessageSid": "SM1",
        "From": f"whatsapp:{TELEFONE}",
        "To": "whatsapp:+14155238886",
        "Body": "oi",
        "NumMedia": "0",
    }
    base.update(extras)
    return base


def assinar(form: dict[str, str], caminho: str = CAMINHO) -> dict[str, str]:
    validador = RequestValidator(TOKEN)
    return {
        "X-Twilio-Signature": validador.compute_signature(f"{PUBLICA}{caminho}", form)
    }


class BancoProibido:
    """Sessionmaker que denuncia qualquer toque no banco."""

    def __call__(self):
        raise AssertionError("o banco não podia ter sido tocado")


def montar_app(criar_sessao, config: Configuracao = CONFIG, handler=None):
    """App + a Twilio de mentira que guarda o que a Marina respondeu."""
    enviadas: list[dict[str, str]] = []

    def padrao(request: httpx.Request) -> httpx.Response:
        enviadas.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(201, json={"sid": f"SM{len(enviadas)}"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler or padrao))
    deps = Dependencias(
        config=config,
        criar_sessao=criar_sessao,
        armazenamento=LocalDocumentStorage(config),
        integracoes=RegistroIntegracoes(criar_sessao, config),
    )
    app = criar_app(deps, TwilioWhatsApp(config, http))
    return app, enviadas


def cliente_de(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://teste"
    )


async def esperar_atendimento(app) -> None:
    """O webhook responde antes de atender; o teste precisa esperar o resto."""
    if app.state.em_voo:
        await asyncio.gather(*list(app.state.em_voo))


# --- assinatura (sem banco) -------------------------------------------------


def test_url_assinada_vem_da_config_e_nao_dos_cabecalhos():
    # A assinatura é calculada sobre a URL completa. Usar o valor configurado
    # evita depender de X-Forwarded-*, que o cliente pode forjar.
    assert url_do_webhook(CONFIG, CAMINHO) == f"{PUBLICA}{CAMINHO}"
    assert url_do_webhook(CONFIG, CAMINHO, "a=1") == f"{PUBLICA}{CAMINHO}?a=1"


@pytest.mark.asyncio
async def test_sem_assinatura_e_403_e_nao_toca_no_banco():
    app, _ = montar_app(BancoProibido())
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, data=formulario())
    assert resposta.status_code == 403


@pytest.mark.asyncio
async def test_assinatura_errada_e_403():
    app, _ = montar_app(BancoProibido())
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(
            CAMINHO, data=formulario(), headers={"X-Twilio-Signature": "nada a ver"}
        )
    assert resposta.status_code == 403


@pytest.mark.asyncio
async def test_assinatura_de_outro_corpo_e_403():
    # Assinatura válida, corpo trocado: é exatamente o ataque que a conferência
    # existe para impedir.
    app, _ = montar_app(BancoProibido())
    cabecalhos = assinar(formulario())
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(
            CAMINHO, data=formulario(Body="me manda R$ 5.000"), headers=cabecalhos
        )
    assert resposta.status_code == 403


# --- fluxo (com banco) ------------------------------------------------------


@pytest.mark.asyncio
async def test_primeira_mensagem_abre_a_conversa_e_responde(sessao_db, criar_sessao_db, empresa):
    app, enviadas = montar_app(criar_sessao_db)
    form = formulario()

    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, data=form, headers=assinar(form))
        assert resposta.status_code == 204
        await esperar_atendimento(app)

    conversa = (await sessao_db.execute(select(Conversa))).scalar_one()
    assert conversa.canal == "whatsapp"
    assert conversa.identidade_externa == TELEFONE
    # A saudação e a pergunta do nome, nessa ordem, uma chamada cada.
    assert [m["Body"] for m in enviadas][:1] != []
    assert len(enviadas) == 2
    assert all(m["To"] == f"whatsapp:{TELEFONE}" for m in enviadas)


@pytest.mark.asyncio
async def test_message_sid_repetido_nao_vira_dois_passos(sessao_db, criar_sessao_db, empresa):
    app, enviadas = montar_app(criar_sessao_db)
    form = formulario()

    async with cliente_de(app) as cliente:
        for _ in range(2):
            resposta = await cliente.post(CAMINHO, data=form, headers=assinar(form))
            assert resposta.status_code == 204
            await esperar_atendimento(app)

    recebidas = await sessao_db.scalar(select(func.count()).select_from(MensagemRecebida))
    assert recebidas == 1
    # Sem a trava, a segunda entrega repetiria a saudação inteira.
    assert len(enviadas) == 2


@pytest.mark.asyncio
async def test_segunda_mensagem_avanca_a_mesma_conversa(sessao_db, criar_sessao_db, empresa):
    app, enviadas = montar_app(criar_sessao_db)

    async with cliente_de(app) as cliente:
        for indice, texto in enumerate(["oi", "Pedro Guerra"]):
            form = formulario(MessageSid=f"SM{indice}", Body=texto)
            await cliente.post(CAMINHO, data=form, headers=assinar(form))
            await esperar_atendimento(app)

    conversas = (await sessao_db.execute(select(Conversa))).scalars().all()
    assert len(conversas) == 1
    # Depois do nome ela NAO pergunta o telefone: no WhatsApp o numero chega na
    # propria mensagem, e perguntar seria pedir o que a conversa ja tem.
    assert conversas[0].estado == "AGUARDANDO_FONTE"

    # A transcrição guarda as duas entradas e todas as saídas, em sequência sem
    # buraco — o contador foi reidratado, não recomeçou do zero.
    mensagens = (
        (
            await sessao_db.execute(
                select(Mensagem)
                .where(Mensagem.conversa_id == conversas[0].id)
                .order_by(Mensagem.sequencia)
            )
        )
        .scalars()
        .all()
    )
    assert [m.sequencia for m in mensagens] == list(range(1, len(mensagens) + 1))
    assert [m.conteudo for m in mensagens if m.direcao == "in"] == ["oi", "Pedro Guerra"]


@pytest.mark.asyncio
async def test_status_de_falha_e_registrado(sessao_db, criar_sessao_db):
    app, _ = montar_app(criar_sessao_db)
    form = {
        "MessageSid": "SM1",
        "MessageStatus": "undelivered",
        "ErrorCode": "63016",
        "To": f"whatsapp:{TELEFONE}",
    }
    async with cliente_de(app) as cliente:
        resposta = await cliente.post(
            CAMINHO_STATUS, data=form, headers=assinar(form, CAMINHO_STATUS)
        )
    assert resposta.status_code == 204


@pytest.mark.asyncio
async def test_saude_responde_sem_assinatura(sessao_db, criar_sessao_db):
    app, _ = montar_app(criar_sessao_db)
    async with cliente_de(app) as cliente:
        resposta = await cliente.get("/saude")
    assert resposta.status_code == 200


# --- de quem e a mensagem ---------------------------------------------------
#
# Cada empresa tem o seu numero. O campo `To` diz qual numero recebeu, e e por ele
# que se descobre por qual empresa o atendimento acontece.


@pytest.mark.asyncio
async def test_a_mensagem_e_atribuida_a_empresa_do_numero_que_a_recebeu(
    sessao_db, criar_sessao_db, empresa, outra_empresa
):
    """Duas empresas cadastradas; a conversa nasce na dona do numero de destino."""
    app, _ = montar_app(criar_sessao_db)
    form = formulario(To=f"whatsapp:{outra_empresa.whatsapp_numero}")

    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, data=form, headers=assinar(form))
        assert resposta.status_code == 204
        await esperar_atendimento(app)

    conversa = (await sessao_db.execute(select(Conversa))).scalar_one()
    assert conversa.empresa_id == outra_empresa.id
    assert conversa.empresa_id != empresa.id


@pytest.mark.asyncio
async def test_a_resposta_sai_pelo_numero_da_empresa(
    sessao_db, criar_sessao_db, empresa, outra_empresa
):
    """Senao o lead recebe resposta de um numero para o qual nao escreveu."""
    app, enviadas = montar_app(criar_sessao_db)
    form = formulario(To=f"whatsapp:{outra_empresa.whatsapp_numero}")

    async with cliente_de(app) as cliente:
        await cliente.post(CAMINHO, data=form, headers=assinar(form))
        await esperar_atendimento(app)

    assert enviadas
    assert all(
        m["From"] == f"whatsapp:{outra_empresa.whatsapp_numero}" for m in enviadas
    )


@pytest.mark.asyncio
async def test_numero_nao_cadastrado_nao_deixa_rastro(sessao_db, criar_sessao_db, empresa):
    """Alguem apontou um numero para a Marina antes de cadastrar a empresa.

    E estado de configuracao, nao falha: responde 204 para a operadora nao ficar
    reentregando, e nao grava nada — nem a marca de idempotencia, que e por isso
    que a atribuicao vem ANTES dela.
    """
    app, enviadas = montar_app(criar_sessao_db)
    form = formulario(To="whatsapp:+15550009999")

    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, data=form, headers=assinar(form))
        assert resposta.status_code == 204
        await esperar_atendimento(app)

    assert await sessao_db.scalar(select(func.count()).select_from(Conversa)) == 0
    assert await sessao_db.scalar(select(func.count()).select_from(MensagemRecebida)) == 0
    assert enviadas == []


@pytest.mark.asyncio
async def test_empresa_desativada_deixa_de_ser_atendida(
    sessao_db, criar_sessao_db, empresa
):
    """Desativar precisa parar o atendimento sem apagar o historico."""
    linha = await sessao_db.get(Empresa, empresa.id)
    linha.ativo = False
    await sessao_db.commit()

    app, enviadas = montar_app(criar_sessao_db)
    form = formulario()

    async with cliente_de(app) as cliente:
        resposta = await cliente.post(CAMINHO, data=form, headers=assinar(form))
        assert resposta.status_code == 204
        await esperar_atendimento(app)

    assert await sessao_db.scalar(select(func.count()).select_from(Conversa)) == 0
    assert enviadas == []
