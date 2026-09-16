"""Segredo que viaja em URL nao pode chegar ao arquivo de log.

Duas bibliotecas escrevem URL inteira sem ninguem pedir: o httpx, a cada chamada que a
Marina faz (e na Z-API o token da instancia vai no caminho), e o uvicorn, a cada
chamada recebida (o webhook da Z-API tem o segredo no caminho; o aperto de mao da
Meta, o token na query).
"""

from __future__ import annotations

import logging

import httpx

from app.logging import MascaraSegredos, mascarar_segredos


def _registro(mensagem: str, argumentos: tuple) -> logging.LogRecord:
    return logging.LogRecord("teste", logging.INFO, __file__, 1, mensagem, argumentos, None)


def test_token_da_instancia_na_url_de_envio_e_mascarado():
    linha = (
        "HTTP Request: POST https://api.z-api.io/instances/INST/token/SEGREDO123/"
        'send-text "HTTP/1.1 200 OK"'
    )
    mascarada = mascarar_segredos(linha)
    assert "SEGREDO123" not in mascarada
    assert "/instances/INST/token/***/send-text" in mascarada


def test_segredo_do_webhook_e_token_da_meta_sao_mascarados():
    assert mascarar_segredos("/webhooks/zapi/SEGREDO123") == "/webhooks/zapi/***"
    linha = "GET /webhooks/meta/whatsapp?hub.mode=subscribe&hub.verify_token=TOKEN9&hub.challenge=1"
    mascarada = mascarar_segredos(linha)
    assert "TOKEN9" not in mascarada
    assert "hub.challenge=1" in mascarada


def test_filtro_mascara_url_passada_como_objeto():
    """O httpx passa a URL como objeto nos argumentos, nao como texto."""
    registro = _registro(
        'HTTP Request: %s %s "%s %d %s"',
        (
            "POST",
            httpx.URL("https://api.z-api.io/instances/INST/token/SEGREDO123/send-text"),
            "HTTP/1.1",
            200,
            "OK",
        ),
    )
    assert MascaraSegredos().filter(registro) is True
    assert "SEGREDO123" not in registro.getMessage()


def test_log_de_acesso_do_webhook_da_zapi_e_mascarado():
    registro = _registro(
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:0", "POST", "/webhooks/zapi/SEGREDO123", "1.1", 204),
    )
    MascaraSegredos().filter(registro)
    assert registro.getMessage() == '1.2.3.4:0 - "POST /webhooks/zapi/*** HTTP/1.1" 204'


def test_linha_sem_segredo_passa_intacta():
    registro = _registro(
        '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:0", "GET", "/saude", "1.1", 200)
    )
    MascaraSegredos().filter(registro)
    assert registro.getMessage() == '1.2.3.4:0 - "GET /saude HTTP/1.1" 200'
