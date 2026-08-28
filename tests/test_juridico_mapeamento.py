"""Tradução Marina -> juridico. Puro, sem rede."""

import pytest

from app.enums import Fonte, TipoContrato
from app.integrations.juridico.mapeamento import (
    ORIGENS_JURIDICO,
    origem_lead,
    payload_lead,
    payload_tarefa,
    telefone_para_juridico,
    texto_qualificacao,
)


@pytest.mark.parametrize("fonte", [f.value for f in Fonte])
def test_toda_fonte_vira_origem_que_o_juridico_aceita(fonte):
    # O juridico devolve 400 para qualquer origem fora da lista dele. Nenhuma
    # fonte da Marina pode escapar do mapa.
    assert origem_lead(fonte) in ORIGENS_JURIDICO


@pytest.mark.parametrize(
    "fonte,esperado",
    [
        (Fonte.ANUNCIO.value, "INSTAGRAM_META_ADS"),
        (Fonte.GOOGLE.value, "GOOGLE_ADS"),
        (Fonte.INDICACAO.value, "INDICACAO"),
        (Fonte.WHATSAPP.value, "INDICACAO"),
        (Fonte.LINKEDIN.value, "INDICACAO"),
    ],
)
def test_mapa_de_origem(fonte, esperado):
    assert origem_lead(fonte) == esperado


def test_fonte_desconhecida_nao_derruba_o_envio():
    # Se a Marina passar a coletar uma fonte nova e ninguem atualizar o mapa, o
    # lead ainda tem que chegar ao juridico.
    assert origem_lead("tiktok") in ORIGENS_JURIDICO
    assert origem_lead(None) in ORIGENS_JURIDICO


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
    assert telefone_para_juridico(entrada) == esperado


# --- texto da qualificacao --------------------------------------------------


def test_qualificacao_leva_o_que_nao_tem_campo_no_juridico():
    texto = texto_qualificacao(
        {
            "tipo_contrato": TipoContrato.CONSIGNADO.value,
            "possui_contrato": True,
            "fonte": Fonte.INDICACAO.value,
        }
    )
    assert "consignado" in texto
    assert "Tem o contrato em mãos" in texto


def test_qualificacao_avisa_quando_nao_tem_o_contrato():
    texto = texto_qualificacao({"possui_contrato": False})
    assert "NÃO tem o contrato" in texto
    assert "segunda via" in texto


def test_qualificacao_preserva_a_origem_que_o_juridico_nao_representa():
    # WhatsApp vira INDICACAO no campo. Se o texto tambem omitisse, a informacao
    # sumiria — e o consultor perderia o canal de entrada do lead.
    texto = texto_qualificacao({"fonte": Fonte.WHATSAPP.value})
    assert "WhatsApp" in texto
    assert "INDICACAO" in texto


def test_qualificacao_nao_repete_numero_de_menu():
    texto = texto_qualificacao({"fonte": Fonte.INDICACAO.value, "fonte_raw": "5"})
    assert 'Disse: "5"' not in texto


def test_qualificacao_guarda_o_que_a_pessoa_escreveu():
    texto = texto_qualificacao(
        {"fonte": Fonte.INDICACAO.value, "fonte_raw": "um amigo me indicou"}
    )
    assert "um amigo me indicou" in texto


def test_qualificacao_com_lead_vazio_nao_quebra():
    assert texto_qualificacao({}).strip()


# --- payloads ---------------------------------------------------------------


def test_payload_lead_nao_manda_consultor():
    # Sem consultorId, o juridico aplica o rodizio dele. A Marina decide QUANDO
    # e a reuniao; o juridico decide de QUEM e o lead.
    corpo = payload_lead(
        {"nome": "Pedro Guerra", "telefone_e164": "+5511987654321", "fonte": "google"}
    )
    assert "consultorId" not in corpo
    assert corpo["telefoneCelular"] == "11987654321"
    assert corpo["origemLead"] == "GOOGLE_ADS"
    assert corpo["campanha"] == "MARINA"


def test_payload_lead_sem_nome_nao_manda_vazio():
    # O juridico devolve 400 se nomeCompleto for vazio.
    assert payload_lead({})["nomeCompleto"].strip()


# --- a reuniao vai como tarefa EM ABERTO ------------------------------------

SLOT = {"data_iso": "2026-09-02", "hora": "14:00"}
LEAD = {
    "nome": "Pedro Guerra",
    "telefone_e164": "+5511987654321",
    "tipo_contrato": TipoContrato.CONSIGNADO.value,
    "possui_contrato": True,
}


def test_tarefa_nasce_no_nome_do_robo():
    # O juridico exige responsavel em toda tarefa. "Em aberto" e representado
    # por uma tarefa parada no proprio usuario de servico, ate alguem pegar.
    corpo = payload_tarefa(SLOT, LEAD, responsavel_id="usuario-marina")
    assert corpo["responsavelId"] == "usuario-marina"


def test_tarefa_nunca_atribui_consultor():
    # A regra mudou em 27/08/2026: ninguem e escolhido, nenhum nome e prometido.
    corpo = payload_tarefa(SLOT, LEAD, responsavel_id="usuario-marina")
    assert "consultorId" not in corpo


def test_tarefa_aparece_no_calendario_como_reuniao():
    # So os tipos da lista do juridico sao aceitos; qualquer outro devolve 400.
    assert payload_tarefa(SLOT, LEAD, responsavel_id="x")["tipo"] == "REUNIAO"


def test_tarefa_leva_data_e_hora_locais():
    # O juridico junta data + horario como hora local dele. Mandar o dia em UTC
    # erraria a data nas reunioes da noite.
    corpo = payload_tarefa(SLOT, LEAD, responsavel_id="x")
    assert corpo["data"] == "2026-09-02"
    assert corpo["horario"] == "14:00"


def test_tarefa_diz_quem_e_o_lead():
    # A tarefa do juridico nao tem campo de lead: sem isto escrito, quem pegasse
    # veria "Sem vinculo" no calendario e nao teria com quem falar.
    corpo = payload_tarefa(SLOT, LEAD, responsavel_id="x", crm_lead_id="lead-ext")
    assert "Pedro Guerra" in corpo["assunto"]
    assert "+5511987654321" in corpo["descricao"]
    assert "lead-ext" in corpo["descricao"]
    assert "consignado" in corpo["descricao"]


def test_tarefa_usa_email_quando_nao_ha_telefone():
    # Um contato basta, e o lead pode ter chegado so com e-mail.
    corpo = payload_tarefa(
        SLOT,
        {"nome": "Ana", "email": "ana@exemplo.com"},
        responsavel_id="x",
    )
    assert "ana@exemplo.com" in corpo["assunto"]
