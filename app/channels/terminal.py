"""Adaptador de terminal.

E aqui — e SO aqui — que um texto digitado vira um Anexo. O adaptador e o unico
componente autorizado a olhar o disco; a maquina de estados recebe o resultado ja
resolvido e nunca pergunta se algo "e um caminho".

Quando o adaptador de WhatsApp existir, ele resolvera um media_id no mesmo Anexo,
e o fluxo nao muda uma linha.
"""

from __future__ import annotations

import asyncio
import mimetypes
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from app.channels.base import Anexo, InboundMessage, MensagemSaida


class TerminalAdapter:
    canal = "terminal"

    def __init__(self, prompt: str = "> ") -> None:
        self._prompt = prompt

    async def receber(self) -> InboundMessage | None:
        # input() bloqueia; sem a thread a corrotina travaria o loop inteiro.
        try:
            linha = await asyncio.to_thread(self._ler)
        except (EOFError, KeyboardInterrupt):
            return None
        if linha is None:
            return None

        anexo = self._resolver_anexo(linha)
        if anexo is not None:
            return InboundMessage(anexo=anexo)
        if self._parece_caminho(linha):
            # Quis mandar arquivo e nao deu. Dizer isso e melhor do que
            # responder "nao entendi" e deixar a pessoa adivinhar.
            return InboundMessage(texto=linha, anexo_nao_resolvido=linha.strip())
        return InboundMessage(texto=linha)

    @staticmethod
    def _parece_caminho(texto: str) -> bool:
        limpo = texto.strip().strip('"').strip("'")
        if not limpo:
            return False
        return (
            limpo.lower().startswith("file://")
            or "/" in limpo
            or "\\" in limpo
            or bool(Path(limpo).suffix)
        )

    def _ler(self) -> str | None:
        try:
            return input(self._prompt)
        except EOFError:
            return None

    @staticmethod
    def _resolver_anexo(texto: str) -> Anexo | None:
        """Texto -> Anexo, quando o texto aponta para um arquivo que existe.

        Exige extensao antes de tocar o disco. Sem isso, um nome proprio que por
        acaso coincida com um arquivo do diretorio viraria anexo.
        """
        limpo = texto.strip().strip('"').strip("'")
        if not limpo:
            return None

        # "file:///C:/..." e o que o Windows entrega ao copiar o endereco de um
        # arquivo do navegador ou do Explorer. Sem tratar isso, quem cola do
        # jeito mais natural recebe "nao entendi".
        if limpo.lower().startswith("file://"):
            limpo = url2pathname(unquote(urlparse(limpo).path))

        caminho = Path(limpo)
        if not caminho.suffix:
            return None
        try:
            if not caminho.is_file():
                return None
            tamanho = caminho.stat().st_size
        except OSError:
            return None

        return Anexo(
            nome_arquivo=caminho.name,
            mime=mimetypes.guess_type(str(caminho))[0] or "application/octet-stream",
            tamanho_bytes=tamanho,
            origem=str(caminho.resolve()),
        )

    async def enviar(self, mensagem: MensagemSaida) -> None:
        await asyncio.to_thread(self._escrever, mensagem.conteudo)

    @staticmethod
    def _escrever(conteudo: str) -> None:
        print(f"\n{conteudo}\n", flush=True)


class ScriptedAdapter:
    """Adaptador de teste: entrega uma lista de mensagens e guarda as respostas.

    Existe para o teste e2e poder comparar a transcricao inteira com um snapshot
    sem depender de stdin.
    """

    canal = "terminal"

    def __init__(self, entradas: list[InboundMessage]) -> None:
        self._entradas = list(entradas)
        self.enviadas: list[MensagemSaida] = []

    async def receber(self) -> InboundMessage | None:
        if not self._entradas:
            return None
        return self._entradas.pop(0)

    async def enviar(self, mensagem: MensagemSaida) -> None:
        self.enviadas.append(mensagem)


def encerrar_stdin() -> None:
    """Fecha stdin, para o CLI terminar limpo em pipe."""
    if not sys.stdin.closed:
        sys.stdin.close()
