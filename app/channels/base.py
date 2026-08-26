"""Contrato entre o mundo e a maquina de estados.

Este modulo NAO faz I/O. Ele so define os tipos que atravessam a fronteira, e e
por isso que `app.machine` pode importa-lo sem violar a pureza.

A entrada e tipada de proposito. Se o adaptador entregasse uma string crua, a
maquina teria que descobrir sozinha se aquele texto e um caminho de arquivo — o
que significaria tocar o disco dentro da funcao pura, e amarraria o fluxo ao
terminal (no WhatsApp nao chega caminho nenhum, chega um id de midia).

Quem resolve o mundo e o adaptador: o TerminalAdapter transforma um caminho em
Anexo, o WhatsAppAdapter um dia transformara um media_id no mesmo Anexo. A
maquina so pergunta `entrada.anexo is not None`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Anexo:
    """Arquivo ja resolvido pelo adaptador, antes de chegar na maquina."""

    nome_arquivo: str
    mime: str
    tamanho_bytes: int
    #: Como o adaptador recupera o conteudo: caminho local no terminal,
    #: media_id no WhatsApp.
    origem: str


@dataclass(frozen=True)
class InboundMessage:
    """O que o lead mandou. Texto, anexo, ou nenhum dos dois (reentrada)."""

    texto: str | None = None
    anexo: Anexo | None = None

    @property
    def vazia(self) -> bool:
        return self.texto is None and self.anexo is None


@dataclass(frozen=True)
class MensagemSaida:
    """O que a Marina responde. Sempre escolhida do catalogo, nunca gerada."""

    template_key: str
    conteudo: str


@runtime_checkable
class ChannelAdapter(Protocol):
    """Adaptador burro: converte mundo -> InboundMessage e MensagemSaida -> envio."""

    canal: str

    async def receber(self) -> InboundMessage | None:
        """Proxima mensagem do lead, ou None quando o canal encerra."""
        ...

    async def enviar(self, mensagem: MensagemSaida) -> None:
        """Entrega a resposta ao lead."""
        ...
