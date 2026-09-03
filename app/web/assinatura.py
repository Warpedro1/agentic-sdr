"""Assinatura dos webhooks da Twilio.

Sem conferir a assinatura de cada chamada, qualquer um que descubra o endereco
publico consegue inventar mensagens — e criar lead e reuniao falsos no juridico.
E a fronteira de seguranca deste canal.

A validacao e do SDK de proposito: a documentacao da Twilio e explicita em nao
implementar a sua ("Don't implement your own signature validation"), e a lista de
parametros que entram no calculo muda sem aviso.

Fora de FastAPI para poder ser testado sozinho.
"""

from __future__ import annotations

from collections.abc import Mapping

from twilio.request_validator import RequestValidator

from app.config import Configuracao
from app.logging import obter_logger

logger = obter_logger("webhook")


def url_do_webhook(config: Configuracao, caminho: str, query: str = "") -> str:
    """A URL que a Twilio usou para assinar.

    Montada a partir de `twilio_url_publica`, e nao dos cabecalhos da requisicao:
    e o endereco exato configurado no Console, nao depende de `X-Forwarded-*` (que
    o cliente pode forjar) e nao passa por reencode — qualquer diferenca de
    escrita, inclusive a barra final, invalida a assinatura.
    """
    base = config.twilio_url_publica.rstrip("/")
    return f"{base}{caminho}?{query}" if query else f"{base}{caminho}"


def valida(
    config: Configuracao,
    url: str,
    parametros: Mapping[str, str],
    assinatura: str | None,
) -> bool:
    """Confere `X-Twilio-Signature` (HMAC-SHA1 com o auth token)."""
    if not config.twilio_validar_assinatura:
        # So o teste desliga. Em producao isto abre a porta para qualquer um.
        logger.warning("assinatura_nao_conferida", url=url)
        return True

    if not assinatura:
        return False
    if not config.twilio_auth_token:
        # Sem token nao ha como validar, e aceitar seria pior do que recusar.
        logger.error("assinatura_sem_token")
        return False

    return RequestValidator(config.twilio_auth_token).validate(
        url, dict(parametros), assinatura
    )
