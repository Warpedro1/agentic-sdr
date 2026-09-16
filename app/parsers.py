"""Interpretacao das respostas do lead.

Explicito, com fallback honesto: quando um parser nao entende, ele devolve None e
a maquina repete a pergunta. Nenhum parser adivinha.

Fora de escopo por decisao de projeto: NAO existe parsing de data em linguagem
natural. Horario e escolhido pelo numero do menu e ponto — e exatamente onde uma
automacao erra feio, e errar ali significa marcar reuniao no dia errado.
"""

from __future__ import annotations

import re
import unicodedata

from app.enums import Fonte, TipoContrato

# --- normalizacao -----------------------------------------------------------


def normalizar(texto: str) -> str:
    """Minusculas, sem acento, espacos colapsados."""
    sem_acento = "".join(
        caractere
        for caractere in unicodedata.normalize("NFD", texto)
        if unicodedata.category(caractere) != "Mn"
    )
    return re.sub(r"\s+", " ", sem_acento).strip().lower()


def _palavras(texto: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalizar(texto)))


# --- nome -------------------------------------------------------------------

LIMITE_NOME = 160


def parse_nome(texto: str) -> str | None:
    limpo = "".join(
        caractere
        for caractere in texto
        if caractere.isprintable() or caractere in " \t"
    )
    limpo = re.sub(r"\s+", " ", limpo).strip(" .,-")
    if len(limpo) < 2:
        return None
    if re.fullmatch(r"[\d\s\-().+]+", limpo):
        return None
    return limpo[:LIMITE_NOME]


def primeiro_nome(nome: str) -> str:
    partes = nome.split()
    return partes[0] if partes else nome


# --- telefone ---------------------------------------------------------------

#: DDDs em uso no Brasil. Um DDD invalido e erro de digitacao, nao numero exotico.
DDDS_VALIDOS = frozenset(
    {
        11, 12, 13, 14, 15, 16, 17, 18, 19,
        21, 22, 24, 27, 28,
        31, 32, 33, 34, 35, 37, 38,
        41, 42, 43, 44, 45, 46, 47, 48, 49,
        51, 53, 54, 55,
        61, 62, 63, 64, 65, 66, 67, 68, 69,
        71, 73, 74, 75, 77, 79,
        81, 82, 83, 84, 85, 86, 87, 88, 89,
        91, 92, 93, 94, 95, 96, 97, 98, 99,
    }
)


def parse_telefone(texto: str) -> str | None:
    """Devolve o telefone em E.164 (+55DDDNNNNNNNNN) ou None."""
    digitos = re.sub(r"\D", "", texto)

    # Aceita o numero ja com codigo do pais.
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        digitos = digitos[2:]

    if len(digitos) not in (10, 11):
        return None

    ddd = int(digitos[:2])
    if ddd not in DDDS_VALIDOS:
        return None

    assinante = digitos[2:]
    if len(set(assinante)) == 1:  # 999999999 e afins
        return None
    if len(assinante) == 9 and not assinante.startswith("9"):
        return None
    if len(assinante) == 8 and assinante[0] not in "2345":
        return None

    return f"+55{digitos}"


def telefone_do_canal(identidade: str) -> str | None:
    """O numero de quem escreveu, vindo do proprio canal, em E.164.

    Nao e o mesmo problema de `parse_telefone`. La o texto foi DIGITADO por uma
    pessoa, e recusar o duvidoso e o certo: melhor perguntar de novo do que gravar
    um numero errado. Aqui o numero veio da operadora, e ja e o endereco por onde a
    conversa esta acontecendo — recusa-lo seria perguntar a alguem o numero de onde
    ele acabou de falar.

    O que muda de fato e o nono digito. O WhatsApp entrega o celular brasileiro de
    DDD alto SEM ele (`+558191234567`), e a mesma regra que o parser ja usa para
    separar fixo de celular resolve: assinante de oito digitos comecando em 2-5 e
    fixo; de 6 a 9 e celular antigo, e o `9` que falta entra na frente.

    Devolve None para o que nao for um telefone brasileiro reconhecivel — o
    identificador de outro canal, por exemplo.
    """
    # Contato com numero oculto chega pela Z-API como `...@lid`. E um identificador,
    # nao um telefone — e os digitos dele poderiam, por acaso, formar um.
    if "@" in (identidade or ""):
        return None

    digitos = re.sub(r"\D", "", identidade or "")

    if not digitos.startswith("55"):
        return None
    digitos = digitos[2:]

    if len(digitos) == 10 and digitos[2:3] in "6789":
        digitos = f"{digitos[:2]}9{digitos[2:]}"

    return parse_telefone(f"+55{digitos}")


# --- e-mail -----------------------------------------------------------------

PADRAO_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def parse_email(texto: str) -> str | None:
    encontrado = PADRAO_EMAIL.search(texto.strip())
    return encontrado.group(0).lower() if encontrado else None


# --- menus ------------------------------------------------------------------


