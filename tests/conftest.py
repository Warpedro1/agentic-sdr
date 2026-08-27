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

from app.aio import loop_factory
from app.config import obter_configuracao
from app.models import Base

BANCO_TESTE = "marina_test"


def _url_teste() -> str:
    base = obter_configuracao().database_url
    return base.rsplit("/", 1)[0] + f"/{BANCO_TESTE}"


def _url_admin() -> str:
    base = obter_configuracao().database_url
    return base.rsplit("/", 1)[0] + "/postgres"


def pytest_asyncio_loop_factories():
    """No Windows o psycopg async nao roda no loop padrao (Proactor).

    Mesma correcao de app/aio.py, aplicada ao loop que o pytest-asyncio cria.
    Usa o hook novo: sobrescrever a fixture `event_loop_policy` funciona hoje mas
    esta descontinuado, e `WindowsSelectorEventLoopPolicy` sai no Python 3.16.
    """
    return {"psycopg": loop_factory}


@pytest_asyncio.fixture(scope="session")
async def engine_teste():
    # Sem connect_timeout, um Postgres fora do ar deixa a suite pendurada para
    # sempre em vez de cair no skip logo abaixo.
    admin = create_async_engine(
        _url_admin(),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 5},
    )
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

    engine = create_async_engine(
        _url_teste(), connect_args={"connect_timeout": 5}
    )
    async with engine.begin() as conexao:
        # drop antes do create: `create_all` sozinho nao ALTERA tabela que ja
        # existe, entao uma coluna nova no modelo passaria despercebida e os
        # testes rodariam contra um esquema velho.
        await conexao.run_sync(Base.metadata.drop_all)
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
