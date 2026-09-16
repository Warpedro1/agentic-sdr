"""Download de midia recebida, com teto de tamanho.

Os tres provedores de WhatsApp entregam o arquivo por URL, e a Marina precisa dele
em disco: `LocalDocumentStorage` valida pela extensao e confere que o arquivo
existe. O que muda entre eles e so a autenticacao do download, que chega pelos
argumentos do httpx — `auth=` na Twilio, `headers=` na Meta, nada na Z-API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class MidiaGrandeDemais(Exception):
    """Passou do limite no meio do download."""


async def baixar_com_limite(
    http: httpx.AsyncClient, url: str, destino: Path, limite: int, **kwargs: Any
) -> int:
    """Grava a midia em `destino` e devolve o tamanho.

    O tamanho e conferido DURANTE o download, para um arquivo enorme nao encher o
    disco antes de alguem reparar. `follow_redirects` porque a URL da Twilio
    redireciona para o armazenamento.
    """
    escritos = 0
    async with http.stream("GET", url, follow_redirects=True, **kwargs) as resposta:
        resposta.raise_for_status()
        with destino.open("wb") as arquivo:
            async for pedaco in resposta.aiter_bytes():
                escritos += len(pedaco)
                if escritos > limite:
                    raise MidiaGrandeDemais
                arquivo.write(pedaco)
    return escritos
