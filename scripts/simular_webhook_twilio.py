"""Manda um webhook assinado para a Marina, como a Twilio faria.

    python scripts/simular_webhook_twilio.py --texto "oi"
    python scripts/simular_webhook_twilio.py --texto "Pedro Guerra"
    python scripts/simular_webhook_twilio.py --texto "oi" --sid SM123  # repete o id
    python scripts/simular_webhook_twilio.py --texto "oi" --sem-assinatura

Serve para exercitar o fluxo inteiro — assinatura, idempotencia e a conversa —
**sem ngrok e sem conta na Twilio**. Assina com o mesmo `RequestValidator` que o
webhook usa para conferir, entao um passa quando o outro passa.

Suba a Marina antes, em outro terminal:

    python -m app.whatsapp

A URL assinada e a `TWILIO_URL_PUBLICA` do .env, e nao o endereco para onde o
POST vai. E de proposito: e assim que a Twilio faz, e e o que este script precisa
reproduzir para a assinatura bater.
"""

from __future__ import annotations

import argparse
import sys
import uuid

import httpx
from twilio.request_validator import RequestValidator

from app.config import obter_configuracao

CAMINHO = "/webhooks/twilio/whatsapp"


def montar_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--texto", required=True, help="o que o lead mandou")
    parser.add_argument(
        "--de",
        default="+5511987654321",
        help="telefone do lead em E.164 (padrão: +5511987654321)",
    )
    parser.add_argument(
        "--sid",
        default=None,
        help="MessageSid; repetir o mesmo testa a idempotência",
    )
    parser.add_argument(
        "--destino",
        default=None,
        help="para onde mandar o POST (padrão: http://HOST:PORTA do .env)",
    )
    parser.add_argument(
        "--sem-assinatura",
        action="store_true",
        help="manda sem o cabeçalho; deve responder 403",
    )
    return parser.parse_args()


def main() -> int:
    args = montar_argumentos()
    config = obter_configuracao()

    if not config.twilio_auth_token and not args.sem_assinatura:
        print(
            "TWILIO_AUTH_TOKEN vazio no .env — sem ele não dá para assinar.\n"
            "Para testar a recusa, use --sem-assinatura.",
            file=sys.stderr,
        )
        return 1

    formulario = {
        "MessageSid": args.sid or f"SM{uuid.uuid4().hex}",
        "AccountSid": config.twilio_account_sid or "ACsimulado",
        "From": f"whatsapp:{args.de}",
        "To": config.twilio_remetente or "whatsapp:+14155238886",
        "Body": args.texto,
        "NumMedia": "0",
        "ProfileName": "Lead de teste",
    }

    url_assinada = f"{config.twilio_url_publica.rstrip('/')}{CAMINHO}"
    cabecalhos = {}
    if not args.sem_assinatura:
        validador = RequestValidator(config.twilio_auth_token)
        cabecalhos["X-Twilio-Signature"] = validador.compute_signature(
            url_assinada, formulario
        )

    base = args.destino or f"http://{config.whatsapp_host}:{config.whatsapp_porta}"
    resposta = httpx.post(f"{base}{CAMINHO}", data=formulario, headers=cabecalhos)

    print(f"POST {base}{CAMINHO} -> {resposta.status_code}")
    print(f"  assinada como: {url_assinada}")
    print(f"  MessageSid:    {formulario['MessageSid']}")
    if resposta.status_code == 403:
        print("  403 = assinatura recusada (esperado com --sem-assinatura).")
    elif resposta.status_code == 204:
        print("  204 = aceita. A resposta da Marina sai pela API da Twilio;")
        print("  com credenciais falsas isso vira 'envio_falhou' no marina.log.")
        print("  A conversa em si: python scripts/dump_lead.py --telefone " + args.de)
    return 0 if resposta.status_code in (204, 403) else 1


if __name__ == "__main__":
    raise SystemExit(main())
