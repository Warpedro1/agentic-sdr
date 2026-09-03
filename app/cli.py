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
from app.composicao import montar_dependencias
from app.composicao import montar_motor as montar_motor_com
from app.config import Configuracao, obter_configuracao
from app.db import encerrar, obter_engine
from app.logging import configurar_logging


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

    Casca sobre `app.composicao`, que é onde a fiação mora hoje — o WhatsApp
    precisa montar as mesmas peças uma vez e o engine por mensagem.
    """
    deps = montar_dependencias(criar_sessao, config)
    return montar_motor_com(deps, adaptador), deps.cliente


if __name__ == "__main__":
    executar(main())
