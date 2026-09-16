"""Parsers: o que a Marina entende e, principalmente, o que ela se recusa a adivinhar."""

import pytest

from app.enums import Fonte, TipoContrato
from app.parsers import (
    parse_email,
    parse_fonte,
    parse_nome,
    parse_opcao_menu,
    parse_sim_nao,
    parse_telefone,
    telefone_do_canal,
    parse_tipo_contrato,
    primeiro_nome,
    quer_pular,
)

# --- nome -------------------------------------------------------------------


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("Pedro Guerra", "Pedro Guerra"),
        ("  maria   das   dores  ", "maria das dores"),
        ("Ana", "Ana"),
        ("José Antônio", "José Antônio"),
    ],
)
def test_nome_aceito(entrada, esperado):
    assert parse_nome(entrada) == esperado


@pytest.mark.parametrize("entrada", ["", " ", "A", "123456", "11 98765-4321", "  .  "])
def test_nome_recusado(entrada):
    assert parse_nome(entrada) is None


def test_nome_truncado_no_limite():
    assert len(parse_nome("x" * 500)) == 160


def test_primeiro_nome():
    assert primeiro_nome("Maria das Dores") == "Maria"


# --- telefone ---------------------------------------------------------------


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("11987654321", "+5511987654321"),
        ("(11) 98765-4321", "+5511987654321"),
        ("11 98765 4321", "+5511987654321"),
        ("+55 11 98765-4321", "+5511987654321"),
        ("5511987654321", "+5511987654321"),
        ("meu numero e 81 99999-1234 ok?", "+5581999991234"),
        ("1133334444", "+551133334444"),  # fixo, 10 digitos
    ],
)
def test_telefone_normalizado(entrada, esperado):
    assert parse_telefone(entrada) == esperado


@pytest.mark.parametrize(
    "entrada",
    [
        "123",
        "987654321",  # sem DDD
        "0098765432100",
        "20987654321",  # DDD 20 nao existe
        "11888884321",  # celular de 9 digitos que nao comeca com 9
        "11999999999",  # todos os digitos iguais
        "sem numero nenhum",
        "",
    ],
)
def test_telefone_recusado(entrada):
    assert parse_telefone(entrada) is None


# --- telefone que vem do canal ----------------------------------------------
#
# Nao e o mesmo problema do parser de digitacao. Aqui o numero veio da operadora
# e ja e o endereco por onde a conversa acontece: recusa-lo seria perguntar a
# alguem o numero de onde ele acabou de falar.


@pytest.mark.parametrize(
    "identidade,esperado",
    [
        # O caso que motivou tudo: o WhatsApp entrega celular de DDD alto SEM o
        # nono digito, e era assim que o numero do proprio time era recusado.
        ("+558191234567", "+5581991234567"),
        ("whatsapp:+558191234567", "+5581991234567"),
        ("+5581991234567", "+5581991234567"),  # ja completo, passa direto
        ("+5511987654321", "+5511987654321"),
        ("+551133334444", "+551133334444"),  # fixo continua fixo, sem ganhar o 9
    ],
)
def test_telefone_do_canal_repoe_o_nono_digito(identidade, esperado):
    assert telefone_do_canal(identidade) == esperado


@pytest.mark.parametrize(
    "identidade",
    [
        "+14155238886",  # outro pais
        "terminal",  # canal sem endereco
        "+55819999999",  # digitos de menos
        # Contato com numero oculto, pela Z-API: identificador, nao telefone — os
        # digitos dele nao podem virar um numero por acaso.
        "5581991234567@lid",
        "",
    ],
)
def test_telefone_do_canal_recusa_o_que_nao_reconhece(identidade):
    assert telefone_do_canal(identidade) is None


# --- e-mail -----------------------------------------------------------------


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("pedro@exemplo.com.br", "pedro@exemplo.com.br"),
        ("  PEDRO@EXEMPLO.COM.BR ", "pedro@exemplo.com.br"),
        ("pode mandar em pedro.g+teste@exemplo.com.br valeu", "pedro.g+teste@exemplo.com.br"),
    ],
)
def test_email_aceito(entrada, esperado):
    assert parse_email(entrada) == esperado


@pytest.mark.parametrize("entrada", ["pedro", "pedro@", "@leve.com", "pedro@leve", ""])
def test_email_recusado(entrada):
    assert parse_email(entrada) is None


