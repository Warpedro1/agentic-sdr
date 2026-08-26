"""Carrega e renderiza o catalogo de mensagens.

Uma resposta so existe se estiver no messages.yaml. Chave inexistente e variavel
faltando levantam erro na hora, e nao passam despercebidas ate a producao.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.channels.base import MensagemSaida

CAMINHO_PADRAO = Path(__file__).parent / "messages.yaml"


class ChaveDesconhecida(KeyError):
    """A chave pedida nao existe no catalogo."""


class VariavelFaltando(KeyError):
    """O template exige uma variavel que nao foi passada."""


class CatalogoMensagens:
    def __init__(self, caminho: Path | None = None) -> None:
        self._caminho = caminho or CAMINHO_PADRAO
        with self._caminho.open(encoding="utf-8") as arquivo:
            carregado = yaml.safe_load(arquivo) or {}
        self._templates: dict[str, str] = {
            chave: str(valor) for chave, valor in carregado.items()
        }

    @property
    def chaves(self) -> frozenset[str]:
        return frozenset(self._templates)

    def bruto(self, chave: str) -> str:
        try:
            return self._templates[chave]
        except KeyError:
            raise ChaveDesconhecida(
                f"'{chave}' nao existe em {self._caminho.name}"
            ) from None

    def render(self, chave: str, **valores: Any) -> MensagemSaida:
        template = self.bruto(chave)
        try:
            conteudo = template.format(**valores)
        except KeyError as erro:
            raise VariavelFaltando(
                f"template '{chave}' exige a variavel {erro}"
            ) from None
        return MensagemSaida(template_key=chave, conteudo=conteudo.strip())


@lru_cache
def obter_catalogo() -> CatalogoMensagens:
    return CatalogoMensagens()
