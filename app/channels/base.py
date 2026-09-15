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
from pathlib import Path
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
    #: O adaptador reconheceu a mensagem como uma tentativa de enviar arquivo,
    #: mas nao conseguiu resolver. Guarda o que foi tentado.
    #:
    #: Sem isto a maquina responderia "nao entendi" — e quem digitou um caminho
    #: com um caractere a menos ficaria sem saber o que houve, gastando as tres
    #: tentativas ate ser escalado para um humano.
    anexo_nao_resolvido: str | None = None

    @property
    def vazia(self) -> bool:
        return self.texto is None and self.anexo is None


@dataclass(frozen=True)
class MensagemSaida:
    """O que a Marina responde. Sempre escolhida do catalogo, nunca gerada."""

    template_key: str
    conteudo: str


@dataclass(frozen=True)
class EntradaWhatsApp:
    """Uma mensagem de WhatsApp recebida, ja traduzida pelo adaptador.

    Mora aqui, e nao no modulo de um provedor, porque atravessa a mesma fronteira
    que os tipos acima: e o que a camada web recebe de QUALQUER adaptador de
    WhatsApp. Twilio e Meta entregam formatos completamente diferentes e produzem
    esta mesma coisa — que e o ponto.
    """

    #: Telefone E.164 sem prefixo. E a identidade da conversa.
    identidade: str
    entrada: InboundMessage
    #: A chave de idempotencia: qualquer provedor pode entregar duas vezes.
    message_sid: str
    #: O numero QUE RECEBEU a mensagem. Cada empresa tem o seu, entao e por aqui
    #: que se descobre por qual empresa este atendimento acontece. So e confiavel
    #: depois de a autenticidade da chamada ser conferida — ele vem no corpo.
    destinatario: str = ""
    #: Nome do perfil no WhatsApp. Vai para o log e NAO substitui a pergunta do
    #: nome — a Marina pergunta, e o cadastro e o que ela apurou.
    profile_name: str | None = None
    #: Arquivo temporario baixado, para o chamador apagar depois do turno.
    temporario: Path | None = None


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
