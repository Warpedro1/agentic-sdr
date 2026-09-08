"""Monta as peças do juridico a partir da configuração.

Existe para que `cli.py` e os scripts não precisem saber como o cliente é
construído — e para que ligar ou desligar a integração seja uma decisão num
lugar só.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Configuracao, obter_configuracao
from app.crm import CrmSink, CrmSinkNoop, CrmSinkOutbox
from app.integrations.juridico.cliente import ClienteJuridico
from app.integrations.juridico.entregador import EntregadorJuridico
from app.logging import obter_logger

logger = obter_logger("juridico.fabrica")


class IntegracaoDesligada(Exception):
    """Pediram a integração, mas ela não está configurada."""


def integracao_ativa(config: Configuracao | None = None) -> bool:
    config = config or obter_configuracao()
    return bool(config.juridico_ativo and config.juridico_token)


def montar_sink(
    criar_sessao: async_sessionmaker[AsyncSession], config: Configuracao | None = None
) -> CrmSink:
    """O destino dos eventos da conversa.

    Com a integração desligada devolve o no-op — e a Marina funciona sozinha,
    exatamente como no MVP.
    """
    config = config or obter_configuracao()
    if not integracao_ativa(config):
        return CrmSinkNoop()
    return CrmSinkOutbox(criar_sessao)


def montar_cliente(config: Configuracao | None = None) -> ClienteJuridico:
    config = config or obter_configuracao()
    if not integracao_ativa(config):
        raise IntegracaoDesligada(
            "defina JURIDICO_ATIVO=true e JURIDICO_TOKEN no .env "
            "(gere o token no juridico com `php artisan integracao:usuario`)"
        )
    return ClienteJuridico(
        base_url=config.juridico_base_url,
        token=config.juridico_token,
        timeout_s=config.juridico_timeout_s,
    )


def montar_entregador(
    criar_sessao: async_sessionmaker[AsyncSession],
    cliente: ClienteJuridico,
    config: Configuracao | None = None,
) -> EntregadorJuridico:
    config = config or obter_configuracao()
    return EntregadorJuridico(
        criar_sessao=criar_sessao,
        cliente=cliente,
        max_tentativas=config.outbox_max_tentativas,
        canal_contato=config.juridico_canal_contato,
    )
