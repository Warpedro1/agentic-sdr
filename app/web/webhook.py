"""As rotas que a Twilio chama.

Tres coisas acontecem aqui, nesta ordem, e nenhuma delas e negociavel:

1. **Assinatura.** Chamada sem assinatura valida e 403 e nada mais.
2. **Idempotencia.** O mesmo `MessageSid` nao vira dois passos da conversa.
3. **Resposta rapida.** O atendimento roda FORA da requisicao e a rota devolve
   204 vazio. Nao devolvemos TwiML: quem envia e a REST API, que e o caminho que
   permite mandar varias mensagens em ordem e registrar cada uma na transcricao.

Nenhuma regra de conversa mora aqui — isso e do engine, que e chamado por
`atender` uma vez por mensagem.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request, Response
from sqlalchemy.dialects.postgresql import insert

from app.channels.whatsapp_twilio import CANAL, TwilioWhatsApp
from app.composicao import Dependencias, montar_motor
from app.logging import obter_logger
from app.models import MensagemRecebida
from app.web.assinatura import url_do_webhook, valida

logger = obter_logger("webhook")

#: Status que a Twilio reporta quando a mensagem NAO chegou. Viram log de erro —
#: sem isso, mensagem perdida some sem ninguem ver.
_STATUS_RUINS = frozenset({"failed", "undelivered"})


def criar_app(deps: Dependencias, twilio: TwilioWhatsApp | None = None) -> FastAPI:
    cliente = twilio or TwilioWhatsApp(deps.config)
    #: Referencia forte para as tarefas em voo: sem isto o coletor de lixo pode
    #: recolher a tarefa no meio do atendimento.
    em_voo: set[asyncio.Task] = set()

    @asynccontextmanager
    async def ciclo(_: FastAPI) -> AsyncIterator[None]:
        yield
        # No encerramento, o que ja comecou tem a chance de terminar: no meio de
        # um atendimento ha estado gravado e mensagem por enviar.
        if em_voo:
            await asyncio.gather(*list(em_voo), return_exceptions=True)

    app = FastAPI(title="Marina — WhatsApp", lifespan=ciclo)
    #: Exposto para o teste poder esperar o atendimento terminar — e para quem
    #: estiver depurando saber quantos turnos estao correndo.
    app.state.em_voo = em_voo

    @app.get("/saude")
    async def saude() -> dict[str, str]:
        """Confere o tunel antes de culpar a Twilio."""
        return {"status": "ok"}

    @app.post("/webhooks/twilio/whatsapp")
    async def receber(request: Request) -> Response:
        form = await _formulario(request)
        _conferir_assinatura(deps, request, form)

        message_sid = form.get("MessageSid", "")
        if not await _registrar_recebida(deps, message_sid):
            logger.info("mensagem_repetida", message_sid=message_sid)
            return Response(status_code=204)

        tarefa = asyncio.create_task(_atender(deps, cliente, form))
        em_voo.add(tarefa)
        tarefa.add_done_callback(em_voo.discard)
        return Response(status_code=204)

    @app.post("/webhooks/twilio/status")
    async def status(request: Request) -> Response:
        form = await _formulario(request)
        _conferir_assinatura(deps, request, form)

        situacao = (form.get("MessageStatus") or "").lower()
        registrar = logger.error if situacao in _STATUS_RUINS else logger.info
        registrar(
            "status_mensagem",
            message_sid=form.get("MessageSid"),
            status=situacao,
            erro=form.get("ErrorCode"),
            destino=form.get("To"),
        )
        return Response(status_code=204)

    return app


async def _formulario(request: Request) -> dict[str, str]:
    """Corpo -> dicionario de parametros.

    A Twilio manda `application/x-www-form-urlencoded`, que se le com a
    biblioteca padrao. `request.form()` do Starlette exigiria `python-multipart`
    — uma dependencia a mais para tratar um multipart que nunca chega aqui.

    `keep_blank_values` importa: campo vazio faz parte do que foi assinado, e
    descarta-lo faria a assinatura nunca bater.
    """
    corpo = (await request.body()).decode("utf-8")
    return dict(parse_qsl(corpo, keep_blank_values=True))


def _conferir_assinatura(
    deps: Dependencias, request: Request, form: Mapping[str, str]
) -> None:
    url = url_do_webhook(deps.config, request.url.path, request.url.query)
    if not valida(deps.config, url, form, request.headers.get("X-Twilio-Signature")):
        logger.warning("assinatura_invalida", url=url)
        raise HTTPException(status_code=403, detail="assinatura invalida")


async def _registrar_recebida(deps: Dependencias, message_sid: str) -> bool:
    """Marca a mensagem como vista. False quando ja tinha sido.

    A trava e do banco (UNIQUE em canal + id externo) porque precisa sobreviver a
    restart e valer entre workers.
    """
    if not message_sid:
        # Sem id nao ha como deduplicar. Processa e deixa registrado.
        logger.warning("mensagem_sem_sid")
        return True

    comando = (
        insert(MensagemRecebida.__table__)
        .values(id=uuid.uuid4(), canal=CANAL, id_externo=message_sid)
        .on_conflict_do_nothing(constraint="uq_mensagem_recebida_externa")
        # RETURNING, e nao `rowcount`: com ON CONFLICT DO NOTHING o conflito nao
        # devolve linha nenhuma, e essa e a leitura confiavel de "ja tinha".
        .returning(MensagemRecebida.__table__.c.id)
    )
    async with deps.criar_sessao() as sessao:
        inserida = (await sessao.execute(comando)).scalar_one_or_none()
        await sessao.commit()
    return inserida is not None


async def _atender(
    deps: Dependencias, cliente: TwilioWhatsApp, form: Mapping[str, str]
) -> None:
    """Um turno da conversa, fora da requisicao."""
    recebida = await cliente.receber_form(form)
    if not recebida.identidade:
        logger.warning("mensagem_sem_remetente", message_sid=recebida.message_sid)
        return

    motor = montar_motor(deps, cliente.adaptador(recebida.identidade))
    try:
        conversa_id = await motor.atender(recebida.identidade, recebida.entrada)
        logger.info(
            "mensagem_atendida",
            conversa_id=str(conversa_id),
            message_sid=recebida.message_sid,
            perfil=recebida.profile_name,
        )
    except Exception as erro:  # noqa: BLE001 - a rota ja respondeu; so registrar
        logger.error(
            "atendimento_falhou",
            message_sid=recebida.message_sid,
            erro=str(erro),
            exc_info=True,
        )
    finally:
        if recebida.temporario is not None:
            # O armazenamento ja copiou o que precisava.
            recebida.temporario.unlink(missing_ok=True)
