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

from app.channels.base import Anexo, InboundMessage, MensagemSaida
from app.config import Configuracao, obter_configuracao
from app.logging import obter_logger

logger = obter_logger("whatsapp")

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


@dataclass(frozen=True)
class EntradaWhatsApp:
    """Uma mensagem recebida, ja traduzida."""

    #: Telefone E.164 sem o prefixo. E a identidade da conversa.
    identidade: str
    entrada: InboundMessage
    #: `MessageSid`. A chave de idempotencia: a Twilio pode entregar duas vezes.
    message_sid: str
    #: Nome do perfil no WhatsApp. Vai para o log e NAO substitui a pergunta do
    #: nome — a Marina pergunta, e o cadastro e o que ela apurou.
    profile_name: str | None = None
    #: Arquivo temporario baixado, para o chamador apagar depois do turno.
    temporario: Path | None = None


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

    def adaptador(self, destino: str) -> TwilioWhatsAppAdapter:
        """Um adaptador amarrado a quem vai receber a resposta."""
        return TwilioWhatsAppAdapter(self, destino)

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
            tamanho = await self._baixar(url, destino)
        except _MidiaGrandeDemais:
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

    async def _baixar(self, url: str, destino: Path) -> int:
        """Grava a midia em `destino` e devolve o tamanho.

        `follow_redirects` porque a URL da Twilio redireciona para o
        armazenamento; a autenticacao e a da propria conta. O tamanho e conferido
        durante o download, para um arquivo enorme nao encher o disco antes de
        alguem reparar.
        """
        limite = self._config.doc_max_bytes
        escritos = 0
        async with self._http.stream(
            "GET", url, auth=self._auth, follow_redirects=True
        ) as resposta:
            resposta.raise_for_status()
            with destino.open("wb") as arquivo:
                async for pedaco in resposta.aiter_bytes():
                    escritos += len(pedaco)
                    if escritos > limite:
                        raise _MidiaGrandeDemais
                    arquivo.write(pedaco)
        return escritos

    # --- saida --------------------------------------------------------------

    async def enviar_para(self, destino: str, mensagem: MensagemSaida) -> None:
        """Uma mensagem, uma chamada.

        O engine ja envia em sequencia, esperando cada uma — e assim que precisa
        ser: a Marina responde duas ou mais por turno, e envios em paralelo podem
        chegar fora de ordem no celular do lead.

        Falha aqui NAO derruba o turno: o estado ja foi persistido, e o que resta
        e deixar o problema visivel. O status callback confirma o resto.
        """
        dados = {
            "From": self._config.twilio_remetente,
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
            logger.error(
                "envio_falhou",
                template_key=mensagem.template_key,
                destino=destino,
                erro=str(erro),
            )
            return
        logger.info(
            "mensagem_enviada",
            template_key=mensagem.template_key,
            message_sid=resposta.json().get("sid"),
        )


class _MidiaGrandeDemais(Exception):
    """Passou de `doc_max_bytes` no meio do download."""


class TwilioWhatsAppAdapter:
    """Adaptador de um turno: sabe para quem responder, e mais nada."""

    canal = CANAL

    def __init__(self, twilio: TwilioWhatsApp, destino: str) -> None:
        self._twilio = twilio
        self._destino = destino

    async def receber(self) -> InboundMessage | None:
        """Canal de push: ninguem puxa mensagem daqui."""
        return None

    async def enviar(self, mensagem: MensagemSaida) -> None:
        await self._twilio.enviar_para(self._destino, mensagem)
