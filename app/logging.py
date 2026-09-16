"""Logging estruturado em JSON.

Uma linha JSON por evento, para que a transcricao e as decisoes da Marina possam ser
auditadas depois sem parsing de texto livre.
"""

import logging
import re
import sys

import structlog

from app.config import obter_configuracao

#: Pedacos de URL que carregam segredo. Duas bibliotecas escrevem URL inteira no log
#: sem ninguem pedir: o httpx registra toda chamada que a Marina FAZ — e na Z-API o
#: token da instancia vai no caminho (`/token/<token>/send-text`) —, e o uvicorn
#: registra toda chamada RECEBIDA — o webhook da Z-API tem o segredo no caminho, e o
#: aperto de mao da Meta traz o token de verificacao na query.
_SEGREDOS_EM_URL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(/token/)[^/?\s\"']+"), r"\1***"),
    (re.compile(r"(/webhooks/zapi/)[^/?\s\"']+"), r"\1***"),
    (re.compile(r"(hub[._]verify_token=)[^&\s\"']+"), r"\1***"),
)


def mascarar_segredos(texto: str) -> str:
    for padrao, troca in _SEGREDOS_EM_URL:
        texto = padrao.sub(troca, texto)
    return texto


class MascaraSegredos(logging.Filter):
    """Reescreve a linha ja formatada antes de ela chegar ao destino.

    Fica no HANDLER, e nao num logger especifico: assim vale para o que vier de
    qualquer biblioteca, inclusive das que ainda nao sabemos que escrevem URL.
    Formatar aqui (`getMessage`) e necessario porque o httpx passa a URL como objeto
    nos argumentos, e nao como texto.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            mensagem = record.getMessage()
        except Exception:  # noqa: BLE001 - registro malformado segue como veio
            return True
        mascarada = mascarar_segredos(mensagem)
        if mascarada != mensagem:
            record.msg = mascarada
            record.args = None
        return True


def configurar_logging(destino: str | None = None) -> None:
    """Configura o log JSON.

    `destino` e um caminho de arquivo; vazio manda para o terminal. O padrao e
    arquivo porque, no CLI, log no terminal se intercala com a conversa e deixa
    o atendimento ilegivel para quem esta falando com a Marina.
    """
    config = obter_configuracao()
    nivel = getattr(logging, config.log_level.upper(), logging.INFO)
    caminho = config.log_arquivo if destino is None else destino

    if caminho:
        logging.basicConfig(
            format="%(message)s",
            level=nivel,
            handlers=[logging.FileHandler(caminho, encoding="utf-8")],
            force=True,
        )
    else:
        logging.basicConfig(
            format="%(message)s", stream=sys.stderr, level=nivel, force=True
        )
    # `force=True` recria os handlers a cada chamada, entao o filtro nao se acumula.
    for handler in logging.getLogger().handlers:
        handler.addFilter(MascaraSegredos())

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
