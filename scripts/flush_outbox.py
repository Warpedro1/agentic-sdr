"""Entrega ao juridico o que ficou pendente na fila.

    python scripts/flush_outbox.py
    python scripts/flush_outbox.py --falhados     # o que desistiu de tentar
    python scripts/flush_outbox.py --reprocessar  # dá nova chance ao que desistiu

A Marina já tenta entregar sozinha durante a conversa. Este script existe para
dois casos: o juridico estava fora do ar e voltou, ou alguém quer forçar a
entrega agora sem esperar a próxima tentativa.

`--reprocessar` existe por um caso real: um item que esgotou as tentativas ficava
sem NENHUM caminho de volta — a fila só olha quem ainda tem tentativa sobrando,
então corrigir o problema não bastava. Use depois de arrumar a causa (a senha no
.env, um módulo faltando, a empresa do usuário de serviço).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from sqlalchemy import select
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
from app.models import CrmOutbox
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

    if args.reprocessar:
        # Zera tentativas E antecipa a próxima tentativa: quem desistiu volta
        # para a fila, e quem está só esperando o backoff é tentado agora. Sem a
        # segunda parte, "reprocessar" logo depois de uma falha não faria nada e
        # pareceria que o item sumiu.
        async with criar_sessao() as sessao:
            espera = (
                await sessao.execute(
                    select(CrmOutbox).where(CrmOutbox.entregue_em.is_(None))
                )
            ).scalars().all()
            for item in espera:
                item.tentativas = 0
                item.proxima_tentativa_em = datetime.now(UTC)
                item.ultimo_erro = None
            await sessao.commit()
        print(f"{len(espera)} item(ns) voltaram para a fila.")

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
    parser.add_argument(
        "--reprocessar",
        action="store_true",
        help="zera as tentativas do que desistiu e tenta entregar de novo",
    )
    sys.exit(executar(main(parser.parse_args())))
