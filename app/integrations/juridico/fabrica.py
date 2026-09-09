"""Monta as peças do juridico para uma empresa.

Cada instalação do juridico representa UMA empresa, e a Marina atende várias. Por
isso tudo aqui recebe a empresa: é dela que saem o endereço e a credencial.

Existe para que os pontos de entrada e os scripts não precisem saber como o cliente
é construído — e para que ligar ou desligar a integração seja uma decisão num lugar
só.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Configuracao, obter_configuracao
from app.crm import CrmSink, CrmSinkNoop, CrmSinkOutbox
from app.empresas import EmpresaCtx
from app.integrations.juridico.cliente import ClienteJuridico
from app.integrations.juridico.entregador import EntregadorJuridico
from app.logging import obter_logger

logger = obter_logger("juridico.fabrica")


class IntegracaoDesligada(Exception):
    """Pediram a integração, mas ela não está configurada."""


def integracao_ativa(empresa: EmpresaCtx, config: Configuracao | None = None) -> bool:
    """Se esta empresa entrega no juridico dela.

    Três condições, e as três precisam valer. `juridico_ativo` da configuração é o
    **desligamento de emergência**: derruba todas as empresas de uma vez. As outras
    duas são da empresa — quem liga e desliga uma empresa é o cadastro dela.

    Sem retorno para a credencial global: numa configuração errada, isso entregaria
    o dado de uma empresa com a credencial de outra.
    """
    # Recusa explicita: a `Configuracao` tem, por coincidencia, os mesmos tres
    # nomes de atributo que esta funcao le — `juridico_ativo`, `juridico_base_url`
    # e `juridico_token`. Sem esta guarda, passar a configuracao no lugar da
    # empresa PASSA em silencio e volta a usar a credencial global, furando pela
    # porta dos fundos a regra de "cada empresa entrega no juridico dela". Foi
    # exatamente o que aconteceu com `flush_outbox.py`.
    if not isinstance(empresa, EmpresaCtx):
        raise TypeError(
            f"integracao_ativa espera uma empresa, recebeu {type(empresa).__name__}. "
            "Resolva a empresa com `app.empresas.resolver_argumento` antes."
        )
    config = config or obter_configuracao()
    return bool(
        config.juridico_ativo
        and empresa.juridico_ativo
        and empresa.juridico_base_url
        and empresa.juridico_token
    )


def montar_sink(
    criar_sessao: async_sessionmaker[AsyncSession],
    empresa: EmpresaCtx,
    config: Configuracao | None = None,
) -> CrmSink:
    """O destino dos eventos da conversa desta empresa.

    Com a integração desligada devolve o no-op — e a Marina atende normalmente, sem
    o lead perceber. Empresa mal configurada é problema de operação, não erro em
    runtime; o diagnóstico de `cadastrar_empresa.py --listar` é quem denuncia.
    """
    config = config or obter_configuracao()
    if not integracao_ativa(empresa, config):
        return CrmSinkNoop()
    return CrmSinkOutbox(criar_sessao, empresa.id)


def montar_cliente(
    empresa: EmpresaCtx, config: Configuracao | None = None
) -> ClienteJuridico:
    config = config or obter_configuracao()
    if not integracao_ativa(empresa, config):
        raise IntegracaoDesligada(
            f"a empresa '{empresa.slug}' não está integrada. Confira com "
            "`python scripts/cadastrar_empresa.py --listar`: falta o endereço do "
            "juridico, a credencial no .env, ou JURIDICO_ATIVO=true."
        )
    return ClienteJuridico(
        base_url=empresa.juridico_base_url,
        token=empresa.juridico_token,
        timeout_s=config.juridico_timeout_s,
    )


def montar_entregador(
    criar_sessao: async_sessionmaker[AsyncSession],
    cliente: ClienteJuridico,
    empresa: EmpresaCtx,
    config: Configuracao | None = None,
) -> EntregadorJuridico:
    config = config or obter_configuracao()
    return EntregadorJuridico(
        criar_sessao=criar_sessao,
        cliente=cliente,
        empresa_id=empresa.id,
        max_tentativas=config.outbox_max_tentativas,
        canal_contato=config.juridico_canal_contato,
    )