# --- menu -------------------------------------------------------------------


@pytest.mark.parametrize("entrada,esperado", [("1", 1), (" 3 ", 3), ("5.", 5)])
def test_menu_aceito(entrada, esperado):
    assert parse_opcao_menu(entrada, 7) == esperado


@pytest.mark.parametrize("entrada", ["0", "8", "-1", "um", "", "1a"])
def test_menu_recusado(entrada):
    assert parse_opcao_menu(entrada, 7) is None


# --- fonte ------------------------------------------------------------------


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("1", Fonte.ANUNCIO),
        ("5", Fonte.INDICACAO),
        ("7", Fonte.OUTRO),
        ("indicação", Fonte.INDICACAO),
        ("um amigo me indicou", Fonte.INDICACAO),
        ("vi no instagram", Fonte.ANUNCIO),
        ("pelo zap", Fonte.WHATSAPP),
        ("pesquisei no google", Fonte.GOOGLE),
        ("linkedin", Fonte.LINKEDIN),
        ("e-mail", Fonte.EMAIL),
    ],
)
def test_fonte_aceita(entrada, esperado):
    resultado = parse_fonte(entrada)
    assert resultado is not None
    assert resultado[0] is esperado


def test_fonte_guarda_texto_original():
    _, original = parse_fonte("  Um amigo me indicou  ")
    assert original == "Um amigo me indicou"


@pytest.mark.parametrize("entrada", ["8", "0", "sei la", ""])
def test_fonte_recusada(entrada):
    assert parse_fonte(entrada) is None


# --- tipo de contrato -------------------------------------------------------


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("1", TipoContrato.CONSIGNADO),
        ("2", TipoContrato.PESSOAL),
        ("3", TipoContrato.CARTAO_CREDITO),
        ("consignado", TipoContrato.CONSIGNADO),
        ("é do INSS", TipoContrato.CONSIGNADO),
        ("desconto em folha", TipoContrato.CONSIGNADO),
        ("empréstimo pessoal", TipoContrato.PESSOAL),
        ("crédito pessoal", TipoContrato.PESSOAL),
        ("cartão", TipoContrato.CARTAO_CREDITO),
        ("cartao de credito", TipoContrato.CARTAO_CREDITO),
        ("a fatura do cartão", TipoContrato.CARTAO_CREDITO),
    ],
)
def test_tipo_contrato_aceito(entrada, esperado):
    resultado = parse_tipo_contrato(entrada)
    assert resultado is not None
    assert resultado[0] is esperado


def test_cartao_vence_pessoal_quando_a_frase_cita_os_dois():
    # A ordem de teste e deliberada: o termo mais especifico ganha.
    resultado = parse_tipo_contrato("emprestimo pessoal no cartao")
    assert resultado[0] is TipoContrato.CARTAO_CREDITO


def test_tipo_contrato_guarda_texto_original():
    _, original = parse_tipo_contrato("  É do INSS  ")
    assert original == "É do INSS"


@pytest.mark.parametrize("entrada", ["4", "0", "nao sei dizer", ""])
def test_tipo_contrato_recusado(entrada):
    assert parse_tipo_contrato(entrada) is None


# --- sim / nao --------------------------------------------------------------


@pytest.mark.parametrize("entrada", ["sim", "S", "tenho", "claro", "positivo", "1"])
def test_sim(entrada):
    assert parse_sim_nao(entrada) is True


@pytest.mark.parametrize("entrada", ["não", "nao", "N", "negativo", "2"])
def test_nao(entrada):
    assert parse_sim_nao(entrada) is False


def test_nao_tenho_nao_e_lido_como_sim():
    # "nao tenho" contem "tenho"; sem a precedencia do NAO isso viraria True.
    assert parse_sim_nao("não tenho") is False


@pytest.mark.parametrize("entrada", ["talvez", "", "quem sabe"])
def test_sim_nao_indefinido(entrada):
    assert parse_sim_nao(entrada) is None


# --- pular ------------------------------------------------------------------


@pytest.mark.parametrize("entrada", ["PULAR", "pular", "pula", "deixa pra depois"])
def test_quer_pular(entrada):
    assert quer_pular(entrada) is True


def test_nao_quer_pular():
    assert quer_pular("C:/contratos/meu.pdf") is False
