"""Logging estruturado em JSON.

Uma linha JSON por evento, para que a transcricao e as decisoes da Marina possam ser
auditadas depois sem parsing de texto livre.
"""

import logging
import sys

import structlog

from app.config import obter_configuracao


def configurar_logging() -> None:
    config = obter_configuracao()
    nivel = getattr(logging, config.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=nivel)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(nivel),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def obter_logger(nome: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(nome)
