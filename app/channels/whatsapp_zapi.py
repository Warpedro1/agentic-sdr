"""Adaptador de WhatsApp pela Z-API.

Terceiro caminho, ao lado da Twilio e da Meta, com o mesmo papel: converter mundo,
sem regra de conversa. O que a Z-API tem de diferente, e que decide o desenho:

1. **Nao e a API oficial.** A instancia se conecta como um aparelho do WhatsApp Web,
   pelo QR code lido no celular do numero. O numero continua vivo no aplicativo, e
   por isso chegam aqui tambem mensagens que nao sao de um lead falando com a
   empresa: as que o proprio numero manda, as de grupo, as respostas a status.
   `motivo_para_ignorar` separa essas.

2. **O token da instancia vai NA URL** (`/instances/{id}/token/{token}/send-text`).
   Qualquer log que escreva a URL vaza a credencial — e o `str()` de um erro do
   httpx escreve. Por isso as falhas daqui sao registradas pelo status e pelo corpo
   da resposta, nunca pelo texto do erro. O log do proprio httpx e mascarado em
   `app/logging.py`.

3. **O webhook nao e assinado.** A prova de origem e um segredo no endereco,
   conferido na rota (`app/web/assinatura.py::valida_zapi`), e nao aqui.

4. **O remetente pode nao ter telefone.** Contato com numero oculto chega como
   `...@lid`. A identidade fica assim mesmo; sem telefone, a Marina pergunta.

5. **A midia vem com URL direta**, publica por 30 dias: uma chamada so, sem
   autenticacao — na Meta sao duas, com token.
"""

from __future__ import annotations

import mimetypes
import re
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from app.channels.base import Anexo, EntradaWhatsApp, InboundMessage, MensagemSaida
from app.channels.midia import MidiaGrandeDemais, baixar_com_limite
from app.config import Configuracao, obter_configuracao
from app.logging import obter_logger

logger = obter_logger("whatsapp_zapi")

CANAL = "whatsapp"

#: Tipos de midia que podem ser o contrato, e o campo onde cada um traz a URL.
#: `sticker` fica de fora de proposito: nao e documento, e tratar como anexo so
#: geraria recusa por extensao.
_URL_DA_MIDIA: dict[str, str] = {
    "document": "documentUrl",
    "image": "imageUrl",
    "audio": "audioUrl",
    "video": "videoUrl",
}

#: Marcadores de mensagem que NAO e um lead falando com a empresa, com o motivo que
#: vai para o log.
_MARCADORES_PARA_IGNORAR: tuple[tuple[str, str], ...] = (
    ("fromMe", "enviada pelo proprio numero"),
    ("isGroup", "grupo"),
    ("isNewsletter", "canal"),
    ("broadcast", "lista de transmissao"),
    ("isStatusReply", "resposta a status"),
    ("waitingMessage", "mensagem ainda nao decifrada"),
)


def identidade_de(phone: str) -> str:
    """`558191234567` -> `+558191234567`; `12345678901234@lid` fica como veio.

    O resto do sistema fala E.164 com o sinal. O `@lid` nao e telefone, e forcar um
    `+` nele o faria parecer um.
    """
    limpo = (phone or "").strip()
    if not limpo or "@" in limpo:
        return limpo
    digitos = re.sub(r"\D", "", limpo)
    return f"+{digitos}" if digitos else ""


def destino_de(identidade: str) -> str:
    """O `phone` que a Z-API espera no envio: so digitos, ou o `@lid` como veio."""
    limpo = (identidade or "").strip()
    return limpo if "@" in limpo else re.sub(r"\D", "", limpo)


def motivo_para_ignorar(payload: Mapping[str, Any]) -> str | None:
    """Por que esta mensagem nao deve ser atendida, ou None se deve.

    Pura e barata de proposito: roda na rota ANTES de qualquer consulta ao banco. O
    caso mais importante e `fromMe` — sem ele, cada resposta da Marina que o numero
    ecoasse de volta viraria uma mensagem nova, e a conversa responderia a si mesma.
    """
    for campo, motivo in _MARCADORES_PARA_IGNORAR:
        if payload.get(campo) is True:
            return motivo
    return None


