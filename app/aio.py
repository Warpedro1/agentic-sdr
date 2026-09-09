"""Contornos de Windows compartilhados por todo ponto de entrada.

Sao dois, e os dois se descobrem do jeito dificil: o event loop que o psycopg
aceita, e o console que nao fala UTF-8.

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


def forcar_utf8() -> None:
    """O console do Windows nao usa UTF-8 por padrao.

    Sem isto "Ola" sai correto mas "Voce" e "cartao" saem quebrados — e tudo
    que este projeto imprime e em portugues.
    """
    for fluxo in (sys.stdout, sys.stdin):
        if hasattr(fluxo, "reconfigure"):
            try:
                fluxo.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
