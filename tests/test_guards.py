"""Guardrails: comandos globais e o bloqueio de assunto financeiro."""

import pytest

from app.guards import ComandoGlobal, comando_global, fora_de_escopo
from app.templates import obter_catalogo


@pytest.mark.parametrize("entrada", ["sair", "SAIR", "quero parar", "cancelar"])
def test_comando_sair(entrada):
    assert comando_global(entrada) is ComandoGlobal.SAIR


@pytest.mark.parametrize("entrada", ["atendente", "quero falar com um humano", "pessoa"])
def test_comando_humano(entrada):
    assert comando_global(entrada) is ComandoGlobal.HUMANO


@pytest.mark.parametrize("entrada", ["voltar", "VOLTAR"])
def test_comando_voltar(entrada):
    assert comando_global(entrada) is ComandoGlobal.VOLTAR


@pytest.mark.parametrize("entrada", ["Pedro Guerra", "1", "", None, "consignado"])
def test_sem_comando(entrada):
    assert comando_global(entrada) is None


def test_pessoal_nao_dispara_escalonamento():
    # "pessoal" contem "pessoa". Se a comparacao fosse por trecho, responder
    # "pessoal" no menu de tipo de contrato jogaria o lead para um humano.
    assert comando_global("empréstimo pessoal") is None


# --- escopo proibido --------------------------------------------------------


@pytest.mark.parametrize(
    "entrada",
    [
        "quanto custa?",
        "quanto vocês cobram?",
        "qual o preço?",
        "quanto vocês cobram de honorários?",
        "meu contrato é abusivo?",
        "quanto vou receber de volta?",
        "qual a taxa?",
        "os juros estão certos?",
        "qual o valor da revisão?",
    ],
)
def test_dispara_fora_de_escopo(entrada):
    assert fora_de_escopo(entrada) is True


@pytest.mark.parametrize(
    "entrada",
    ["Pedro Guerra", "1", "consignado", "pedro@exemplo.com.br", "", None],
)
def test_nao_dispara_fora_de_escopo(entrada):
    assert fora_de_escopo(entrada) is False


def test_acento_nao_escapa_do_guardrail():
    assert fora_de_escopo("honorários") is True
    assert fora_de_escopo("honorarios") is True


# --- catalogo ---------------------------------------------------------------


def test_fora_de_escopo_nao_responde_a_pergunta():
    # O template existe para desviar, nunca para dar um numero. Se alguem editar a
    # copy e colocar um valor ali, este teste e a ultima barreira.
    texto = obter_catalogo().bruto("fora_de_escopo").lower()
    assert "consultor" in texto
    assert not any(sinal in texto for sinal in ("r$", "%", "reais"))


def test_todas_as_chaves_usadas_existem():
    chaves = obter_catalogo().chaves
    obrigatorias = {
        "boas_vindas",
        "pergunta_nome",
        "pergunta_telefone",
        "erro_telefone",
        "pergunta_email",
        "pergunta_fonte",
        "pergunta_tipo_contrato",
        "erro_tipo_contrato",
        "pergunta_contrato",
        "pedido_contrato",
        "contrato_recebido",
        "contrato_pulado",
        "sem_contrato",
        "oferta_horarios",
        "sem_horarios",
        "confirmacao",
        "agendado",
        "slot_perdido",
        "nao_entendi",
        "fora_de_escopo",
        "escalonamento",
        "opt_out",
        "retomada_opt_out",
        "retomada_conversa",
        "retorno_reconhecido",
        "opt_out_mantido",
    }
    assert obrigatorias <= chaves


def test_render_substitui_variaveis():
    mensagem = obter_catalogo().render("pergunta_telefone", nome="Pedro")
    assert "Pedro" in mensagem.conteudo
    assert mensagem.template_key == "pergunta_telefone"


def test_render_reclama_de_variavel_faltando():
    with pytest.raises(KeyError):
        obter_catalogo().render("pergunta_telefone")


def test_adaptador_aceita_file_uri_e_caminho_comum(tmp_path):
    """file:///C:/... e o que o Windows entrega ao copiar o endereco do arquivo."""
    from app.channels.terminal import TerminalAdapter

    arquivo = tmp_path / "contrato.pdf"
    arquivo.write_bytes(b"%PDF-1.4 x")

    caminho = str(arquivo)
    uri = arquivo.as_uri()

    assert TerminalAdapter._resolver_anexo(caminho) is not None
    assert TerminalAdapter._resolver_anexo(uri) is not None
    assert TerminalAdapter._resolver_anexo(f'"{caminho}"') is not None


def test_adaptador_sinaliza_caminho_que_nao_existe():
    from app.channels.terminal import TerminalAdapter

    assert TerminalAdapter._parece_caminho("C:/nao/existe/Trello.pd") is True
    assert TerminalAdapter._parece_caminho("file:///C:/x/y.pdf") is True
    # Uma resposta normal nao pode ser confundida com tentativa de anexo.
    assert TerminalAdapter._parece_caminho("Pedro Guerra") is False
    assert TerminalAdapter._parece_caminho("1") is False
