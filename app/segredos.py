"""Leitura de segredos do ambiente.

Existe por causa de um requisito de sigilo: a credencial de acesso ao juridico de
cada empresa nao pode ser legivel num dump do banco, num log nem num `SELECT`. O
banco guarda o NOME da variavel; o valor vem daqui.

O `.env` entra como fallback porque o `pydantic-settings` **le** o arquivo mas nao
exporta nada para o processo — sem isto, `python -m app.whatsapp` em desenvolvimento
reclamaria de token ausente com o `.env` preenchido, que e o sintoma mais confuso
possivel. Em producao as variaveis sao de verdade e o fallback nunca e usado.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

_ARQUIVO = Path(".env")

_trava = threading.Lock()
_cache: dict[str, str] | None = None


def _do_arquivo() -> dict[str, str]:
    valores: dict[str, str] = {}
    if not _ARQUIVO.is_file():
        return valores
    for linha in _ARQUIVO.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valores[chave.strip()] = valor.strip().strip("\"'")
    return valores


def ler(nome: str) -> str:
    """O valor da variavel, ou vazio. O ambiente real ganha do `.env`."""
    if not nome:
        return ""
    do_ambiente = os.environ.get(nome, "").strip()
    if do_ambiente:
        return do_ambiente

    global _cache
    if _cache is None:
        with _trava:
            if _cache is None:
                _cache = _do_arquivo()
    return _cache.get(nome, "").strip()


def esquecer() -> None:
    """Descarta o cache do arquivo. Usado pelos testes e por quem rotaciona token."""
    global _cache
    _cache = None
