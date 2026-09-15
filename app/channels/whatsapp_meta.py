"""Adaptador de WhatsApp direto na Meta (Cloud API), sem intermediario.

Mesmo papel do adaptador da Twilio, e nenhuma regra de conversa aqui tambem: este
arquivo converte mundo. O que muda em relacao ao outro provedor:

1. **O endereco de envio e o id do numero, nao o numero.** A URL e
   `/{versao}/{phone_number_id}/messages`, e por isso cada empresa guarda esse id
   (`empresas.whatsapp_numero_id`) alem do numero em si.

2. **O corpo e JSON, aninhado.** A mensagem vem em
   `entry[].changes[].value.messages[]`, e no mesmo lugar chegam os avisos de
   entrega (`value.statuses[]`) — nao ha endereco separado para status, como na
   Twilio.

3. **O telefone vem sem o `+`.** A Meta entrega `558191234567`; a identidade da
   conversa em todo o resto do sistema e E.164 com o sinal. A conversao acontece
   aqui, na fronteira, e nao se espalha.

4. **A midia exige duas chamadas**: uma para descobrir a URL a partir do id, outra
   para baixar. Na Twilio a URL ja vinha pronta no formulario.
"""

from __future__ import annotations

import mimetypes
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from app.channels.base import Anexo, EntradaWhatsApp, InboundMessage, MensagemSaida
from app.config import Configuracao, obter_configuracao
from app.logging import obter_logger

logger = obter_logger("whatsapp_meta")

CANAL = "whatsapp"

_GRAFO = "https://graph.facebook.com"

#: Tipos de mensagem que trazem arquivo. `sticker` fica de fora de proposito: nao
#: e documento, e tratar como anexo so geraria recusa por extensao.
_TIPOS_COM_MIDIA = frozenset({"document", "image", "audio", "video"})


def com_mais(numero: str) -> str:
    """`558191234567` -> `+558191234567`.

    A Meta entrega telefone so com digitos. O resto do sistema — identidade da
    conversa, cadastro do lead, trava de concorrencia — fala E.164 com o sinal.
    """
    limpo = (numero or "").strip()
    if not limpo:
        return ""
    return limpo if limpo.startswith("+") else f"+{limpo}"


