"""Guardrails: comandos globais e escopo proibido.

Sao funcoes puras sobre texto. Rodam antes do parser do estado, na ordem definida
em `app.machine`, e valem em qualquer estado.
"""

from __future__ import annotations

from enum import StrEnum

from app.parsers import _palavras, normalizar


class ComandoGlobal(StrEnum):
    SAIR = "sair"
    HUMANO = "humano"
    VOLTAR = "voltar"


TOKENS_SAIR = frozenset({"sair", "parar", "cancelar", "encerrar"})
TOKENS_HUMANO = frozenset({"atendente", "humano", "pessoa", "consultor"})
TOKENS_VOLTAR = frozenset({"voltar", "volta"})


def comando_global(texto: str | None) -> ComandoGlobal | None:
    """Comando valido em qualquer estado, ou None.

    A comparacao e por PALAVRA INTEIRA, nao por trecho. Se fosse por trecho,
    "pessoal" (uma resposta legitima do menu de tipo de contrato) dispararia o
    escalonamento por conter "pessoa".
    """
    if not texto:
        return None
    palavras = _palavras(texto)
    if palavras & TOKENS_SAIR:
        return ComandoGlobal.SAIR
    if palavras & TOKENS_HUMANO:
        return ComandoGlobal.HUMANO
    if palavras & TOKENS_VOLTAR:
        return ComandoGlobal.VOLTAR
    return None


#: Assuntos que a Marina nao responde, em nenhuma hipotese. Ja normalizados
#: (sem acento, minusculas) para casar com o texto normalizado do lead.
#:
#: A comparacao aqui e por TRECHO, e nao por palavra, porque precisa pegar
#: variacoes como "honorarios" e "abusivo". O efeito colateral aceito e que um
#: texto legitimo contendo "taxa" ou "valor" tambem dispara o template — o custo
#: disso e reperguntar, enquanto o custo de deixar passar e a Marina opinar sobre
#: dinheiro sem um consultor olhar o contrato.
TRECHOS_FORA_DE_ESCOPO: tuple[str, ...] = (
    "quanto custa",
    "honorari",
    "abusiv",
    "quanto vou receber",
    "taxa",
    "juros",
    "valor",
)


def fora_de_escopo(texto: str | None) -> bool:
    if not texto:
        return False
    normalizado = normalizar(texto)
    return any(trecho in normalizado for trecho in TRECHOS_FORA_DE_ESCOPO)
