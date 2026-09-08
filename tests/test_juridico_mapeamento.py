"""Tradução Marina -> juridico. Puro, sem rede."""

import pytest

from app.enums import Fonte, TipoContrato
from app.integrations.juridico.mapeamento import (
    FONTES_CRM,
    MAX_ASSUNTO,
    fonte_crm,
    payload_negociacao,
    payload_tarefa,
    telefone_para_crm,
    texto_qualificacao,
)


@pytest.mark.parametrize("fonte", [f.value for f in Fonte])
def test_toda_fonte_vira_valor_que_o_juridico_aceita(fonte):
    # O juridico recusa qualquer fonte fora da lista dele. Nenhuma fonte da
    # Marina pode escapar do mapa.
    assert fonte_crm(fonte) in FONTES_CRM


@pytest.mark.parametrize(
    "fonte,esperado",
    [
        (Fonte.GOOGLE.value, "buscadores"),
        (Fonte.INDICACAO.value, "indicacao_cliente"),
        (Fonte.EMAIL.value, "contato_email"),
        (Fonte.ANUNCIO.value, "redes_sociais"),
        (Fonte.LINKEDIN.value, "redes_sociais"),
        # Ganho em relação ao CRM antigo, que não tinha nada disso.
        (Fonte.WHATSAPP.value, "contato_telefone"),
    ],
)
def test_mapa_de_fonte(fonte, esperado):
    assert fonte_crm(fonte) == esperado


def test_fonte_desconhecida_nao_derruba_o_envio():
    # Se a Marina passar a coletar uma fonte nova e ninguem atualizar o mapa, o
    # lead ainda tem que chegar ao juridico.
    assert fonte_crm("tiktok") in FONTES_CRM
    assert fonte_crm(None) in FONTES_CRM


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("+5511987654321", "11987654321"),
        ("+551133334444", "1133334444"),
        ("11987654321", "11987654321"),
        (None, ""),
    ],
)
def test_telefone_perde_o_codigo_do_pais(entrada, esperado):
    assert telefone_para_crm(entrada) == esperado


# --- texto da qualificacao --------------------------------------------------


def test_qualificacao_leva_o_que_nao_tem_campo_no_juridico():
    texto = texto_qualificacao(
        {
            "tipo_contrato": TipoContrato.CONSIGNADO.value,
            "possui_contrato": True,
            "fonte": Fonte.INDICACAO.value,
        }
    )
    assert "empréstimo consignado" in texto
    assert "Tem o contrato em mãos." in texto
    assert "Origem: indicação." in texto


def test_qualificacao_avisa_quando_nao_tem_o_contrato():
    texto = texto_qualificacao({"possui_contrato": False})
    assert "NÃO tem o contrato em mãos" in texto
    assert "segunda via" in texto


def test_qualificacao_registra_a_origem_real_quando_a_traducao_perde():
    # LinkedIn vira "redes sociais" no CRM; a origem verdadeira nao pode sumir.
    texto = texto_qualificacao({"fonte": Fonte.LINKEDIN.value})
    assert "Origem real: LinkedIn" in texto
    assert "redes_sociais" in texto


def test_qualificacao_inclui_o_canal_quando_informado():
    texto = texto_qualificacao({"fonte": Fonte.WHATSAPP.value}, canal="WhatsApp")
    assert "Primeiro contato por WhatsApp." in texto


def test_qualificacao_ignora_resposta_de_menu():
    # "3" e o numero que a pessoa digitou no menu, nao uma frase dela.
    assert "Disse:" not in texto_qualificacao({"fonte_raw": "3"})
    assert 'Disse: "vi no jornal".' in texto_qualificacao({"fonte_raw": "vi no jornal"})


# --- payload da negociacao --------------------------------------------------


def test_payload_da_negociacao_leva_a_referencia_externa():
    payload = payload_negociacao(
        {
            "lead_id": "abc-123",
            "nome": "Pedro Guerra",
            "telefone_e164": "+5511987654321",
            "email": "pedro@exemplo.com.br",
            "fonte": Fonte.INDICACAO.value,
        }
    )
    assert payload["referencia_externa"] == "abc-123"
    assert payload["nome"] == "Pedro Guerra"
    assert payload["telefone"] == "11987654321"
    assert payload["fonte"] == "indicacao_cliente"
    assert payload["email"] == "pedro@exemplo.com.br"
    # Quem recebe decide o responsavel a partir do token.
    assert "responsavel_id" not in payload
    assert "usuario_id" not in payload


def test_payload_da_negociacao_omite_email_vazio():
    # Mandar string vazia seria recusado pela validacao de e-mail do juridico.
    assert "email" not in payload_negociacao({"lead_id": "x", "email": ""})
    assert "email" not in payload_negociacao({"lead_id": "x"})


def test_payload_da_negociacao_sobrevive_a_lead_sem_nome():
    assert payload_negociacao({"lead_id": "x"})["nome"] == "Lead sem nome"


# --- payload da tarefa ------------------------------------------------------


def _agendamento():
    return {"data_iso": "2026-09-02", "hora": "14:00"}


def _lead():
    return {
        "nome": "Pedro Guerra",
        "telefone_e164": "+5511987654321",
        "email": "pedro@exemplo.com.br",
        "tipo_contrato": TipoContrato.CONSIGNADO.value,
        "possui_contrato": True,
        "fonte": Fonte.INDICACAO.value,
    }


def test_tarefa_junta_data_e_hora_no_formato_do_juridico():
    payload = payload_tarefa(_agendamento(), _lead())
    assert payload["agendada_para"] == "2026-09-02 14:00:00"
    assert payload["tipo"] == "reuniao"


def test_tarefa_nao_indica_responsavel():
    # A reuniao nasce em aberto: quem recebe usa o usuario do token, e trocar o
    # dono e uma acao de quem for atender.
    payload = payload_tarefa(_agendamento(), _lead())
    assert "responsavel_id" not in payload
    assert "responsavelId" not in payload


def test_tarefa_identifica_o_lead_no_assunto_e_na_observacao():
    payload = payload_tarefa(_agendamento(), _lead())
    assert "Pedro Guerra" in payload["assunto"]
    assert "+5511987654321" in payload["assunto"]
    assert "empréstimo consignado" in payload["observacao"]
    assert "assuma como responsável" in payload["observacao"]


def test_tarefa_trunca_assunto_no_limite_da_coluna():
    # A coluna aceita 180 chars; estourar devolve 422, que e erro permanente e
    # mataria o agendamento na fila por causa de um nome comprido.
    payload = payload_tarefa(_agendamento(), {**_lead(), "nome": "N" * 400})
    assert len(payload["assunto"]) == MAX_ASSUNTO


def test_tarefa_usa_email_quando_nao_ha_telefone():
    payload = payload_tarefa(
        _agendamento(), {**_lead(), "telefone_e164": None}
    )
    assert "pedro@exemplo.com.br" in payload["assunto"]
