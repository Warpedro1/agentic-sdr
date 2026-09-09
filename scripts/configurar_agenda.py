"""Define os horários de atendimento dos consultores.

    python scripts/configurar_agenda.py                       # todos, seg-sex 9h-18h
    python scripts/configurar_agenda.py --email ana@exemplo.com.br --inicio 13:00
    python scripts/configurar_agenda.py --listar

Este passo é humano e não tem como ser adivinhado: o juridico não guarda em
lugar nenhum quem atende em quais dias. Sem horário definido, um consultor
existe na Marina mas nunca recebe reunião — ele simplesmente não aparece na
lista oferecida ao lead.
"""

from __future__ import annotations

import argparse
import sys
from datetime import time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.aio import executar, forcar_utf8
from app.db import encerrar, obter_engine
from app.empresas import EmpresaDesconhecida, resolver_argumento
from app.logging import configurar_logging
from app.models import Consultor, RegraDisponibilidade

NOMES_DIAS = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


def parse_hora(valor: str) -> time:
    horas, _, minutos = valor.partition(":")
    return time(int(horas), int(minutos or 0))


def parse_dias(valor: str) -> list[int]:
    """'0-4' ou '0,2,4' -> lista de dias (0=segunda)."""
    if "-" in valor:
        inicio, _, fim = valor.partition("-")
        return list(range(int(inicio), int(fim) + 1))
    return [int(parte) for parte in valor.split(",") if parte.strip()]


async def listar(criar_sessao, empresa) -> int:
    async with criar_sessao() as sessao:
        consultores = (
            (
                await sessao.execute(
                    select(Consultor)
                    .where(Consultor.empresa_id == empresa.id)
                    .order_by(Consultor.nome)
                )
            )
            .scalars()
            .all()
        )
        print(f"Empresa: {empresa.slug}\n")
        for consultor in consultores:
            regras = (
                (
                    await sessao.execute(
                        select(RegraDisponibilidade)
                        .where(RegraDisponibilidade.consultor_id == consultor.id)
                        .order_by(RegraDisponibilidade.dia_semana)
                    )
                )
                .scalars()
                .all()
            )
            marca = "" if consultor.ativo else "  (inativo)"
            ligado = " · ligado ao juridico" if consultor.crm_usuario_id else ""
            print(f"\n{consultor.nome} <{consultor.email}>{marca}{ligado}")
            if not regras:
                print("   SEM HORÁRIO — não recebe reunião")
            for regra in regras:
                print(
                    f"   {NOMES_DIAS[regra.dia_semana]} "
                    f"{regra.hora_inicio:%H:%M}-{regra.hora_fim:%H:%M} "
                    f"({regra.duracao_min}min + {regra.buffer_min} de intervalo)"
                )
    return 0


async def main(args) -> int:
    forcar_utf8()
    configurar_logging(destino="")
    criar_sessao = async_sessionmaker(obter_engine(), expire_on_commit=False)

    async with criar_sessao() as sessao:
        try:
            empresa = await resolver_argumento(sessao, args.empresa)
        except EmpresaDesconhecida as erro:
            print(erro)
            await encerrar()
            return 2

    if args.listar:
        codigo = await listar(criar_sessao, empresa)
        await encerrar()
        return codigo

    dias = parse_dias(args.dias)
    inicio = parse_hora(args.inicio)
    fim = parse_hora(args.fim)
    if fim <= inicio:
        print("O fim precisa ser depois do início.")
        return 1

    async with criar_sessao() as sessao:
        # Escopado: o mesmo e-mail pode existir em duas empresas, e apagar a
        # agenda inteira do consultor errado e o tipo de estrago que nao avisa.
        consulta = select(Consultor).where(
            Consultor.empresa_id == empresa.id, Consultor.ativo.is_(True)
        )
        if args.email:
            consulta = consulta.where(Consultor.email == args.email.lower())
        consultores = (await sessao.execute(consulta)).scalars().all()

        if not consultores:
            print("Nenhum consultor ativo encontrado. Rode sync_consultores.py antes.")
            return 1

        for consultor in consultores:
            # Substitui a agenda inteira do consultor: mais previsível do que
            # acumular regras antigas com as novas sem ninguém perceber.
            await sessao.execute(
                delete(RegraDisponibilidade).where(
                    RegraDisponibilidade.consultor_id == consultor.id
                )
            )
            for dia in dias:
                sessao.add(
                    RegraDisponibilidade(
                        empresa_id=empresa.id,
                        consultor_id=consultor.id,
                        dia_semana=dia,
                        hora_inicio=inicio,
                        hora_fim=fim,
                        duracao_min=args.duracao,
                        buffer_min=args.buffer,
                    )
                )
            print(
                f"{consultor.nome}: {'/'.join(NOMES_DIAS[d] for d in dias)} "
                f"{inicio:%H:%M}-{fim:%H:%M}"
            )
        await sessao.commit()

    await encerrar()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Horários de atendimento.")
    parser.add_argument("--listar", action="store_true", help="mostra a agenda atual")
    parser.add_argument(
        "--empresa", default=None, help="slug da empresa; dispensável se só há uma"
    )
    parser.add_argument("--email", default=None, help="um consultor só")
    parser.add_argument("--dias", default="0-4", help="0=segunda; ex.: 0-4 ou 0,2,4")
    parser.add_argument("--inicio", default="09:00")
    parser.add_argument("--fim", default="18:00")
    parser.add_argument("--duracao", type=int, default=60)
    parser.add_argument("--buffer", type=int, default=15)
    sys.exit(executar(main(parser.parse_args())))
