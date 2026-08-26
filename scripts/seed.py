"""Popula o banco com os consultores e a agenda semanal deles.

Idempotente: rodar duas vezes nao duplica nada.

    python scripts/seed.py
"""

import asyncio
from datetime import time

from sqlalchemy import select

from app.db import encerrar, sessao
from app.logging import configurar_logging, obter_logger
from app.models import Consultor, RegraDisponibilidade

logger = obter_logger("seed")

CONSULTORES = [
    ("Ana Ribeiro", "ana.ribeiro@exemplo.com.br"),
    ("Bruno Tavares", "bruno.tavares@exemplo.com.br"),
    ("Carla Monteiro", "carla.monteiro@exemplo.com.br"),
]

#: Segunda a sexta (0-4), das 9h as 18h. Reuniao de 60 min com 15 de intervalo,
#: entao os horarios caem de 75 em 75 minutos.
DIAS_UTEIS = range(0, 5)
HORA_INICIO = time(9, 0)
HORA_FIM = time(18, 0)
DURACAO_MIN = 60
BUFFER_MIN = 15


async def main() -> None:
    configurar_logging()
    criados_consultores = 0
    criadas_regras = 0

    async with sessao() as s:
        for nome, email in CONSULTORES:
            consultor = (
                await s.execute(select(Consultor).where(Consultor.email == email))
            ).scalar_one_or_none()

            if consultor is None:
                consultor = Consultor(nome=nome, email=email, ativo=True)
                s.add(consultor)
                await s.flush()
                criados_consultores += 1

            for dia in DIAS_UTEIS:
                existente = (
                    await s.execute(
                        select(RegraDisponibilidade).where(
                            RegraDisponibilidade.consultor_id == consultor.id,
                            RegraDisponibilidade.dia_semana == dia,
                            RegraDisponibilidade.hora_inicio == HORA_INICIO,
                        )
                    )
                ).scalar_one_or_none()
                if existente is not None:
                    continue

                s.add(
                    RegraDisponibilidade(
                        consultor_id=consultor.id,
                        dia_semana=dia,
                        hora_inicio=HORA_INICIO,
                        hora_fim=HORA_FIM,
                        duracao_min=DURACAO_MIN,
                        buffer_min=BUFFER_MIN,
                    )
                )
                criadas_regras += 1

        await s.commit()

    logger.info(
        "seed_concluido",
        consultores_criados=criados_consultores,
        regras_criadas=criadas_regras,
        total_consultores=len(CONSULTORES),
    )
    await encerrar()


if __name__ == "__main__":
    asyncio.run(main())
