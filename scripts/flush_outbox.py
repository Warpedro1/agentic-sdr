"""Entrega ao juridico o que ficou pendente na fila.

    python scripts/flush_outbox.py                # todas as empresas, uma a uma
    python scripts/flush_outbox.py --empresa acme # só uma
    python scripts/flush_outbox.py --falhados     # o que desistiu de tentar
    python scripts/flush_outbox.py --reprocessar  # dá nova chance ao que desistiu

A Marina já tenta entregar sozinha durante a conversa. Este script existe para
dois casos: o juridico estava fora do ar e voltou, ou alguém quer forçar a
entrega agora sem esperar a próxima tentativa.

**Cada empresa tem a sua instalação do juridico**, então a drenagem é por empresa:
sem `--empresa` ele percorre todas as ativas, montando um entregador para cada.
Não existe drenagem "geral" — um item só pode ser entregue no CRM da empresa dele.

`--reprocessar` existe por um caso real: um item que esgotou as tentativas ficava
sem NENHUM caminho de volta — a fila só olha quem ainda tem tentativa sobrando,
então corrigir o problema não bastava. Use depois de arrumar a causa (a credencial
no .env, o endereço do juridico, as permissões do usuário de serviço).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.aio import executar, forcar_utf8
from app.config import obter_configuracao
from app.db import encerrar, obter_engine
from app.empresas import (
    EmpresaCtx,
    EmpresaDesconhecida,
    listar_ativas,
    resolver_argumento,
)
from app.integrations.juridico.fabrica import (
    IntegracaoDesligada,
    montar_cliente,
    montar_entregador,
)
from app.logging import configurar_logging
from app.models import CrmOutbox, Empresa
from app.outbox import falhados


async def _listar_falhados(criar_sessao, config, empresa: EmpresaCtx | None) -> int:
    """Mostra o que desistiu. Sem `--empresa`, mostra de todas.

    De propósito: quem opera quer ver o estrago inteiro de uma vez, e restringir
    por padrão esconderia metade do problema.
    """
    async with criar_sessao() as sessao:
        itens = await falhados(
            sessao,
            config.outbox_max_tentativas,
            empresa.id if empresa else None,
        )
        nomes = {
            linha.id: linha.slug
            for linha in await sessao.scalars(select(Empresa))
        }

    if not itens:
        print("Nada falhado. A fila está em dia.")
        return 0

    for item in itens:
        dono = nomes.get(item.empresa_id, "(sem empresa)")
        print(f"\n[{dono}] [{item.tipo}] lead={item.lead_id} tentativas={item.tentativas}")
        print(f"  {item.ultimo_erro}")
    print()
    return 0


async def _reprocessar(criar_sessao, empresa: EmpresaCtx | None) -> None:
    """Zera tentativas E antecipa a próxima.

    Sem a segunda parte, "reprocessar" logo depois de uma falha não faria nada e
    pareceria que o item sumiu — quem está só esperando o backoff continuaria
    esperando.
    """
    criterios = [CrmOutbox.entregue_em.is_(None)]
    if empresa is not None:
        criterios.append(CrmOutbox.empresa_id == empresa.id)

    async with criar_sessao() as sessao:
        espera = (await sessao.execute(select(CrmOutbox).where(*criterios))).scalars().all()
        for item in espera:
            item.tentativas = 0
            item.proxima_tentativa_em = datetime.now(UTC)
            item.ultimo_erro = None
        await sessao.commit()
    alcance = f"de {empresa.slug}" if empresa else "de todas as empresas"
    print(f"{len(espera)} item(ns) {alcance} voltaram para a fila.")


async def _drenar_uma(criar_sessao, config, empresa: EmpresaCtx, limite: int) -> dict:
    """Uma empresa, um cliente, um entregador. Nunca compartilhados."""
    try:
        cliente = montar_cliente(empresa, config)
    except IntegracaoDesligada as erro:
        print(f"[{empresa.slug}] integração desligada: {erro}")
        return {"entregues": 0, "falhas": 0, "adiados": 0}

    try:
        entregador = montar_entregador(criar_sessao, cliente, empresa, config)
        return await entregador.drenar(limite=limite)
    finally:
        await cliente.fechar()


async def main(args) -> int:
    forcar_utf8()
    configurar_logging(destino="")
    config = obter_configuracao()
    criar_sessao = async_sessionmaker(obter_engine(), expire_on_commit=False)

    try:
        empresa: EmpresaCtx | None = None
        if args.empresa:
            async with criar_sessao() as sessao:
                try:
                    empresa = await resolver_argumento(sessao, args.empresa)
                except EmpresaDesconhecida as erro:
                    print(erro)
                    return 2

        if args.falhados:
            return await _listar_falhados(criar_sessao, config, empresa)

        if args.reprocessar:
            await _reprocessar(criar_sessao, empresa)

        async with criar_sessao() as sessao:
            alvos = [empresa] if empresa else await listar_ativas(sessao)
        if not alvos:
            print("Nenhuma empresa cadastrada. Rode scripts/cadastrar_empresa.py.")
            return 1

        total = {"entregues": 0, "falhas": 0, "adiados": 0}
        for alvo in alvos:
            resumo = await _drenar_uma(criar_sessao, config, alvo, args.limite)
            if len(alvos) > 1:
                print(
                    f"[{alvo.slug}] entregues: {resumo['entregues']} · "
                    f"falhas: {resumo['falhas']} · adiados: {resumo['adiados']}"
                )
            for chave in total:
                total[chave] += resumo[chave]

        print(
            f"\nTotal — entregues: {total['entregues']} · "
            f"falhas: {total['falhas']} · adiados: {total['adiados']}"
        )
        if total["falhas"]:
            print("Veja o motivo com: python scripts/flush_outbox.py --falhados")
        return 0
    finally:
        await encerrar()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Drena a fila do CRM externo.")
    parser.add_argument("--limite", type=int, default=200)
    parser.add_argument(
        "--empresa", help="slug da empresa; sem isto, percorre todas as ativas"
    )
    parser.add_argument(
        "--falhados", action="store_true", help="lista o que desistiu de tentar"
    )
    parser.add_argument(
        "--reprocessar",
        action="store_true",
        help="zera as tentativas do que desistiu e tenta entregar de novo",
    )
    sys.exit(executar(main(parser.parse_args())))
