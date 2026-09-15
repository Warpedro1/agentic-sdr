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
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request, Response
from sqlalchemy.dialects.postgresql import insert

from app.channels.whatsapp_meta import (
    MetaWhatsApp,
    blocos_de,
    id_da_mensagem,
    numero_id_de,
)
from app.channels.whatsapp_twilio import CANAL, TwilioWhatsApp
from app.composicao import Dependencias, montar_motor
from app.empresas import EmpresaCtx, por_numero, por_numero_id
from app.logging import obter_logger
from app.models import MensagemRecebida
from app.parsers import telefone_do_canal
from app.web.assinatura import url_do_webhook, valida, valida_meta

logger = obter_logger("webhook")

#: Status que a Twilio reporta quando a mensagem NAO chegou. Viram log de erro —
#: sem isso, mensagem perdida some sem ninguem ver.
_STATUS_RUINS = frozenset({"failed", "undelivered"})


def criar_app(
    deps: Dependencias,
    twilio: TwilioWhatsApp | None = None,
    meta: MetaWhatsApp | None = None,
) -> FastAPI:
    cliente = twilio or TwilioWhatsApp(deps.config)
    #: Os dois provedores convivem, e nao ha chave para escolher um: cada caminho
    #: de entrada responde pelo seu proprio. Quem chamar, atende — e a resposta sai
    #: por onde a mensagem entrou.
    cliente_meta = meta or MetaWhatsApp(deps.config)
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

        # So agora o corpo e confiavel, e so por isso o `To` pode decidir de qual
        # empresa e a mensagem. Resolver antes da assinatura deixaria qualquer um
        # escolher em nome de qual empresa criar lead.
        empresa = await _resolver_empresa(deps, form)
        if empresa is None:
            return Response(status_code=204)

        message_sid = form.get("MessageSid", "")
        if not await _registrar_recebida(deps, message_sid):
            logger.info("mensagem_repetida", message_sid=message_sid)
            return Response(status_code=204)

        tarefa = asyncio.create_task(_atender(deps, cliente, empresa, form))
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

    # --- Meta (Cloud API) ---------------------------------------------------

    @app.get("/webhooks/meta/whatsapp")
    async def verificar_meta(request: Request) -> Response:
        """O aperto de mao que a Meta exige antes de comecar a entregar.

        Ela chama uma unica vez, com um desafio, e so passa a mandar mensagem se a
        resposta for o desafio de volta em texto puro. O segredo comparado aqui nao
        protege as mensagens — quem faz isso e a assinatura no POST; este so prova
        que o endereco e nosso.
        """
        parametros = request.query_params
        if parametros.get("hub.mode") != "subscribe":
            raise HTTPException(status_code=400, detail="modo desconhecido")

        esperado = deps.config.meta_verify_token
        if not esperado or parametros.get("hub.verify_token") != esperado:
            logger.warning("verificacao_meta_recusada")
            raise HTTPException(status_code=403, detail="token de verificacao invalido")

        logger.info("verificacao_meta_aceita")
        return Response(content=parametros.get("hub.challenge", ""), media_type="text/plain")

    @app.post("/webhooks/meta/whatsapp")
    async def receber_meta(request: Request) -> Response:
        """Mesma ordem do caminho da Twilio, e pelos mesmos motivos.

        A diferenca esta em duas coisas. O corpo e lido em BYTES e conferido antes
        de virar JSON, porque a Meta assina o texto cru — reserializar mudaria o
        conteudo e derrubaria a assinatura. E uma unica chamada pode trazer varios
        blocos, inclusive misturando mensagem com aviso de entrega; cada um segue
        seu caminho, e nenhum deles derruba os outros.
        """
        corpo = await request.body()
        if not valida_meta(
            deps.config, corpo, request.headers.get("X-Hub-Signature-256")
        ):
            logger.warning("assinatura_invalida", origem="meta")
            raise HTTPException(status_code=403, detail="assinatura invalida")

        try:
            payload = json.loads(corpo.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("corpo_meta_ilegivel")
            return Response(status_code=204)

        for valor in blocos_de(payload):
            _registrar_status_meta(valor)

            # Bloco sem mensagem e aviso de entrega ou mudanca de conta: ja ficou
            # registrado acima, e nao ha ninguem para atender.
            message_sid = id_da_mensagem(valor)
            if not message_sid:
                continue

            empresa = await _empresa_do_numero_id(deps, numero_id_de(valor), message_sid)
            if empresa is None:
                continue

            if not await _registrar_recebida(deps, message_sid):
                logger.info("mensagem_repetida", message_sid=message_sid)
                continue

            # Traduzir — inclusive baixar anexo — fica para dentro do turno, como no
            # caminho da Twilio: reentrega e numero desconhecido nao gastam download.
            tarefa = asyncio.create_task(
                _atender_meta(deps, cliente_meta, empresa, valor)
            )
            em_voo.add(tarefa)
            tarefa.add_done_callback(em_voo.discard)

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


async def _resolver_empresa(
    deps: Dependencias, form: Mapping[str, str]
) -> EmpresaCtx | None:
    """De quem e esta mensagem, pelo numero que a recebeu.

    Numero desconhecido devolve None, e o chamador responde 204 sem gravar nada.
    Nunca erro, por tres motivos: quem chama e a operadora e nao uma pessoa, entao
    um 4xx so faz ela reentregar e encher o painel de alertas que ninguem consegue
    acionar; um 404 confirmaria a um sondador quais numeros estao configurados; e o
    caso e estado de CONFIGURACAO, nao falha — alguem apontou um numero para a
    Marina antes de cadastrar a empresa. O aviso no log e o que resolve isso.

    Sem cache: empresa recem-cadastrada precisa valer na proxima mensagem, e uma
    consulta indexada a mais e irrelevante perto do que um turno ja faz.
    """
    numero = form.get("To", "")
    async with deps.criar_sessao() as sessao:
        empresa = await por_numero(sessao, numero)
    if empresa is None:
        logger.warning(
            "empresa_desconhecida",
            numero=numero,
            message_sid=form.get("MessageSid"),
        )
    return empresa


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
    deps: Dependencias,
    cliente: TwilioWhatsApp,
    empresa: EmpresaCtx,
    form: Mapping[str, str],
) -> None:
    """Um turno da conversa desta empresa, fora da requisicao."""
    recebida = await cliente.receber_form(form)
    if not recebida.identidade:
        logger.warning("mensagem_sem_remetente", message_sid=recebida.message_sid)
        return

    # A resposta sai pelo numero da empresa, senao o lead recebe mensagem de um
    # numero para o qual nao escreveu.
    adaptador = cliente.adaptador(recebida.identidade, empresa.whatsapp_numero)
    motor = montar_motor(deps, adaptador, empresa)
    try:
        conversa_id = await motor.atender(recebida.identidade, recebida.entrada)
        logger.info(
            "mensagem_atendida",
            conversa_id=str(conversa_id),
            empresa=empresa.slug,
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


async def _empresa_do_numero_id(
    deps: Dependencias, numero_id: str, message_sid: str
) -> EmpresaCtx | None:
    """A empresa dona do numero que recebeu, no caminho da Meta.

    Mesmo desvio de `_resolver_empresa`: nao achou, ninguem e atendido e nada e
    gravado — nem a marca de idempotencia. Numero nao cadastrado e estado de
    CONFIGURACAO, nao falha, e responder erro so faria a Meta reentregar.
    """
    async with deps.criar_sessao() as sessao:
        empresa = await por_numero_id(sessao, numero_id)
    if empresa is None:
        logger.warning(
            "empresa_desconhecida", numero_id=numero_id, message_sid=message_sid
        )
    return empresa


def _registrar_status_meta(valor: Mapping[str, object]) -> None:
    """Avisos de entrega, que na Meta chegam pelo MESMO endereco das mensagens.

    Sem isto, mensagem que nao chega ao lead some sem ninguem ver — e o motivo real
    (fora da janela de 24h, numero que pediu para sair) so existe aqui.
    """
    situacoes = valor.get("statuses")
    if not isinstance(situacoes, list):
        return
    for situacao in situacoes:
        if not isinstance(situacao, dict):
            continue
        nome = str(situacao.get("status") or "").lower()
        erros = situacao.get("errors") or []
        registrar = logger.error if nome in _STATUS_RUINS or erros else logger.info
        registrar(
            "status_mensagem",
            message_sid=situacao.get("id"),
            status=nome,
            erro=erros or None,
            destino=situacao.get("recipient_id"),
        )


async def _atender_meta(
    deps: Dependencias,
    cliente: MetaWhatsApp,
    empresa: EmpresaCtx,
    valor: Mapping[str, object],
) -> None:
    """Um turno da conversa desta empresa, fora da requisicao. Gemeo de `_atender`."""
    recebida = await cliente.receber_valor(valor)
    if recebida is None:
        # Reacao, figurinha, localizacao: nada que responda a pergunta da Marina.
        return
    if not recebida.identidade:
        logger.warning("mensagem_sem_remetente", message_sid=recebida.message_sid)
        return

    # A resposta sai pelo numero da empresa, senao o lead recebe mensagem de um
    # numero para o qual nao escreveu. E vai para o celular COM o nono digito: a Meta
    # entrega o brasileiro sem ele, mas a lista de destinatarios do numero de teste
    # compara o texto literal, e ninguem cadastra o proprio numero sem o 9 — toda
    # resposta era recusada (131030). A Meta resolve as duas formas para a mesma
    # conta: o aviso de entrega volta com o `wa_id` sem o 9. A conversa continua
    # identificada pelo que o canal entregou.
    destino = telefone_do_canal(recebida.identidade) or recebida.identidade
    adaptador = cliente.adaptador(destino, empresa.whatsapp_numero_id or "")
    motor = montar_motor(deps, adaptador, empresa)
    try:
        conversa_id = await motor.atender(recebida.identidade, recebida.entrada)
        logger.info(
            "mensagem_atendida",
            conversa_id=str(conversa_id),
            empresa=empresa.slug,
            message_sid=recebida.message_sid,
            perfil=recebida.profile_name,
            origem="meta",
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
