"""Montagem das dependencias, separada de quem as usa.

Existe por causa do WhatsApp. No terminal ha uma conversa por processo, entao dava
para montar tudo junto com o Engine. Num canal de push o Engine e montado POR
MENSAGEM — o adaptador carrega o destino da resposta —, e as pecas caras (o
cliente HTTP do juridico, que faz login e guarda o token; o entregador; o
armazenamento) precisam nascer uma vez so, no boot.

Nenhuma regra de conversa mora aqui: e fiacao.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.base import ChannelAdapter
from app.config import Configuracao, obter_configuracao
from app.crm import CrmSink
from app.engine import Engine
from app.integrations.juridico.fabrica import (
    integracao_ativa,
    montar_cliente,
    montar_entregador,
    montar_sink,
)
from app.storage.local import ArmazenamentoDocumento, LocalDocumentStorage


@dataclass(frozen=True)
class Dependencias:
    """Tudo o que o Engine precisa, menos o adaptador."""

    config: Configuracao
    criar_sessao: async_sessionmaker[AsyncSession]
    armazenamento: ArmazenamentoDocumento
    crm: CrmSink
    #: None quando a integracao esta desligada — a Marina roda sozinha.
    entregador: Any | None = None
    #: Idem. Guardado para poder ser fechado no encerramento.
    cliente: Any | None = None

    async def fechar(self) -> None:
        if self.cliente is not None:
            await self.cliente.fechar()


def montar_dependencias(
    criar_sessao: async_sessionmaker[AsyncSession],
    config: Configuracao | None = None,
) -> Dependencias:
    """Monta as pecas que a configuracao pedir, uma vez.

    Com a integracao desligada, `cliente` e `entregador` sao None e o sink vira o
    no-op — exatamente como antes de existir CRM externo.
    """
    config = config or obter_configuracao()
    cliente = montar_cliente(config) if integracao_ativa(config) else None
    entregador = montar_entregador(criar_sessao, cliente, config) if cliente else None
    return Dependencias(
        config=config,
        criar_sessao=criar_sessao,
        armazenamento=LocalDocumentStorage(config),
        crm=montar_sink(criar_sessao, config),
        entregador=entregador,
        cliente=cliente,
    )


def montar_motor(deps: Dependencias, adaptador: ChannelAdapter) -> Engine:
    """Um Engine para este adaptador. Barato: so guarda referencias."""
    return Engine(
        criar_sessao=deps.criar_sessao,
        adaptador=adaptador,
        config=deps.config,
        armazenamento=deps.armazenamento,
        crm=deps.crm,
        entregador=deps.entregador,
    )
