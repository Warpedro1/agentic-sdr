"""Formatacao de data em portugues, sem depender de locale.

`locale.setlocale(LC_TIME, "pt_BR")` funciona no Linux e falha em boa parte das
instalacoes do Windows. Como a data aparece na mensagem que o lead le e no
snapshot do teste e2e, ela precisa sair igual em qualquer maquina.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

DIAS_SEMANA = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)

MESES = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)


def para_local(momento: datetime, timezone_exibicao: str) -> datetime:
    return momento.astimezone(ZoneInfo(timezone_exibicao))


def data_extenso(momento: datetime, timezone_exibicao: str) -> str:
    """Ex.: 'quinta-feira, 27 de agosto'."""
    local = para_local(momento, timezone_exibicao)
    return f"{DIAS_SEMANA[local.weekday()]}, {local.day} de {MESES[local.month - 1]}"


def hora(momento: datetime, timezone_exibicao: str) -> str:
    return para_local(momento, timezone_exibicao).strftime("%H:%M")
