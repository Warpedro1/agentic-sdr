"""Adaptador de WhatsApp pela Twilio.

Aqui — e so aqui — o formulario que a Twilio manda vira `InboundMessage`, e uma
`MensagemSaida` vira uma chamada HTTP. Nenhuma regra de conversa mora neste
arquivo: ele converte mundo, como o `TerminalAdapter` faz com o disco.

Duas peculiaridades deste canal:

1. **Ninguem puxa mensagem.** `receber()` existe so para satisfazer o Protocol e
   devolve None. Quem dirige e o webhook, chamando `Engine.atender`.

2. **A resposta precisa de destinatario.** `ChannelAdapter.enviar` nao carrega um,
   e inventar um parametro no Protocol contaminaria o terminal. Por isso o
   adaptador e amarrado ao destino e criado POR TURNO — barato, porque o cliente
   HTTP e a configuracao ficam no `TwilioWhatsApp`, que nasce uma vez no boot.
"""

from __future__ import annotations

import mimetypes
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.channels.base import Anexo, EntradaWhatsApp, InboundMessage, MensagemSaida
from app.channels.midia import MidiaGrandeDemais, baixar_com_limite
from app.config import Configuracao, obter_configuracao
from app.logging import obter_logger

logger = obter_logger("whatsapp")

def _detalhe_twilio(erro: Exception) -> dict[str, object]:
    """O que a Twilio respondeu, quando ela respondeu alguma coisa.

    Devolve dicionario vazio para falha sem resposta (conexao recusada, tempo
    esgotado) e para corpo que nao seja o JSON de erro esperado — aqui nada pode
    estourar, porque este codigo roda dentro do tratamento de OUTRO erro.
    """
    resposta = getattr(erro, "response", None)
    if resposta is None:
        return {}
    detalhe: dict[str, object] = {"http": resposta.status_code}
    try:
        corpo = resposta.json()
    except Exception:  # noqa: BLE001 - corpo nao-JSON e so ausencia de detalhe
        return detalhe
    if isinstance(corpo, dict):
        for origem, destino in (("code", "codigo"), ("message", "twilio_mensagem")):
            if corpo.get(origem) is not None:
                detalhe[destino] = corpo[origem]
    return detalhe


CANAL = "whatsapp"

_PREFIXO = "whatsapp:"
_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def so_numero(endereco: str) -> str:
    """`whatsapp:+5511999999999` -> `+5511999999999`."""
    limpo = (endereco or "").strip()
    return limpo[len(_PREFIXO) :] if limpo.startswith(_PREFIXO) else limpo


def como_endereco(numero: str) -> str:
    """`+5511999999999` -> `whatsapp:+5511999999999`."""
    limpo = (numero or "").strip()
    return limpo if limpo.startswith(_PREFIXO) else f"{_PREFIXO}{limpo}"