def parse_opcao_menu(texto: str, total: int) -> int | None:
    # Só pontuacao a DIREITA ("1)", "2."). Retirar a esquerda transformaria
    # "-1" em "1", aceitando uma opcao que nao existe.
    limpo = normalizar(texto).rstrip(" .)-")
    if not limpo.isdigit():
        return None
    escolha = int(limpo)
    return escolha if 1 <= escolha <= total else None


# --- fonte ------------------------------------------------------------------

ORDEM_FONTE: tuple[Fonte, ...] = (
    Fonte.ANUNCIO,
    Fonte.WHATSAPP,
    Fonte.EMAIL,
    Fonte.LINKEDIN,
    Fonte.INDICACAO,
    Fonte.GOOGLE,
    Fonte.OUTRO,
)

SINONIMOS_FONTE: dict[Fonte, tuple[str, ...]] = {
    Fonte.ANUNCIO: ("anuncio", "anuncios", "ads", "propaganda", "facebook", "instagram"),
    Fonte.WHATSAPP: ("whatsapp", "whats", "wpp", "zap"),
    Fonte.EMAIL: ("email", "e-mail", "mail", "newsletter"),
    Fonte.LINKEDIN: ("linkedin", "linked in"),
    Fonte.INDICACAO: (
        "indicacao",
        "indicaram",
        "indicou",
        "indicado",
        "amigo",
        "amiga",
        "conhecido",
        "parente",
    ),
    Fonte.GOOGLE: ("google", "pesquisa", "pesquisei", "busca", "buscador"),
    Fonte.OUTRO: ("outro", "outra", "nenhum", "nao sei", "nao lembro"),
}


def parse_fonte(texto: str) -> tuple[Fonte, str] | None:
    """Devolve (fonte normalizada, texto original). O original vai para fonte_raw."""
    original = texto.strip()

    escolha = parse_opcao_menu(texto, len(ORDEM_FONTE))
    if escolha is not None:
        return ORDEM_FONTE[escolha - 1], original

    normalizado = normalizar(texto)
    for fonte in ORDEM_FONTE:
        if any(sinonimo in normalizado for sinonimo in SINONIMOS_FONTE[fonte]):
            return fonte, original
    return None


# --- tipo de contrato -------------------------------------------------------

ORDEM_TIPO_CONTRATO: tuple[TipoContrato, ...] = (
    TipoContrato.CONSIGNADO,
    TipoContrato.PESSOAL,
    TipoContrato.CARTAO_CREDITO,
)

#: A ordem de teste NAO e a ordem do menu: cartao vem antes de pessoal, para que
#: uma frase que cite os dois ("emprestimo pessoal no cartao") caia em cartao, que
#: e o termo mais especifico. Manter esta ordem ao acrescentar sinonimos.
ORDEM_TESTE_TIPO: tuple[TipoContrato, ...] = (
    TipoContrato.CARTAO_CREDITO,
    TipoContrato.CONSIGNADO,
    TipoContrato.PESSOAL,
)

SINONIMOS_TIPO_CONTRATO: dict[TipoContrato, tuple[str, ...]] = {
    TipoContrato.CARTAO_CREDITO: (
        "cartao de credito",
        "cartao",
        "fatura",
        "rotativo",
        "credito rotativo",
    ),
    TipoContrato.CONSIGNADO: (
        "consignado",
        "consignacao",
        "inss",
        "aposentado",
        "aposentadoria",
        "desconto em folha",
        "folha",
    ),
    TipoContrato.PESSOAL: (
        "credito pessoal",
        "emprestimo pessoal",
        "pessoal",
        "banco",
    ),
}


def parse_tipo_contrato(texto: str) -> tuple[TipoContrato, str] | None:
    original = texto.strip()

    escolha = parse_opcao_menu(texto, len(ORDEM_TIPO_CONTRATO))
    if escolha is not None:
        return ORDEM_TIPO_CONTRATO[escolha - 1], original

    normalizado = normalizar(texto)
    for tipo in ORDEM_TESTE_TIPO:
        if any(sinonimo in normalizado for sinonimo in SINONIMOS_TIPO_CONTRATO[tipo]):
            return tipo, original
    return None


# --- sim / nao --------------------------------------------------------------

TOKENS_SIM = frozenset({"sim", "s", "tenho", "isso", "claro", "positivo", "1"})
TOKENS_NAO = frozenset({"nao", "n", "negativo", "2"})


def parse_sim_nao(texto: str) -> bool | None:
    palavras = _palavras(texto)
    # NAO e testado primeiro de proposito: "nao tenho" contem "tenho", e sem esta
    # ordem seria lido como sim.
    if palavras & TOKENS_NAO:
        return False
    if palavras & TOKENS_SIM:
        return True
    return None


# --- pular ------------------------------------------------------------------

TOKENS_PULAR = frozenset({"pular", "pula", "depois", "skip"})


def quer_pular(texto: str) -> bool:
    return bool(_palavras(texto) & TOKENS_PULAR)
