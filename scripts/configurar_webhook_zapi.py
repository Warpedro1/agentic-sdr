"""Confere a instância da Z-API de uma empresa e aponta os webhooks para a Marina.

    python scripts/configurar_webhook_zapi.py --empresa leve --url-publica https://abc.trycloudflare.com
    python scripts/configurar_webhook_zapi.py --empresa leve --so-status

O endereço do túnel muda a cada reinício, e a Z-API precisa do novo. Este script faz
as duas coisas que andam sempre juntas: conferir se o número está conectado e gravar
o endereço em todos os webhooks da instância (`update-every-webhooks`).

Nunca imprime o token da instância, o token da conta nem o segredo do webhook.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.aio import executar, forcar_utf8
from app.channels.whatsapp_zapi import ZApiWhatsApp
from app.config import obter_configuracao
from app.db import encerrar, obter_engine
from app.empresas import (
    PREFIXO_TOKEN_ZAPI,
    EmpresaDesconhecida,
    resolver_argumento,
    slug_para_env,
)
from app.logging import configurar_logging


def montar_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--empresa", help="slug da empresa (obrigatório com mais de uma)")
    parser.add_argument(
        "--url-publica", help="endereço HTTPS público da Marina, sem barra no fim"
    )
    parser.add_argument(
        "--so-status", action="store_true", help="só confere a conexão; não altera nada"
    )
    return parser.parse_args()


async def main() -> int:
    forcar_utf8()
    args = montar_argumentos()
    configurar_logging(destino="")  # script: log no terminal, já mascarado
    config = obter_configuracao()

    criar_sessao = async_sessionmaker(obter_engine(), expire_on_commit=False)
    try:
        async with criar_sessao() as sessao:
            empresa = await resolver_argumento(sessao, args.empresa)
    except EmpresaDesconhecida as erro:
        print(erro)
        return 2
    finally:
        await encerrar()

    if not empresa.zapi_instancia_id:
        print(f"A empresa '{empresa.slug}' não tem instância da Z-API. Cadastre com:")
        print(
            f"  python scripts/cadastrar_empresa.py --slug {empresa.slug} "
            "--atualizar --zapi-instancia <id>"
        )
        return 2
    if not empresa.zapi_token:
        variavel = slug_para_env(empresa.slug, PREFIXO_TOKEN_ZAPI)
        print(f"Token da instância ausente. Ponha no .env:\n\n  {variavel}=<token>\n")
        return 2
    if not config.zapi_client_token:
        print("! ZAPI_CLIENT_TOKEN vazio: se a conta exigir o token de segurança,")
        print("  a Z-API recusa todas as chamadas.")

    zapi = ZApiWhatsApp(config)
    instancia, token = empresa.zapi_instancia_id, empresa.zapi_token
    try:
        codigo, status = await zapi.consultar(instancia, token, "status")
        print(f"\nInstância da empresa '{empresa.slug}'")
        if codigo != 200 or "connected" not in status:
            motivo = status.get("error") or status or "resposta ilegível"
            print(f"  status        recusado — HTTP {codigo}: {motivo}")
            return 1

        conectada = bool(status.get("connected"))
        print(f"  conectada     {'sim' if conectada else 'NÃO'}")
        celular = "com internet" if status.get("smartphoneConnected") else "sem internet"
        print(f"  celular       {celular}")
        if status.get("error"):
            print(f"  situação      {status['error']}")

        if conectada:
            codigo, aparelho = await zapi.consultar(instancia, token, "device")
            if codigo == 200:
                print(f"  número        {aparelho.get('phone') or '(não informado)'}")
                if aparelho.get("name"):
                    print(f"  nome          {aparelho['name']}")
        else:
            print("\n  Conecte o número: no painel da Z-API, abra a instância e mostre o")
            print("  QR code; no celular do número, WhatsApp > Aparelhos conectados >")
            print("  Conectar aparelho.")

        if args.so_status:
            return 0 if conectada else 1

        if not args.url_publica:
            print("\nInforme --url-publica, ou use --so-status.")
            return 2
        base = args.url_publica.strip().rstrip("/")
        if not base.startswith("https://"):
            print("\nA Z-API só aceita webhook HTTPS.")
            return 2
        if not config.zapi_webhook_segredo:
            print("\nZAPI_WEBHOOK_SEGREDO vazio no .env: a Marina recusaria toda chamada.")
            return 2

        destino = f"{base}/webhooks/zapi/{config.zapi_webhook_segredo}"
        codigo, resposta = await zapi.apontar_webhooks(instancia, token, destino)
        if codigo == 200 and resposta.get("value") is True:
            print(f"\nWebhooks apontados para {base}/webhooks/zapi/***")
            return 0
        print(f"\nA Z-API recusou — HTTP {codigo}: {resposta.get('error') or resposta}")
        return 1
    finally:
        await zapi.fechar()


if __name__ == "__main__":
    sys.exit(executar(main()))