class TwilioWhatsApp:
    """Conversa com a API da Twilio. Uma instancia por processo."""

    def __init__(
        self, config: Configuracao | None = None, http: httpx.AsyncClient | None = None
    ) -> None:
        self._config = config or obter_configuracao()
        self._http = http or httpx.AsyncClient(timeout=30.0)

    @property
    def _auth(self) -> tuple[str, str]:
        return (self._config.twilio_account_sid, self._config.twilio_auth_token)

    def adaptador(
        self, destino: str, remetente: str | None = None
    ) -> TwilioWhatsAppAdapter:
        """Um adaptador amarrado a quem vai receber a resposta, e por qual numero."""
        return TwilioWhatsAppAdapter(self, destino, remetente)

    async def fechar(self) -> None:
        await self._http.aclose()

    # --- entrada ------------------------------------------------------------

    async def receber_form(self, form: Mapping[str, str]) -> EntradaWhatsApp:
        """Formulario do webhook -> mensagem tipada."""
        identidade = so_numero(form.get("From", ""))
        texto = (form.get("Body") or "").strip() or None
        anexo, temporario = await self._resolver_anexo(form)
        return EntradaWhatsApp(
            identidade=identidade,
            entrada=InboundMessage(texto=None if anexo else texto, anexo=anexo),
            message_sid=form.get("MessageSid", ""),
            destinatario=so_numero(form.get("To", "")),
            profile_name=form.get("ProfileName") or None,
            temporario=temporario,
        )

    async def _resolver_anexo(
        self, form: Mapping[str, str]
    ) -> tuple[Anexo | None, Path | None]:
        """Baixa a midia para um arquivo temporario, se houver uma aceitavel.

        Baixar aqui, e nao guardar a URL, e o que mantem `LocalDocumentStorage`
        intacto: ele valida pela extensao e faz `Path(anexo.origem).is_file()`.
        E e coerente com a divisao do projeto — quem resolve o mundo e o
        adaptador; a maquina so pergunta se ha anexo.

        Midia recusada volta como (None, None) e vira log: a copy que a maquina
        tem para anexo nao resolvido fala de caminho e de Explorer, que no
        WhatsApp nao quer dizer nada.
        """
        try:
            quantas = int(form.get("NumMedia") or 0)
        except ValueError:
            quantas = 0
        if quantas <= 0:
            return None, None

        url = form.get("MediaUrl0")
        tipo = (form.get("MediaContentType0") or "").split(";")[0].strip()
        if not url:
            logger.warning("midia_sem_url", message_sid=form.get("MessageSid"))
            return None, None

        extensao = mimetypes.guess_extension(tipo) or ""
        if extensao.lstrip(".").lower() not in self._config.extensoes_permitidas:
            logger.warning("midia_recusada", motivo="tipo nao aceito", tipo=tipo)
            return None, None

        destino = Path(tempfile.gettempdir()) / f"marina-{uuid.uuid4().hex}{extensao}"
        try:
            # A autenticacao do download e a da propria conta.
            tamanho = await baixar_com_limite(
                self._http, url, destino, self._config.doc_max_bytes, auth=self._auth
            )
        except MidiaGrandeDemais:
            logger.warning(
                "midia_recusada", motivo="acima do limite", limite=self._config.doc_max_bytes
            )
            destino.unlink(missing_ok=True)
            return None, None
        except httpx.HTTPError as erro:
            logger.warning("midia_nao_baixada", erro=str(erro))
            destino.unlink(missing_ok=True)
            return None, None

        anexo = Anexo(
            nome_arquivo=destino.name,
            mime=tipo or "application/octet-stream",
            tamanho_bytes=tamanho,
            origem=str(destino),
        )
        return anexo, destino

    # --- saida --------------------------------------------------------------

    async def enviar_para(
        self, destino: str, mensagem: MensagemSaida, remetente: str | None = None
    ) -> None:
        """Uma mensagem, uma chamada.

        O engine ja envia em sequencia, esperando cada uma — e assim que precisa
        ser: a Marina responde duas ou mais por turno, e envios em paralelo podem
        chegar fora de ordem no celular do lead.

        `remetente` e o numero da empresa que esta atendendo. Sem ele, o lead
        receberia resposta de um numero para o qual nao escreveu. Opcional com
        retorno para a configuracao porque o terminal e o simulador nao tem empresa
        amarrada a um numero.

        Falha aqui NAO derruba o turno: o estado ja foi persistido, e o que resta
        e deixar o problema visivel. O status callback confirma o resto.
        """
        dados = {
            "From": como_endereco(remetente) if remetente else self._config.twilio_remetente,
            "To": como_endereco(destino),
            "Body": mensagem.conteudo,
        }
        if self._config.twilio_url_publica:
            dados["StatusCallback"] = (
                f"{self._config.twilio_url_publica.rstrip('/')}/webhooks/twilio/status"
            )

        url = _API.format(sid=self._config.twilio_account_sid)
        try:
            resposta = await self._http.post(url, data=dados, auth=self._auth)
            resposta.raise_for_status()
        except httpx.HTTPError as erro:
            # O texto do httpx diz so "400 Bad Request". Quem explica o que houve e
            # o corpo da Twilio, e e ali que vem o codigo — 63016 fora da janela de
            # 24h, 21654 template exigido, 21610 numero que pediu para sair. Sem
            # isto, toda falha de envio vira uma investigacao do zero.
            logger.error(
                "envio_falhou",
                template_key=mensagem.template_key,
                destino=destino,
                erro=str(erro),
                **_detalhe_twilio(erro),
            )
            return
        logger.info(
            "mensagem_enviada",
            template_key=mensagem.template_key,
            message_sid=resposta.json().get("sid"),
        )


class TwilioWhatsAppAdapter:
    """Adaptador de um turno: sabe para quem responder, e mais nada."""

    canal = CANAL

    def __init__(
        self, twilio: TwilioWhatsApp, destino: str, remetente: str | None = None
    ) -> None:
        self._twilio = twilio
        self._destino = destino
        #: O numero da empresa que esta atendendo.
        self._remetente = remetente

    async def receber(self) -> InboundMessage | None:
        """Canal de push: ninguem puxa mensagem daqui."""
        return None

    async def enviar(self, mensagem: MensagemSaida) -> None:
        await self._twilio.enviar_para(self._destino, mensagem, self._remetente)
