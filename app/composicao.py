"""Montagem das dependencias, separada de quem as usa.

Existe por causa do WhatsApp. No terminal ha uma conversa por processo, entao dava
para montar tudo junto com o Engine. Num canal de push o Engine e montado POR
MENSAGEM — o adaptador carrega o destino da resposta —, e as pecas caras precisam
nascer uma vez so, no boot.

A Marina atende varias empresas, e cada uma tem a sua instalacao do juridico. Por
isso o que era **um** cliente virou um registro: um por empresa, criado na primeira
vez que ela recebe mensagem.

Nenhuma regra de conversa mora aqui: e fiacao.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.base import ChannelAdapter
from app.config import Configuracao, obter_configuracao
from app.crm import CrmSink, CrmSinkNoop
from app.empresas import EmpresaCtx
from app.engine import Engine
from app.integrations.juridico.cliente import ClienteJuridico
from app.integrations.juridico.entregador import EntregadorJuridico
from app.integrations.juridico.fabrica import (
    integracao_ativa,
    montar_cliente,
    montar_entregador,
    montar_sink,
)
from app.logging import obter_logger
from app.storage.local import ArmazenamentoDocumento, LocalDocumentStorage

logger = obter_logger("composicao")


class RegistroIntegracoes:
    """Um cliente de juridico por empresa, criado sob demanda.

    Sob demanda porque uma empresa sem trafego nao precisa abrir conexao nenhuma —
    e isso importa quando forem vinte.

    Nao ha trava: `ClienteJuridico.__init__` e sincrono, entao entre conferir o
    cache e preenche-lo nao existe ponto de `await` e o asyncio nao intercala. Uma
    trava aqui seria cerimonia sem risco a cobrir.
    """

    def __init__(
        self,
        criar_sessao: async_sessionmaker[AsyncSession],
        config: Configuracao,
    ) -> None:
        self._criar_sessao = criar_sessao
        self._config = config
        #: empresa -> (impressao digital da credencial, cliente, entregador)
        self._cache: dict[uuid.UUID, tuple[tuple[str, str], ClienteJuridico, EntregadorJuridico]] = {}
        #: Clientes substituidos por troca de endereco ou rotacao de credencial.
        #: Fechar exige `await`, entao ficam aqui ate o encerramento.
        self._aposentados: list[ClienteJuridico] = []

    def sink(self, empresa: EmpresaCtx) -> CrmSink:
        return montar_sink(self._criar_sessao, empresa, self._config)

    def entregador(self, empresa: EmpresaCtx) -> EntregadorJuridico | None:
        """O entregador desta empresa, ou None se ela nao esta integrada."""
        if not integracao_ativa(empresa, self._config):
            return None
        return self._peca(empresa)[1]

    def cliente(self, empresa: EmpresaCtx) -> ClienteJuridico | None:
        if not integracao_ativa(empresa, self._config):
            return None
        return self._peca(empresa)[0]

    def _peca(self, empresa: EmpresaCtx) -> tuple[ClienteJuridico, EntregadorJuridico]:
        # A credencial pode ter sido rotacionada, ou o endereco corrigido, desde a
        # ultima mensagem. Guardar a impressao digital junto e o que faz o cliente
        # velho sair de cena em vez de continuar falando com o lugar errado.
        digital = (empresa.juridico_base_url, empresa.juridico_token)
        guardado = self._cache.get(empresa.id)
        if guardado is not None:
            if guardado[0] == digital:
                return guardado[1], guardado[2]
            logger.info("cliente_juridico_renovado", empresa=empresa.slug)
            self._aposentados.append(guardado[1])

        cliente = montar_cliente(empresa, self._config)
        entregador = montar_entregador(self._criar_sessao, cliente, self._config)
        self._cache[empresa.id] = (digital, cliente, entregador)
        return cliente, entregador

    async def fechar(self) -> None:
        for _, cliente, _ in self._cache.values():
            await cliente.fechar()
        for cliente in self._aposentados:
            await cliente.fechar()
        self._cache.clear()
        self._aposentados.clear()


@dataclass(frozen=True)
class Dependencias:
    """Tudo o que o Engine precisa, menos o adaptador e a empresa."""

    config: Configuracao
    criar_sessao: async_sessionmaker[AsyncSession]
    armazenamento: ArmazenamentoDocumento
    integracoes: RegistroIntegracoes

    async def fechar(self) -> None:
        await self.integracoes.fechar()


def montar_dependencias(
    criar_sessao: async_sessionmaker[AsyncSession],
    config: Configuracao | None = None,
) -> Dependencias:
    """Monta o que vale para todas as empresas, uma vez.

    O que e por empresa — cliente, entregador, destino dos eventos — nasce no
    registro, na primeira mensagem daquela empresa.
    """
    config = config or obter_configuracao()
    return Dependencias(
        config=config,
        criar_sessao=criar_sessao,
        armazenamento=LocalDocumentStorage(config),
        integracoes=RegistroIntegracoes(criar_sessao, config),
    )


def montar_motor(
    deps: Dependencias, adaptador: ChannelAdapter, empresa: EmpresaCtx
) -> Engine:
    """Um Engine para esta empresa e este adaptador. Barato: so guarda referencias.

    `empresa` e obrigatoria e sem valor padrao. Um padrao significando "qualquer
    empresa" seria exatamente o defeito que esta mudanca existe para eliminar,
    voltando com cara de conveniencia.
    """
    return Engine(
        criar_sessao=deps.criar_sessao,
        adaptador=adaptador,
        empresa=empresa,
        config=deps.config,
        armazenamento=deps.armazenamento,
        crm=deps.integracoes.sink(empresa),
        entregador=deps.integracoes.entregador(empresa),
    )


__all__ = [
    "CrmSinkNoop",
    "Dependencias",
    "RegistroIntegracoes",
    "montar_dependencias",
    "montar_motor",
]