class ZApiWhatsApp:
    """Conversa com a Z-API. Uma instancia por processo, servindo todas as empresas."""

    def __init__(
        self, config: Configuracao | None = None, http: httpx.AsyncClient | None = None
    ) -> None:
        self._config = config or obter_configuracao()
        self._http = http or httpx.AsyncClient(timeout=30.0)

    @property
    def _cabecalhos(self) -> dict[str, str]:
        """O token da CONTA, quando ha. Com ele ativo, chamada sem ele e recusada."""
        token = self._config.zapi_client_token.strip()
        return {"Client-Token": token} if token else {}

    def _url(self, instancia_id: str, token: str, rota: str) -> str:
        base = self._config.zapi_url_base.rstrip("/")
        return f"{base}/instances/{instancia_id}/token/{token}/{rota}"

    def adaptador(
        self, destino: str, instancia_id: str, token: str
    ) -> ZApiWhatsAppAdapter:
        """Um adaptador amarrado a quem responde, e por qual instancia."""
        return ZApiWhatsAppAdapter(self, destino, instancia_id, token)

    async def fechar(self) -> None:
        await self._http.aclose()

    # --- entrada ------------------------------------------------------------

    async def receber_payload(
        self, payload: Mapping[str, Any]
    ) -> EntradaWhatsApp | None:
        """Webhook `ReceivedCallback` -> mensagem tipada.

        Devolve None quando nao ha nada que responda a pergunta da Marina: reacao,
        figurinha, localizacao, contato. Passar adiante como mensagem vazia contaria
        como tentativa falha — o lead seria escalado por reagir com um emoji.
        """
        message_sid = str(payload.get("messageId") or "")
        texto = None
        bloco_texto = payload.get("text")
        if isinstance(bloco_texto, dict):
            texto = str(bloco_texto.get("message") or "").strip() or None

        tipo = next((t for t in _URL_DA_MIDIA if isinstance(payload.get(t), dict)), None)
        if texto is None and tipo is None:
            logger.info(
                "tipo_nao_tratado",
                tipo=[chave for chave, valor in payload.items() if isinstance(valor, dict)],
                message_sid=message_sid,
            )
            return None

        anexo, temporario = (
            await self._resolver_anexo(payload, tipo) if tipo else (None, None)
        )
        return EntradaWhatsApp(
            identidade=identidade_de(str(payload.get("phone") or "")),
            entrada=InboundMessage(texto=None if anexo else texto, anexo=anexo),
            message_sid=message_sid,
            destinatario=str(payload.get("instanceId") or ""),
            profile_name=payload.get("senderName") or payload.get("chatName") or None,
            temporario=temporario,
        )

    async def _resolver_anexo(
        self, payload: Mapping[str, Any], tipo: str
    ) -> tuple[Anexo | None, Path | None]:
        """Baixa a midia para um arquivo temporario, se houver uma aceitavel.

        Mesmo criterio dos outros provedores: baixar, e nao guardar a URL, e o que
        mantem o armazenamento local intacto. Midia recusada volta como (None, None)
        e vira log, nunca excecao — o turno nao pode cair por causa de um anexo.
        """
        bloco = payload.get(tipo)
        if not isinstance(bloco, dict):
            return None, None
        url = str(bloco.get(_URL_DA_MIDIA[tipo]) or "")
        if not url:
            logger.warning("midia_sem_url", message_sid=payload.get("messageId"))
            return None, None

        mime = str(bloco.get("mimeType") or "").split(";")[0].strip()
        extensao = mimetypes.guess_extension(mime) or ""
        if not extensao and bloco.get("fileName"):
            # Documento de tipo que o sistema nao reconhece pelo mime: o nome do
            # arquivo ainda diz a extensao.
            extensao = Path(str(bloco["fileName"])).suffix.lower()
        if extensao.lstrip(".").lower() not in self._config.extensoes_permitidas:
            logger.warning("midia_recusada", motivo="tipo nao aceito", tipo=mime)
            return None, None

        destino = Path(tempfile.gettempdir()) / f"marina-{uuid.uuid4().hex}{extensao}"
        try:
            tamanho = await baixar_com_limite(
                self._http, url, destino, self._config.doc_max_bytes
            )
        except MidiaGrandeDemais:
            logger.warning(
                "midia_recusada",
                motivo="acima do limite",
                limite=self._config.doc_max_bytes,
            )
            destino.unlink(missing_ok=True)
            return None, None
        except httpx.HTTPError as erro:
            logger.warning("midia_nao_baixada", erro=type(erro).__name__)
            destino.unlink(missing_ok=True)
            return None, None

        anexo = Anexo(
            nome_arquivo=destino.name,
            mime=mime or "application/octet-stream",
            tamanho_bytes=tamanho,
            origem=str(destino),
        )
        return anexo, destino

    # --- saida --------------------------------------------------------------

    async def enviar_para(
        self, destino: str, mensagem: MensagemSaida, instancia_id: str, token: str
    ) -> None:
        """Uma mensagem, uma chamada.

        Mesmo contrato dos outros provedores: o engine ja envia em sequencia, e
        falha aqui nao derruba o turno — so fica visivel, com o motivo.

        Resposta 200 nao basta: a Z-API devolve `{"error": ...}` em alguns casos,
        e isso tambem e falha.
        """
        if not instancia_id or not token:
            logger.error(
                "envio_sem_instancia",
                template_key=mensagem.template_key,
                destino=destino,
                falta="instancia" if not instancia_id else "token da instancia",
            )
            return

        corpo = {"phone": destino_de(destino), "message": mensagem.conteudo}
        try:
            resposta = await self._http.post(
                self._url(instancia_id, token, "send-text"),
                json=corpo,
                headers=self._cabecalhos,
            )
        except httpx.HTTPError as erro:
            # O NOME do erro, nunca o texto: o texto do httpx traz a URL, e a URL
            # traz o token da instancia.
            logger.error(
                "envio_falhou",
                template_key=mensagem.template_key,
                destino=destino,
                erro=type(erro).__name__,
            )
            return

        falha = falha_da_resposta(resposta)
        if falha is not None:
            logger.error(
                "envio_falhou",
                template_key=mensagem.template_key,
                destino=destino,
                **falha,
            )
            return
        logger.info(
            "mensagem_enviada",
            template_key=mensagem.template_key,
            message_sid=_corpo(resposta).get("messageId"),
        )

    # --- operacao -----------------------------------------------------------

    async def consultar(
        self, instancia_id: str, token: str, rota: str
    ) -> tuple[int, dict[str, Any]]:
        """GET de diagnostico (`status`, `device`): status HTTP e corpo."""
        resposta = await self._http.get(
            self._url(instancia_id, token, rota), headers=self._cabecalhos
        )
        return resposta.status_code, _corpo(resposta)

    async def apontar_webhooks(
        self, instancia_id: str, token: str, url: str
    ) -> tuple[int, dict[str, Any]]:
        """Grava `url` em todos os webhooks da instancia.

        `notifySentByMe` desligado: a Marina nao precisa saber do que o proprio
        numero envia, e cada eco seria uma chamada a mais para descartar.
        """
        resposta = await self._http.put(
            self._url(instancia_id, token, "update-every-webhooks"),
            json={"value": url, "notifySentByMe": False},
            headers=self._cabecalhos,
        )
        return resposta.status_code, _corpo(resposta)


