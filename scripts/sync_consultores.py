"""Importa os consultores do juridico para a Marina.

    python scripts/sync_consultores.py

Por que importar em vez de cadastrar à mão: os consultores da Marina precisam
ser as pessoas reais que vão aparecer nas reuniões. Manter duas listas separadas
garante que uma hora elas divergem.

O que este script NÃO faz: definir horários de atendimento. O juridico não tem
noção de agenda — lá uma reunião é uma tarefa com data que alguém digitou.
Depois de importar, use `configurar_agenda.py`.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.aio import executar
from app.db import encerrar, obter_engine
from app.integrations.juridico.fabrica import IntegracaoDesligada, montar_cliente
from app.logging import configurar_logging, obter_logger
from app.models import Consultor, RegraDisponibilidade

logger = obter_logger("sync_consultores")


async def main() -> int:
    configurar_logging(destino="")  # script: log no terminal mesmo
    try:
        cliente = montar_cliente()
    except IntegracaoDesligada as erro:
        print(f"Integração desligada: {erro}")
        return 1

    criar_sessao = async_sessionmaker(obter_engine(), expire_on_commit=False)
    criados = atualizados = 0

    try:
        remotos = await cliente.listar_usuarios()
    finally:
        await cliente.fechar()

    if not remotos:
        print("O juridico não devolveu nenhum usuário ativo.")
        await encerrar()
        return 1

    async with criar_sessao() as sessao:
        for remoto in remotos:
            crm_id = str(remoto.get("id") or "")
            nome = str(remoto.get("nome") or "").strip()
            email = str(remoto.get("email") or "").strip().lower()
            if not crm_id or not nome:
                continue

            local = (
                await sessao.execute(
                    select(Consultor).where(Consultor.crm_usuario_id == crm_id)
                )
            ).scalar_one_or_none()

            if local is None and email:
                # Consultor que já existia na Marina antes da integração: liga
                # pelo e-mail em vez de criar um duplicado.
                local = (
                    await sessao.execute(
                        select(Consultor).where(Consultor.email == email)
                    )
                ).scalar_one_or_none()

            if local is None:
                sessao.add(
                    Consultor(
                        nome=nome,
                        email=email or f"{crm_id}@sem-email.local",
                        ativo=True,
                        crm_usuario_id=crm_id,
                    )
                )
                criados += 1
            else:
                local.nome = nome
                local.crm_usuario_id = crm_id
                local.ativo = True
                if email:
                    local.email = email
                atualizados += 1

        await sessao.commit()

        sem_agenda = (
            (
                await sessao.execute(
                    select(Consultor)
                    .outerjoin(
                        RegraDisponibilidade,
                        RegraDisponibilidade.consultor_id == Consultor.id,
                    )
                    .where(Consultor.ativo.is_(True), RegraDisponibilidade.id.is_(None))
                )
            )
            .scalars()
            .unique()
            .all()
        )

    print(f"Consultores criados: {criados} · atualizados: {atualizados}")
    if sem_agenda:
        print()
        print("SEM HORÁRIO DE ATENDIMENTO (não vão receber reunião nenhuma):")
        for consultor in sem_agenda:
            print(f"  - {consultor.nome}")
        print()
        print("Defina com: python scripts/configurar_agenda.py")

    await encerrar()
    return 0


if __name__ == "__main__":
    argparse.ArgumentParser(description="Importa consultores do juridico.").parse_args()
    sys.exit(executar(main()))