def blocos_de(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Os blocos `value` de um webhook da Meta, na ordem em que chegaram.

    A Meta agrupa: uma unica chamada pode carregar varias entradas, cada uma com
    varias mudancas. Na pratica quase sempre vem uma so, mas tratar o formato como
    se fosse singular seria perder mensagem em silencio no dia em que nao for.
    """
    blocos: list[dict[str, Any]] = []
    for entrada in payload.get("entry") or []:
        if not isinstance(entrada, dict):
            continue
        for mudanca in entrada.get("changes") or []:
            if not isinstance(mudanca, dict):
                continue
            valor = mudanca.get("value")
            if isinstance(valor, dict):
                blocos.append(valor)
    return blocos


def numero_id_de(valor: Mapping[str, Any]) -> str:
    """O id do numero que RECEBEU a mensagem. E a chave de roteamento por empresa."""
    metadados = valor.get("metadata")
    if not isinstance(metadados, dict):
        return ""
    return str(metadados.get("phone_number_id") or "")


def id_da_mensagem(valor: Mapping[str, Any]) -> str:
    """O id da mensagem do lead neste bloco, ou vazio se o bloco nao trouxer uma.

    E a chave de idempotencia, e e lida ANTES de traduzir a mensagem: traduzir pode
    significar baixar um anexo, e reentrega ou numero desconhecido nao devem baixar.
    """
    mensagens = valor.get("messages")
    if not isinstance(mensagens, list) or not mensagens:
        return ""
    primeira = mensagens[0]
    return str(primeira.get("id") or "") if isinstance(primeira, dict) else ""


class MetaWhatsApp:
    """Conversa com a Cloud API da Meta. Uma instancia por processo."""

    def __init__(
        self, config: Configuracao | None = None, http: httpx.AsyncClient | None = None
    ) -> None:
        self._config = config or obter_configuracao()
        self._http = http or httpx.AsyncClient(timeout=30.0)

    @property
    def _cabecalhos(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.meta_access_token}"}

    def adaptador(self, destino: str, numero_id: str) -> MetaWhatsAppAdapter:
        """Um adaptador amarrado a quem responde, e pelo numero de qual empresa."""
        return MetaWhatsAppAdapter(self, destino, numero_id)

    async def fechar(self) -> None:
        await self._http.aclose()

    # --- entrada ------------------------------------------------------------

    async def receber_valor(self, valor: Mapping[str, Any]) -> EntradaWhatsApp | None:
        """Um bloco `value` do webhook -> mensagem tipada.

        Devolve None quando o bloco nao carrega mensagem de lead: aviso de entrega,
        mudanca de configuracao da conta, ou um tipo que ainda nao tratamos. Nenhum
        deles e erro — a Meta usa o mesmo endereco para tudo.
        """
        mensagens = valor.get("messages")
        if not isinstance(mensagens, list) or not mensagens:
            return None
        mensagem = mensagens[0]
        if not isinstance(mensagem, dict):
            return None

        identidade = com_mais(str(mensagem.get("from") or ""))
        tipo = str(mensagem.get("type") or "")
        if tipo != "text" and tipo not in _TIPOS_COM_MIDIA:
            # Reacao, figurinha, localizacao... Nenhum deles responde a pergunta que
            # a Marina fez. Passar adiante como mensagem vazia contaria como
            # tentativa falha — o lead seria escalado por reagir com um emoji.
            logger.info("tipo_nao_tratado", tipo=tipo, message_sid=mensagem.get("id"))
            return None
        texto = None
        if tipo == "text":
            corpo = mensagem.get("text")
            if isinstance(corpo, dict):
                texto = (corpo.get("body") or "").strip() or None

        anexo, temporario = await self._resolver_anexo(mensagem, tipo)

        return EntradaWhatsApp(
            identidade=identidade,
            entrada=InboundMessage(texto=None if anexo else texto, anexo=anexo),
            message_sid=str(mensagem.get("id") or ""),
            destinatario=numero_id_de(valor),
            profile_name=_perfil_de(valor),
            temporario=temporario,
        )

    async def _resolver_anexo(
        self, mensagem: Mapping[str, Any], tipo: str
    ) -> tuple[Anexo | None, Path | None]:
        """Baixa a midia para um arquivo temporario, se houver uma aceitavel.

        Mesmo criterio do outro provedor — baixar aqui, e nao guardar a referencia,
        e o que mantem o armazenamento local intacto: ele valida pela extensao e
        confere que o arquivo existe. Midia recusada volta como (None, None) e vira
        log, nunca excecao: o turno nao pode cair por causa de um anexo.
        """
        if tipo not in _TIPOS_COM_MIDIA:
            return None, None

        bloco = mensagem.get(tipo)
        if not isinstance(bloco, dict):
            return None, None
        media_id = str(bloco.get("id") or "")
        if not media_id:
            logger.warning("midia_sem_id", message_sid=mensagem.get("id"))
            return None, None

        mime = str(bloco.get("mime_type") or "").split(";")[0].strip()
        extensao = mimetypes.guess_extension(mime) or ""
        if extensao.lstrip(".").lower() not in self._config.extensoes_permitidas:
            logger.warning("midia_recusada", motivo="tipo nao aceito", tipo=mime)
            return None, None

        destino = Path(tempfile.gettempdir()) / f"marina-{uuid.uuid4().hex}{extensao}"
        try:
            url = await self._url_da_midia(media_id)
            tamanho = await self._baixar(url, destino)
        except _MidiaGrandeDemais:
            logger.warning(
                "midia_recusada",
                motivo="acima do limite",
                limite=self._config.doc_max_bytes,
            )
            destino.unlink(missing_ok=True)
            return None, None
        except httpx.HTTPError as erro:
            logger.warning("midia_nao_baixada", erro=str(erro))
            destino.unlink(missing_ok=True)
            return None, None

        anexo = Anexo(
            nome_arquivo=destino.name,
            mime=mime or "application/octet-stream",
            tamanho_bytes=tamanho,
            origem=str(destino),
        )
        return anexo, destino

    async def _url_da_midia(self, media_id: str) -> str:
        """A primeira das duas chamadas: id -> URL temporaria de download."""
        url = f"{_GRAFO}/{self._config.meta_api_versao}/{media_id}"
        resposta = await self._http.get(url, headers=self._cabecalhos)
        resposta.raise_for_status()
        return str(resposta.json().get("url") or "")

    async def _baixar(self, url: str, destino: Path) -> int:
        """Grava a midia em `destino` e devolve o tamanho.

        A URL de download tambem exige o token — ela e temporaria, mas nao publica.
        O tamanho e conferido DURANTE o download, para um arquivo enorme nao encher
        o disco antes de alguem reparar.
        """
        limite = self._config.doc_max_bytes
        escritos = 0
        async with self._http.stream(
            "GET", url, headers=self._cabecalhos, follow_redirects=True
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

    async def enviar_para(
        self, destino: str, mensagem: MensagemSaida, numero_id: str
    ) -> None:
        """Uma mensagem, uma chamada.

        O engine ja envia em sequencia, esperando cada uma — e assim que precisa
        ser: a Marina responde duas ou mais por turno, e envios em paralelo podem
        chegar fora de ordem no celular do lead.

        Falha aqui NAO derruba o turno: o estado ja foi persistido, e o que resta e
        deixar o problema visivel com o motivo que a Meta deu.
        """
        if not numero_id:
            logger.error(
                "envio_sem_numero_id",
                template_key=mensagem.template_key,
                destino=destino,
            )
            return

        url = f"{_GRAFO}/{self._config.meta_api_versao}/{numero_id}/messages"
        corpo = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": com_mais(destino),
            "type": "text",
            "text": {"preview_url": False, "body": mensagem.conteudo},
        }
        try:
            resposta = await self._http.post(url, json=corpo, headers=self._cabecalhos)
            resposta.raise_for_status()
        except httpx.HTTPError as erro:
            logger.error(
                "envio_falhou",
                template_key=mensagem.template_key,
                destino=destino,
                erro=str(erro),
                **_detalhe_meta(erro),
            )
            return

        logger.info(
            "mensagem_enviada",
            template_key=mensagem.template_key,
            message_sid=_id_enviado(resposta),
        )


def _perfil_de(valor: Mapping[str, Any]) -> str | None:
    contatos = valor.get("contacts")
    if not isinstance(contatos, list) or not contatos:
        return None
    primeiro = contatos[0]
    if not isinstance(primeiro, dict):
        return None
    perfil = primeiro.get("profile")
    if not isinstance(perfil, dict):
        return None
    return perfil.get("name") or None


def _id_enviado(resposta: httpx.Response) -> str | None:
    try:
        mensagens = resposta.json().get("messages")
    except Exception:  # noqa: BLE001 - so o log perde detalhe
        return None
    if isinstance(mensagens, list) and mensagens and isinstance(mensagens[0], dict):
        return mensagens[0].get("id")
    return None


def _detalhe_meta(erro: Exception) -> dict[str, object]:
    """O que a Meta respondeu, quando ela respondeu alguma coisa.

    O texto do httpx diz so "400 Bad Request"; quem explica e o corpo. Aqui nada
    pode estourar — este codigo roda dentro do tratamento de OUTRO erro.
    """
    resposta = getattr(erro, "response", None)
    if resposta is None:
        return {}
    detalhe: dict[str, object] = {"http": resposta.status_code}
    try:
        corpo = resposta.json()
    except Exception:  # noqa: BLE001 - corpo nao-JSON e so ausencia de detalhe
        return detalhe
    falha = corpo.get("error") if isinstance(corpo, dict) else None
    if isinstance(falha, dict):
        for origem, destino in (
            ("code", "codigo"),
            ("message", "meta_mensagem"),
            ("error_subcode", "subcodigo"),
        ):
            if falha.get(origem) is not None:
                detalhe[destino] = falha[origem]
        detalhes = falha.get("error_data")
        if isinstance(detalhes, dict) and detalhes.get("details"):
            detalhe["meta_detalhe"] = detalhes["details"]
    return detalhe


class _MidiaGrandeDemais(Exception):
    """Passou de `doc_max_bytes` no meio do download."""


class MetaWhatsAppAdapter:
    """Adaptador de um turno: sabe para quem responder, e mais nada."""

    canal = CANAL

    def __init__(self, meta: MetaWhatsApp, destino: str, numero_id: str) -> None:
        self._meta = meta
        self._destino = destino
        #: O id do numero da empresa que esta atendendo.
        self._numero_id = numero_id

    async def receber(self) -> InboundMessage | None:
        """Canal de push: ninguem puxa mensagem daqui."""
        return None

    async def enviar(self, mensagem: MensagemSaida) -> None:
        await self._meta.enviar_para(self._destino, mensagem, self._numero_id)
