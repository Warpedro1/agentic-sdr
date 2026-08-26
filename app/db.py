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
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
