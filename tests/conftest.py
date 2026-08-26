"""Infraestrutura dos testes que precisam de Postgres.

Os testes da maquina, dos parsers e dos guardrails NAO passam por aqui — eles
rodam sem banco nenhum, e e assim que devem continuar.

Quem precisa de banco usa a fixture `sessao_db`. Se o Postgres nao estiver no ar,
esses testes sao pulados com uma mensagem clara, em vez de falharem com erro de
conexao e esconderem um problema real.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql import text

from app.config import obter_configuracao
from app.models import Base

BANCO_TESTE = "marina_test"


def _url_teste() -> str:
    base = obter_configuracao().database_url
    return base.rsplit("/", 1)[0] + f"/{BANCO_TESTE}"


def _url_admin() -> str:
    base = obter_configuracao().database_url
    return base.rsplit("/", 1)[0] + "/postgres"


@pytest_asyncio.fixture(scope="session")
async def engine_teste():
    admin = create_async_engine(_url_admin(), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conexao:
            existe = await conexao.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :nome"),
                {"nome": BANCO_TESTE},
            )
            if not existe:
                await conexao.execute(text(f'CREATE DATABASE "{BANCO_TESTE}"'))
    except Exception as erro:  # noqa: BLE001 - queremos pular, nao explodir
        pytest.skip(
            f"Postgres indisponivel ({type(erro).__name__}). "
            "Suba com: docker compose up -d",
            allow_module_level=True,
        )
    finally:
        await admin.dispose()

    engine = create_async_engine(_url_teste())
    async with engine.begin() as conexao:
        await conexao.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def sessao_db(engine_teste):
    """Sessao limpa. As tabelas sao esvaziadas antes de cada teste.

    Nao da para isolar por transacao aqui: `reservar` faz commit de proposito (a
    trava so vale se a transacao existir de verdade), entao a limpeza e por
    TRUNCATE.
    """
    tabelas = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    async with engine_teste.begin() as conexao:
        await conexao.execute(text(f"TRUNCATE {tabelas} RESTART IDENTITY CASCADE"))

    criar_sessao = async_sessionmaker(engine_teste, expire_on_commit=False)
    async with criar_sessao() as sessao:
        yield sessao


@pytest_asyncio.fixture
async def criar_sessao_db(engine_teste, sessao_db):
    """Fabrica de sessoes independentes, para testar concorrencia de verdade."""
    return async_sessionmaker(engine_teste, expire_on_commit=False)