def _corpo(resposta: httpx.Response) -> dict[str, Any]:
    try:
        corpo = resposta.json()
    except Exception:  # noqa: BLE001 - corpo nao-JSON e so ausencia de detalhe
        return {}
    return corpo if isinstance(corpo, dict) else {}


def falha_da_resposta(resposta: httpx.Response) -> dict[str, object] | None:
    """O motivo da falha, ou None se a Z-API aceitou.

    O detalhe e montado so com status e corpo — nunca com a URL, que carrega o token.
    """
    corpo = _corpo(resposta)
    if not resposta.is_error and not corpo.get("error"):
        return None
    detalhe: dict[str, object] = {"http": resposta.status_code}
    for origem, destino in (("error", "zapi_erro"), ("message", "zapi_mensagem")):
        if corpo.get(origem):
            detalhe[destino] = corpo[origem]
    return detalhe


class ZApiWhatsAppAdapter:
    """Adaptador de um turno: sabe para quem responder, e por qual instancia."""

    canal = CANAL

    def __init__(
        self, zapi: ZApiWhatsApp, destino: str, instancia_id: str, token: str
    ) -> None:
        self._zapi = zapi
        self._destino = destino
        #: A instancia da empresa que esta atendendo, e o token dela.
        self._instancia_id = instancia_id
        self._token = token

    async def receber(self) -> InboundMessage | None:
        """Canal de push: ninguem puxa mensagem daqui."""
        return None

    async def enviar(self, mensagem: MensagemSaida) -> None:
        await self._zapi.enviar_para(
            self._destino, mensagem, self._instancia_id, self._token
        )
