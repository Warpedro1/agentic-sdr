"""Entrega ao juridico o que ficou pendente na fila.

    python scripts/flush_outbox.py
    python scripts/flush_outbox.py --falhados     # o que desistiu de tentar

A Marina já tenta entregar sozinha durante a conversa. Este script existe para
dois casos: o juridico estava fora do ar e voltou, ou alguém quer forçar a
entrega agora sem esperar a próxima tentativa.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.aio import executar
from app.config import obter_configuracao
from app.db import encerrar, obter_engine
from app.integrations.juridico.fabrica import (
    IntegracaoDesligada,
    montar_cliente,
    montar_entregador,
)
from app.logging import configurar_logging
from app.outbox import falhados


async def main(args) -> int:
    configurar_logging(destino="")
    config = obter_configuracao()
    criar_sessao = async_sessionmaker(obter_engine(), expire_on_commit=False)

    if args.falhados:
        async with criar_sessao() as sessao:
            itens = await falhados(sessao, config.outbox_max_tentativas)
        if not itens:
            print("Nada falhado. A fila está em dia.")
        for item in itens:
            print(f"\n[{item.tipo}] lead={item.lead_id} tentativas={item.tentativas}")
            print(f"  {item.ultimo_erro}")
        await encerrar()
        return 0

    try:
        cliente = montar_cliente(config)
    except IntegracaoDesligada as erro:
        print(f"Integração desligada: {erro}")
        return 1

    try:
        entregador = montar_entregador(criar_sessao, cliente, config)
        resumo = await entregador.drenar(limite=args.limite)
    finally:
        await cliente.fechar()

    print(
        f"Entregues: {resumo['entregues']} · "
        f"falhas: {resumo['falhas']} · "
        f"adiados: {resumo['adiados']}"
    )
    if resumo["falhas"]:
        print("Veja o motivo com: python scripts/flush_outbox.py --falhados")

    await encerrar()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Drena a fila do CRM externo.")
    parser.add_argument("--limite", type=int, default=200)
    parser.add_argument(
        "--falhados", action="store_true", help="lista o que desistiu de tentar"
    )
    sys.exit(executar(main(parser.parse_args())))
