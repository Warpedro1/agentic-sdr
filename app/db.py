"""Conexao com o Postgres.

Engine e sessionmaker sao criados sob demanda para que importar `app.models` (nos
testes da maquina, por exemplo) nunca abra conexao.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import obter_configuracao

_engine: AsyncEngine | None = None
_engine_de_travas: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def obter_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            obter_configuracao().database_url,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def obter_engine_de_travas() -> AsyncEngine:
    """Pool so para as travas por identidade do `Engine`.

    A trava segura a conexao pelo turno inteiro, e o turno ainda precisa de outra
    para trabalhar. No mesmo pool, turnos simultaneos de numeros diferentes prendiam
    todas as conexoes nas travas e esperavam para sempre pela de trabalho. Separado,
    o pior caso e o turno excedente esperar a vez — por isso sem overflow e com
    espera longa.
    """
    global _engine_de_travas
    if _engine_de_travas is None:
        config = obter_configuracao()
        _engine_de_travas = create_async_engine(
            config.database_url,
            pool_pre_ping=True,
            pool_size=config.travas_pool,
            max_overflow=0,
            pool_timeout=300,
        )
    return _engine_de_travas


def obter_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(obter_engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def sessao() -> AsyncIterator[AsyncSession]:
    async with obter_sessionmaker()() as s:
        yield s


async def encerrar() -> None:
    global _engine, _engine_de_travas, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    if _engine_de_travas is not None:
        await _engine_de_travas.dispose()
    _engine = None
    _engine_de_travas = None
    _sessionmaker = None
