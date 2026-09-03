"""Ponto de entrada da Marina no WhatsApp.

    python -m app.whatsapp

Sobe o servidor que recebe os webhooks da Twilio. Monta as dependencias uma vez
e entrega ao webhook, que cria um engine por mensagem. Nenhuma regra de conversa
mora aqui, como no `cli.py`.
"""

from __future__ import annotations

import uvicorn
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.aio import executar
from app.channels.whatsapp_twilio import TwilioWhatsApp
from app.composicao import montar_dependencias
from app.config import obter_configuracao
from app.db import encerrar, obter_engine
from app.logging import configurar_logging, obter_logger
from app.web.webhook import criar_app

logger = obter_logger("whatsapp")


async def main() -> None:
    configurar_logging()
    config = obter_configuracao()

    if not config.twilio_account_sid or not config.twilio_auth_token:
        # Falhar aqui e melhor do que subir e recusar todo webhook por assinatura
        # invalida, que e o sintoma mais confuso possivel.
        raise SystemExit(
            "TWILIO_ACCOUNT_SID e TWILIO_AUTH_TOKEN sao obrigatorios. "
            "Ver .env.example e docs/whatsapp/02-integracao-twilio.md."
        )

    criar_sessao = async_sessionmaker(obter_engine(), expire_on_commit=False)
    deps = montar_dependencias(criar_sessao, config)
    cliente = TwilioWhatsApp(config)

    logger.info(
        "whatsapp_iniciando",
        host=config.whatsapp_host,
        porta=config.whatsapp_porta,
        url_publica=config.twilio_url_publica,
        remetente=config.twilio_remetente,
    )

    servidor = uvicorn.Server(
        uvicorn.Config(
            criar_app(deps, cliente),
            host=config.whatsapp_host,
            port=config.whatsapp_porta,
            log_config=None,  # o structlog ja cuida do log
        )
    )
    try:
        # `serve()` e corrotina: roda no loop que `executar` criou. `uvicorn.run`
        # criaria um loop proprio, e no Windows seria o Proactor — em que o
        # psycopg async nao conecta.
        await servidor.serve()
    finally:
        await cliente.fechar()
        await deps.fechar()
        await encerrar()


if __name__ == "__main__":
    executar(main())
