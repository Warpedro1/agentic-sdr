"""Ponto de entrada da Marina no WhatsApp.

    python -m app.whatsapp

Sobe o servidor que recebe os webhooks — da Twilio, da Meta e da Z-API, que
convivem. Monta as dependencias uma vez e entrega ao webhook, que cria um engine por
mensagem. Nenhuma regra de conversa mora aqui, como no `cli.py`.
"""

from __future__ import annotations

import uvicorn
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.aio import executar
from app.channels.whatsapp_meta import MetaWhatsApp
from app.channels.whatsapp_twilio import TwilioWhatsApp
from app.channels.whatsapp_zapi import ZApiWhatsApp
from app.composicao import montar_dependencias
from app.config import Configuracao, obter_configuracao
from app.db import encerrar, obter_engine
from app.empresas import PREFIXO_TOKEN_ZAPI, EmpresaCtx, listar_ativas, slug_para_env
from app.logging import configurar_logging, obter_logger
from app.web.webhook import criar_app

logger = obter_logger("whatsapp")


def _provedores(config: Configuracao) -> dict[str, bool]:
    """Quais provedores tem o minimo para funcionar.

    O minimo e o par que deixa conferir a assinatura E responder: sem o segredo
    que assina, todo webhook vira 403; sem a credencial de envio, a Marina atende
    e ninguem recebe a resposta.

    Na Z-API o minimo e o segredo do endereco: o token de envio e de cada empresa, e
    a falta dele vira aviso por empresa (`_avisar_empresas_da_zapi`).
    """
    return {
        "twilio": bool(config.twilio_account_sid and config.twilio_auth_token),
        "meta": bool(config.meta_access_token and config.meta_app_secret),
        "zapi": bool(config.zapi_webhook_segredo),
    }


def _avisar_configuracao_pela_metade(config: Configuracao) -> None:
    """Configuracao incompleta nao impede subir, mas vira aviso no boot.

    Sem isto a descoberta seria pelo sintoma — um 403 atras do outro no log, sem
    nada dizendo que faltou uma variavel.
    """
    if config.meta_access_token and not config.meta_app_secret:
        logger.warning(
            "meta_sem_chave_do_app",
            efeito="toda chamada da Meta sera recusada com 403",
            corrigir="preencha META_APP_SECRET (Configuracoes > Basico, no app da Meta)",
        )
    if config.meta_access_token and not config.meta_verify_token:
        logger.warning(
            "meta_sem_token_de_verificacao",
            efeito="a Meta nao conseguira validar o endereco do webhook",
        )
    if config.zapi_webhook_segredo and not config.zapi_client_token:
        logger.warning(
            "zapi_sem_client_token",
            efeito="se a conta exigir o token de seguranca, todo envio pela Z-API sera recusado",
            corrigir="preencha ZAPI_CLIENT_TOKEN (painel da Z-API > Seguranca)",
        )


def _avisar_empresas_da_zapi(empresas: list[EmpresaCtx]) -> None:
    """Instancia sem token e o pior caso silencioso: a Marina recebe e nao responde."""
    if not any(e.zapi_instancia_id for e in empresas):
        logger.warning(
            "nenhuma_empresa_com_instancia_zapi",
            corrigir="scripts/cadastrar_empresa.py --atualizar --zapi-instancia <id>",
        )
    for empresa in empresas:
        if empresa.zapi_instancia_id and not empresa.zapi_token:
            logger.warning(
                "zapi_sem_token_da_instancia",
                empresa=empresa.slug,
                efeito="a Marina recebe as mensagens e nao consegue responder",
                corrigir=f"preencha {slug_para_env(empresa.slug, PREFIXO_TOKEN_ZAPI)} no .env",
            )


async def main() -> None:
    configurar_logging()
    config = obter_configuracao()

    provedores = _provedores(config)
    if not any(provedores.values()):
        # Falhar aqui e melhor do que subir e recusar todo webhook por assinatura
        # invalida, que e o sintoma mais confuso possivel.
        raise SystemExit(
            "Nenhum provedor de WhatsApp configurado. Preencha TWILIO_ACCOUNT_SID e "
            "TWILIO_AUTH_TOKEN, ou META_ACCESS_TOKEN e META_APP_SECRET, ou "
            "ZAPI_WEBHOOK_SEGREDO. Ver .env.example."
        )
    _avisar_configuracao_pela_metade(config)

    criar_sessao = async_sessionmaker(obter_engine(), expire_on_commit=False)
    deps = montar_dependencias(criar_sessao, config)
    twilio = TwilioWhatsApp(config)
    meta = MetaWhatsApp(config)
    zapi = ZApiWhatsApp(config)

    # Quais numeros estao de fato atendidos, e por qual caminho. Mensagem para
    # numero fora desta lista e ignorada com aviso, e sem isto no boot a descoberta
    # seria pelo log de uma mensagem perdida.
    async with criar_sessao() as sessao:
        empresas = await listar_ativas(sessao)
    logger.info(
        "whatsapp_iniciando",
        host=config.whatsapp_host,
        porta=config.whatsapp_porta,
        url_publica=config.twilio_url_publica,
        provedores=provedores,
        empresas=[
            {
                "slug": e.slug,
                "numero": e.whatsapp_numero or "(sem numero)",
                "numero_id_meta": e.whatsapp_numero_id or "(sem id)",
                "instancia_zapi": e.zapi_instancia_id or "(sem instancia)",
            }
            for e in empresas
        ],
    )
    if not empresas:
        logger.warning("nenhuma_empresa_cadastrada")
    elif not any(
        e.whatsapp_numero or e.whatsapp_numero_id or e.zapi_instancia_id for e in empresas
    ):
        logger.warning("nenhuma_empresa_com_numero")
    if provedores["meta"] and not any(e.whatsapp_numero_id for e in empresas):
        logger.warning(
            "nenhuma_empresa_com_numero_da_meta",
            corrigir="scripts/cadastrar_empresa.py --atualizar --whatsapp-id <phone_number_id>",
        )
    if provedores["zapi"]:
        _avisar_empresas_da_zapi(empresas)

    servidor = uvicorn.Server(
        uvicorn.Config(
            criar_app(deps, twilio, meta, zapi),
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
        await twilio.fechar()
        await meta.fechar()
        await zapi.fechar()
        await deps.fechar()
        await encerrar()


if __name__ == "__main__":
    executar(main())
