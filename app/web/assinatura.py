"""Autenticidade dos webhooks — Twilio, Meta e Z-API.

Os tres caminhos de entrada tem a MESMA primeira porta: sem prova de origem, nada
acontece. O que muda e o material que prova, e vale conhecer a diferenca:

- Twilio assina a URL somada aos campos do formulario (HMAC-SHA1).
- Meta assina o CORPO CRU (HMAC-SHA256). Por isso o corpo precisa ser lido em
  bytes e conferido ANTES de virar JSON: reserializar muda o texto e derruba a
  assinatura.
- Z-API nao assina nada. A prova e um segredo no endereco do webhook, que so ela
  conhece — mais fraco que assinatura, e por isso nunca escrito em log.

--- o modulo original ---

Assinatura dos webhooks da Twilio.

Sem conferir a assinatura de cada chamada, qualquer um que descubra o endereco
publico consegue inventar mensagens — e criar lead e reuniao falsos no juridico.
E a fronteira de seguranca deste canal.

A validacao e do SDK de proposito: a documentacao da Twilio e explicita em nao
implementar a sua ("Don't implement your own signature validation"), e a lista de
parametros que entram no calculo muda sem aviso.

Fora de FastAPI para poder ser testado sozinho.
"""

from __future__ import annotations

import hashlib
import hmac
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


def valida_meta(config: Configuracao, corpo: bytes, assinatura: str | None) -> bool:
    """Confere `X-Hub-Signature-256` (HMAC-SHA256 do corpo cru, com a chave do app).

    Implementada a mao, e nao por biblioteca, porque o calculo e uma linha e a Meta
    o documenta de forma estavel — ao contrario da Twilio, onde a lista de
    parametros que entram no calculo muda sem aviso.

    A comparacao usa `compare_digest`: comparar com `==` vaza, pelo tempo de
    resposta, quantos bytes iniciais o atacante acertou.
    """
    if not config.meta_validar_assinatura:
        # So o teste desliga. Em producao isto abre a porta para qualquer um.
        logger.warning("assinatura_nao_conferida", origem="meta")
        return True

    if not assinatura:
        return False
    if not config.meta_app_secret:
        # Sem a chave nao ha como validar, e aceitar seria pior do que recusar.
        logger.error("assinatura_sem_chave", origem="meta")
        return False

    esperado = hmac.new(
        config.meta_app_secret.encode("utf-8"), corpo, hashlib.sha256
    ).hexdigest()
    recebido = assinatura.removeprefix("sha256=").strip()
    return hmac.compare_digest(esperado, recebido)


def valida_zapi(config: Configuracao, segredo: str) -> bool:
    """Confere o segredo que veio no endereco do webhook da Z-API.

    A Z-API nao assina as chamadas nem manda cabecalho de autenticacao: o unico
    material que prova a origem e o endereco, que so ela conhece. Quem vir o
    endereco consegue forjar uma chamada, e por isso o segredo e mascarado em todo
    log (`app/logging.py`) e deve ser longo e aleatorio.

    Sem segredo configurado, recusa — aceitar seria abrir o endereco para todos.
    Nao ha chave para desligar a conferencia, nem para teste: o teste configura um.
    """
    esperado = config.zapi_webhook_segredo
    if not esperado:
        logger.error("segredo_sem_configuracao", origem="zapi")
        return False
    return hmac.compare_digest(
        esperado.encode("utf-8"), (segredo or "").encode("utf-8")
    )
