"""Event loop compativel com o psycopg.

No Windows o loop padrao do asyncio e o Proactor, e o psycopg em modo async nao
funciona nele — a conexao falha com InterfaceError antes de qualquer query. A
correcao e usar o SelectorEventLoop.

Todo ponto de entrada assincrono do projeto (alembic, seed, cli, scripts) deve
chamar `executar` em vez de `asyncio.run`, para nao repetir esse detalhe — e
para que ninguem descubra do jeito dificil de novo.
"""

from __future__ import annotations

import asyncio
import selectors
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def loop_factory() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


def executar(coro: Coroutine[Any, Any, T]) -> T:
    """asyncio.run() com o loop certo para o psycopg."""
    return asyncio.run(coro, loop_factory=loop_factory)
