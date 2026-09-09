"""Ponto de entrada da Marina no terminal.

    python -m app.cli
    python -m app.cli --empresa acme

Monta as dependencias e entrega ao engine. Nenhuma regra de conversa mora aqui:
trocar este arquivo por um adaptador de WhatsApp nao muda o fluxo.

No WhatsApp a empresa vem do numero que recebeu a mensagem. Aqui nao existe numero,
entao ela vem do argumento — ou e assumida, quando so ha uma cadastrada.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.aio import executar, forcar_utf8
from app.channels.terminal import TerminalAdapter
from app.composicao import Dependencias, montar_dependencias
from app.composicao import montar_motor as montar_motor_com
from app.config import Configuracao, obter_configuracao
from app.db import encerrar, obter_engine
from app.empresas import EmpresaCtx, EmpresaDesconhecida, resolver_argumento
from app.logging import configurar_logging


def montar_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Conversa com a Marina no terminal.")
    parser.add_argument(
        "--empresa",
        help="slug da empresa que atende; dispensavel quando so ha uma cadastrada",
    )
    return parser.parse_args()


async def main() -> None:
    forcar_utf8()
    args = montar_argumentos()
    configurar_logging()
    config = obter_configuracao()

    criar_sessao = async_sessionmaker(obter_engine(), expire_on_commit=False)
    deps = montar_dependencias(criar_sessao, config)

    try:
        async with criar_sessao() as sessao:
            empresa = await resolver_argumento(sessao, args.empresa)
    except EmpresaDesconhecida as erro:
        print(f"\n{erro}")
        await encerrar()
        sys.exit(2)

    motor = montar_motor_com(deps, TerminalAdapter(), empresa)
    try:
        conversa_id = await motor.rodar()
        print(f"\n[conversa {conversa_id}]")
    finally:
        await deps.fechar()
        await encerrar()


def montar_motor(
    criar_sessao, adaptador, config: Configuracao, empresa: EmpresaCtx
) -> tuple[object, Dependencias]:
    """Monta o engine com as dependências que a configuração pedir.

    Separado de `main` para poder ser testado: a fiação do CLI já quebrou uma vez
    sem nenhum teste perceber, porque nada aqui era exercitado.

    Casca sobre `app.composicao`, que é onde a fiação mora hoje — o WhatsApp
    precisa montar as mesmas peças uma vez e o engine por mensagem.

    Devolve as dependências junto porque é quem sabe fechar os clientes: agora há
    um por empresa, e não mais um só.
    """
    deps = montar_dependencias(criar_sessao, config)
    return montar_motor_com(deps, adaptador, empresa), deps


if __name__ == "__main__":
    executar(main())
