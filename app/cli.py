"""Ponto de entrada da Marina no terminal.

    python -m app.cli

Monta as dependencias e entrega ao engine. Nenhuma regra de conversa mora aqui:
trocar este arquivo por um adaptador de WhatsApp nao muda o fluxo.
"""

from __future__ import annotations

import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.aio import executar
from app.channels.terminal import TerminalAdapter
from app.config import Configuracao, obter_configuracao
from app.db import encerrar, obter_engine
from app.engine import Engine
from app.integrations.juridico.fabrica import (
    integracao_ativa,
    montar_cliente,
    montar_entregador,
    montar_sink,
)
from app.logging import configurar_logging
from app.storage.local import LocalDocumentStorage


def _forcar_utf8() -> None:
    """O console do Windows nao usa UTF-8 por padrao.

    Sem isto, "Ola" sai correto mas "Voce" e "cartao" saem quebrados — e a copy
    inteira e em portugues.
    """
    for fluxo in (sys.stdout, sys.stdin):
        if hasattr(fluxo, "reconfigure"):
            try:
                fluxo.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


async def main() -> None:
    _forcar_utf8()
    configurar_logging()
    config = obter_configuracao()

    criar_sessao = async_sessionmaker(obter_engine(), expire_on_commit=False)
    motor, cliente = montar_motor(criar_sessao, TerminalAdapter(), config)

    try:
        conversa_id = await motor.rodar()
        print(f"\n[conversa {conversa_id}]")
    finally:
        if cliente is not None:
            await cliente.fechar()
        await encerrar()


def montar_motor(criar_sessao, adaptador, config: Configuracao):
    """Monta o engine com as dependências que a configuração pedir.

    Separado de `main` para poder ser testado: a fiação do CLI já quebrou uma vez
    sem nenhum teste perceber, porque nada aqui era exercitado.

    Com a integração desligada, `cliente` é None e o sink vira o no-op — a Marina
    roda sozinha, exatamente como antes de existir CRM externo.
    """
    cliente = montar_cliente(config) if integracao_ativa(config) else None
    entregador = montar_entregador(criar_sessao, cliente, config) if cliente else None
    motor = Engine(
        criar_sessao=criar_sessao,
        adaptador=adaptador,
        config=config,
        armazenamento=LocalDocumentStorage(config),
        crm=montar_sink(criar_sessao, config),
        entregador=entregador,
    )
    return motor, cliente


if __name__ == "__main__":
    executar(main())
