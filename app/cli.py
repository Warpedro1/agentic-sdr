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
from app.config import obter_configuracao
from app.crm import CrmSinkNoop
from app.db import encerrar, obter_engine
from app.engine import Engine
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
    motor = Engine(
        criar_sessao=criar_sessao,
        adaptador=TerminalAdapter(),
        config=config,
        armazenamento=LocalDocumentStorage(config),
        crm=CrmSinkNoop(),
    )

    try:
        conversa_id = await motor.rodar()
        print(f"\n[conversa {conversa_id}]")
    finally:
        await encerrar()


if __name__ == "__main__":
    executar(main())
