"""Todas as transicoes da maquina. Nenhum banco, nenhum disco, nenhum relogio.

O `ctx` e montado na mao em cada teste — inclusive o reconhecimento, que na
producao vem de uma consulta feita pelo engine. E isso que permite cobrir os
quatro cenarios de retorno sem subir Postgres.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.channels.base import Anexo, InboundMessage
from app.enums import ESTADOS_INTERNOS, ESTADOS_TERMINAIS, Estado
from app.machine import (
    PERGUNTA_DO_ESTADO,
    EncerrarConversa,
    EscalarParaHumano,
    GerarSlots,
    Politicas,
    RegistrarOptOut,
    ReservarSlot,
    ResolverIdentidade,
    RevogarOptOut,
    SalvarDocumento,
    handle,
)

AGORA = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

SLOTS = [
    {
        "indice": 1,
        "inicio_utc": "2026-08-27T17:00:00+00:00",
        "consultor_id": "c1",
        "consultor_nome": "Ana Ribeiro",
        "data_extenso": "quinta-feira, 27 de agosto",
        "hora": "14:00",
    },
    {
        "indice": 2,
        "inicio_utc": "2026-08-27T18:15:00+00:00",
        "consultor_id": "c2",
        "consultor_nome": "Bruno Tavares",
        "data_extenso": "quinta-feira, 27 de agosto",
        "hora": "15:15",
    },
]

LEAD_COMPLETO = {
    "nome": "Pedro Guerra",
    "telefone_e164": "+5511987654321",
    "email": "pedro@exemplo.com.br",
    "fonte": "indicacao",
    "tipo_contrato": "consignado",
    "possui_contrato": True,
}


def ctx(**extra):
    base = {"agora": AGORA, "politicas": Politicas(), "canal": "terminal", "tentativas": 0}
    base.update(extra)
    return base


def txt(valor):
    return InboundMessage(texto=valor)


def chaves(transicao):
    return [m.template_key for m in transicao.mensagens]


def tipos_efeito(transicao):
    return [type(e) for e in transicao.efeitos]


# --- abertura ---------------------------------------------------------------


def test_inicio_sauda_pergunta_o_nome_e_grava_consentimento():
    t = handle(Estado.INICIO, {}, ctx(), InboundMessage())
    assert t.novo_estado is Estado.AGUARDANDO_NOME
    assert chaves(t) == ["boas_vindas", "pergunta_nome"]
    # O consentimento e carimbado AGORA, na saudacao — nao na hora em que o lead
    # for criado no banco, varias mensagens depois.
    assert t.ctx_updates["consentimento_em"] == AGORA


def test_boas_vindas_avisa_sobre_o_uso_dos_dados():
    t = handle(Estado.INICIO, {}, ctx(), InboundMessage())
    assert "SAIR" in t.mensagens[0].conteudo


# --- nome e telefone --------------------------------------------------------


def test_nome_valido_avanca_para_telefone():
    t = handle(Estado.AGUARDANDO_NOME, {}, ctx(), txt("Pedro Guerra"))
    assert t.novo_estado is Estado.AGUARDANDO_TELEFONE
    assert t.ctx_updates["nome"] == "Pedro Guerra"
    assert "Pedro" in t.mensagens[0].conteudo


def test_nome_invalido_repete_a_pergunta():
    t = handle(Estado.AGUARDANDO_NOME, {}, ctx(), txt("123"))
    assert t.novo_estado is Estado.AGUARDANDO_NOME
    assert chaves(t) == ["nao_entendi"]
    assert t.ctx_updates["tentativas"] == 1


def test_telefone_invalido_usa_o_erro_especifico():
    t = handle(Estado.AGUARDANDO_TELEFONE, {}, ctx(nome="Pedro"), txt("abc"))
    assert t.novo_estado is Estado.AGUARDANDO_TELEFONE
    assert chaves(t) == ["erro_telefone"]


def test_terceira_falha_entrega_para_humano():
    t = handle(
        Estado.AGUARDANDO_TELEFONE, {}, ctx(nome="Pedro", tentativas=2), txt("abc")
    )
    assert t.novo_estado is Estado.ESCALADO_HUMANO
    assert EscalarParaHumano in tipos_efeito(t)
    assert EncerrarConversa in tipos_efeito(t)
    # Escalou ANTES de haver telefone: nao pode prometer um retorno que o time
    # nao tem como fazer.
    assert chaves(t) == ["escalonamento_sem_contato"]
    assert not t.mensagens[0].conteudo.rstrip().endswith("no .")


def test_escalonamento_com_telefone_promete_o_retorno():
    t = handle(
        Estado.AGUARDANDO_FONTE,
        LEAD_COMPLETO,
        ctx(),
        txt("quero falar com um humano"),
    )
    assert chaves(t) == ["escalonamento"]
    assert "+5511987654321" in t.mensagens[0].conteudo


def test_nenhuma_mensagem_sai_com_variavel_vazia_pendurada():
    # Rede contra o defeito de "te chama no ." em qualquer template futuro.
    for estado in (Estado.AGUARDANDO_NOME, Estado.AGUARDANDO_TELEFONE):
        t = handle(estado, {}, ctx(tentativas=2), txt("???"))
        for mensagem in t.mensagens:
            assert not mensagem.conteudo.rstrip().endswith(("no .", "para .", "em ."))


def test_limite_de_tentativas_respeita_a_politica():
    t = handle(
        Estado.AGUARDANDO_NOME,
        {},
        ctx(politicas=Politicas(max_tentativas=2), tentativas=1),
        txt("123"),
    )
    assert t.novo_estado is Estado.ESCALADO_HUMANO


def test_telefone_valido_pede_a_identidade_ao_engine():
    t = handle(Estado.AGUARDANDO_TELEFONE, {}, ctx(nome="Pedro"), txt("11987654321"))
    assert t.novo_estado is Estado.RESOLVENDO_IDENTIDADE
    assert t.ctx_updates["telefone"] == "+5511987654321"
    # A maquina NAO consulta o banco: ela descreve o que precisa e para.
    assert t.mensagens == []
    (efeito,) = t.efeitos
    assert isinstance(efeito, ResolverIdentidade)
    assert efeito.telefone == "+5511987654321"
    assert efeito.nome == "Pedro"


# --- reconhecimento: os quatro cenarios -------------------------------------


def test_reconhecimento_lead_novo_segue_para_email():
    t = handle(
        Estado.RESOLVENDO_IDENTIDADE,
        {},
        ctx(reconhecimento={"lead_conhecido": None, "opt_out_ativo": False}),
        InboundMessage(),
    )
    assert t.novo_estado is Estado.AGUARDANDO_EMAIL
    assert chaves(t) == ["pergunta_email"]


def test_reconhecimento_opt_out_ativo_pede_confirmacao_antes_de_continuar():
    t = handle(
        Estado.RESOLVENDO_IDENTIDADE,
        {},
        ctx(reconhecimento={"lead_conhecido": LEAD_COMPLETO, "opt_out_ativo": True}),
        InboundMessage(),
    )
    assert t.novo_estado is Estado.AGUARDANDO_RETOMADA_OPT_OUT
    assert chaves(t) == ["retomada_opt_out"]


def test_reconhecimento_conversa_recente_oferece_retomar():
    t = handle(
        Estado.RESOLVENDO_IDENTIDADE,
        {},
        ctx(
            reconhecimento={
                "lead_conhecido": LEAD_COMPLETO,
                "opt_out_ativo": False,
                "ultima_conversa_em": AGORA - timedelta(hours=3),
                "ultimo_estado": Estado.AGUARDANDO_FONTE.value,
            }
        ),
        InboundMessage(),
    )
    assert t.novo_estado is Estado.AGUARDANDO_RETOMADA_CONVERSA
    assert "Pedro" in t.mensagens[0].conteudo


def test_reconhecimento_conversa_antiga_pula_o_que_ja_sabe():
    # Passou da janela: conversa nova, mas sem reperguntar nada que ja esta no
    # cadastro. Com o lead completo, cai direto no carregamento de horarios.
    t = handle(
        Estado.RESOLVENDO_IDENTIDADE,
        {},
        ctx(
            reconhecimento={
                "lead_conhecido": LEAD_COMPLETO,
                "opt_out_ativo": False,
                "ultima_conversa_em": AGORA - timedelta(days=9),
                "ultimo_estado": Estado.AGUARDANDO_FONTE.value,
            }
        ),
        InboundMessage(),
    )
    assert t.novo_estado is Estado.CARREGANDO_HORARIOS
    assert chaves(t) == ["retorno_reconhecido"]
    assert tipos_efeito(t) == [GerarSlots]


def test_reconhecimento_antigo_para_no_primeiro_campo_faltante():
    parcial = {**LEAD_COMPLETO, "tipo_contrato": None, "possui_contrato": None}
    t = handle(
        Estado.RESOLVENDO_IDENTIDADE,
        {},
        ctx(
            reconhecimento={
                "lead_conhecido": parcial,
                "opt_out_ativo": False,
                "ultima_conversa_em": AGORA - timedelta(days=9),
                "ultimo_estado": Estado.AGUARDANDO_FONTE.value,
            }
        ),
        InboundMessage(),
    )
    assert t.novo_estado is Estado.AGUARDANDO_TIPO_CONTRATO
    assert chaves(t) == ["retorno_reconhecido", "pergunta_tipo_contrato"]


def test_conversa_terminada_nao_e_retomavel():
    t = handle(
        Estado.RESOLVENDO_IDENTIDADE,
        {},
        ctx(
            reconhecimento={
                "lead_conhecido": LEAD_COMPLETO,
                "opt_out_ativo": False,
                "ultima_conversa_em": AGORA - timedelta(hours=1),
                "ultimo_estado": Estado.AGENDADO.value,
            }
        ),
        InboundMessage(),
    )
    assert t.novo_estado is not Estado.AGUARDANDO_RETOMADA_CONVERSA


# --- retomadas --------------------------------------------------------------


def test_retomada_opt_out_aceita_revoga_e_segue():
    t = handle(
        Estado.AGUARDANDO_RETOMADA_OPT_OUT,
        {},
        ctx(reconhecimento={"lead_conhecido": LEAD_COMPLETO}),
        txt("1"),
    )
    assert RevogarOptOut in tipos_efeito(t)
    assert t.novo_estado is Estado.CARREGANDO_HORARIOS


def test_retomada_opt_out_recusada_mantem_encerrado():
    t = handle(Estado.AGUARDANDO_RETOMADA_OPT_OUT, {}, ctx(), txt("2"))
    assert t.novo_estado is Estado.OPT_OUT
    assert chaves(t) == ["opt_out_mantido"]
    # Ja estava suprimido: nao registra opt-out de novo.
    assert RegistrarOptOut not in tipos_efeito(t)


def test_retomada_conversa_volta_ao_estado_anterior():
    t = handle(
        Estado.AGUARDANDO_RETOMADA_CONVERSA,
        {},
        ctx(
            reconhecimento={
                "lead_conhecido": LEAD_COMPLETO,
                "ultimo_estado": Estado.AGUARDANDO_FONTE.value,
            }
        ),
        txt("1"),
    )
    assert t.novo_estado is Estado.AGUARDANDO_FONTE
    assert chaves(t) == ["pergunta_fonte"]


def test_retomada_conversa_recusada_recomeca_a_coleta():
    t = handle(
        Estado.AGUARDANDO_RETOMADA_CONVERSA,
        {},
        ctx(reconhecimento={"lead_conhecido": LEAD_COMPLETO}),
        txt("2"),
    )
    assert t.novo_estado is Estado.AGUARDANDO_EMAIL


# --- coleta -----------------------------------------------------------------


def test_email_avanca_para_fonte():
    t = handle(Estado.AGUARDANDO_EMAIL, {}, ctx(), txt("pedro@exemplo.com.br"))
    assert t.novo_estado is Estado.AGUARDANDO_FONTE
    assert t.lead_updates["email"] == "pedro@exemplo.com.br"


def test_fonte_guarda_normalizado_e_original():
    t = handle(Estado.AGUARDANDO_FONTE, {"email": "a@b.com"}, ctx(), txt("um amigo indicou"))
    assert t.novo_estado is Estado.AGUARDANDO_TIPO_CONTRATO
    assert t.lead_updates["fonte"] == "indicacao"
    assert t.lead_updates["fonte_raw"] == "um amigo indicou"


def test_tipo_contrato_avanca_para_tem_contrato():
    lead = {"email": "a@b.com", "fonte": "google"}
    t = handle(Estado.AGUARDANDO_TIPO_CONTRATO, lead, ctx(), txt("cartão"))
    assert t.novo_estado is Estado.AGUARDANDO_TEM_CONTRATO
    assert t.lead_updates["tipo_contrato"] == "cartao_credito"
    assert t.lead_updates["tipo_contrato_raw"] == "cartão"


def test_tipo_contrato_invalido_usa_o_erro_especifico():
    t = handle(Estado.AGUARDANDO_TIPO_CONTRATO, {}, ctx(), txt("sei la"))
    assert chaves(t) == ["erro_tipo_contrato"]
    assert t.novo_estado is Estado.AGUARDANDO_TIPO_CONTRATO


def test_tem_contrato_sim_pede_o_arquivo():
    lead = {"email": "a@b.com", "fonte": "google", "tipo_contrato": "pessoal"}
    t = handle(Estado.AGUARDANDO_TEM_CONTRATO, lead, ctx(), txt("sim"))
    assert t.novo_estado is Estado.AGUARDANDO_ENVIO_CONTRATO
    assert t.lead_updates["possui_contrato"] is True


def test_tem_contrato_nao_vai_direto_aos_horarios():
    lead = {"email": "a@b.com", "fonte": "google", "tipo_contrato": "pessoal"}
    t = handle(Estado.AGUARDANDO_TEM_CONTRATO, lead, ctx(), txt("não tenho"))
    assert t.novo_estado is Estado.CARREGANDO_HORARIOS
    assert t.lead_updates["possui_contrato"] is False
    assert chaves(t) == ["sem_contrato"]


# --- documento --------------------------------------------------------------


def test_anexo_e_arquivado_e_segue_para_horarios():
    anexo = Anexo("contrato.pdf", "application/pdf", 1024, "C:/tmp/contrato.pdf")
    t = handle(
        Estado.AGUARDANDO_ENVIO_CONTRATO,
        LEAD_COMPLETO,
        ctx(),
        InboundMessage(anexo=anexo),
    )
    assert t.novo_estado is Estado.CARREGANDO_HORARIOS
    assert chaves(t) == ["contrato_recebido"]
    assert tipos_efeito(t) == [SalvarDocumento, GerarSlots]


def test_contrato_recebido_diz_que_a_analise_e_humana():
    anexo = Anexo("c.pdf", "application/pdf", 10, "x")
    t = handle(
        Estado.AGUARDANDO_ENVIO_CONTRATO, LEAD_COMPLETO, ctx(), InboundMessage(anexo=anexo)
    )
    assert "consultor" in t.mensagens[0].conteudo


def test_pular_o_envio():
    t = handle(Estado.AGUARDANDO_ENVIO_CONTRATO, LEAD_COMPLETO, ctx(), txt("PULAR"))
    assert t.novo_estado is Estado.CARREGANDO_HORARIOS
    assert chaves(t) == ["contrato_pulado"]


def test_anexo_pula_o_guardrail_de_escopo():
    # Um arquivo chamado taxa.pdf chegaria no guardrail se o adaptador entregasse
    # texto. Como chega tipado, nem passa por la.
    anexo = Anexo("taxa.pdf", "application/pdf", 10, "C:/tmp/taxa.pdf")
    t = handle(
        Estado.AGUARDANDO_ENVIO_CONTRATO, LEAD_COMPLETO, ctx(), InboundMessage(anexo=anexo)
    )
    assert chaves(t) == ["contrato_recebido"]


# --- agendamento ------------------------------------------------------------


def test_carregar_horarios_oferta_a_lista():
    t = handle(
        Estado.CARREGANDO_HORARIOS, LEAD_COMPLETO, ctx(slots_ofertados=SLOTS), InboundMessage()
    )
    assert t.novo_estado is Estado.AGUARDANDO_ESCOLHA_HORARIO
    conteudo = t.mensagens[0].conteudo
    assert "1 - quinta-feira, 27 de agosto às 14:00" in conteudo
    assert "2 - quinta-feira, 27 de agosto às 15:15" in conteudo


def test_sem_horarios_escala_para_humano():
    t = handle(
        Estado.CARREGANDO_HORARIOS, LEAD_COMPLETO, ctx(slots_ofertados=[]), InboundMessage()
    )
    assert t.novo_estado is Estado.ESCALADO_HUMANO
    assert chaves(t) == ["sem_horarios"]
    assert "+5511987654321" in t.mensagens[0].conteudo


def test_escolha_de_horario_pede_confirmacao_com_o_consultor():
    t = handle(
        Estado.AGUARDANDO_ESCOLHA_HORARIO, LEAD_COMPLETO, ctx(slots_ofertados=SLOTS), txt("2")
    )
    assert t.novo_estado is Estado.AGUARDANDO_CONFIRMACAO
    assert t.ctx_updates["slot_escolhido"]["consultor_nome"] == "Bruno Tavares"
    assert "Bruno Tavares" in t.mensagens[0].conteudo


def test_horario_fora_do_menu_repete():
    t = handle(
        Estado.AGUARDANDO_ESCOLHA_HORARIO, LEAD_COMPLETO, ctx(slots_ofertados=SLOTS), txt("9")
    )
    assert t.novo_estado is Estado.AGUARDANDO_ESCOLHA_HORARIO
    assert chaves(t) == ["nao_entendi"]


def test_horario_em_linguagem_natural_nao_e_interpretado():
    # Decisao de projeto: data em texto livre NAO e interpretada. Errar aqui
    # significa marcar reuniao no dia errado.
    t = handle(
        Estado.AGUARDANDO_ESCOLHA_HORARIO,
        LEAD_COMPLETO,
        ctx(slots_ofertados=SLOTS),
        txt("pode ser amanhã de tarde"),
    )
    assert chaves(t) == ["nao_entendi"]


def test_confirmacao_reserva():
    t = handle(
        Estado.AGUARDANDO_CONFIRMACAO, LEAD_COMPLETO, ctx(slot_escolhido=SLOTS[0]), txt("1")
    )
    assert t.novo_estado is Estado.RESERVANDO
    (efeito,) = t.efeitos
    assert isinstance(efeito, ReservarSlot)
    assert efeito.indice == 1


def test_confirmacao_recusada_reoferta():
    t = handle(
        Estado.AGUARDANDO_CONFIRMACAO, LEAD_COMPLETO, ctx(slot_escolhido=SLOTS[0]), txt("2")
    )
    assert t.novo_estado is Estado.CARREGANDO_HORARIOS
    assert tipos_efeito(t) == [GerarSlots]


def test_reserva_confirmada_encerra_agendado():
    t = handle(
        Estado.RESERVANDO,
        LEAD_COMPLETO,
        ctx(slot_escolhido=SLOTS[0], reserva={"ok": True}),
        InboundMessage(),
    )
    assert t.novo_estado is Estado.AGENDADO
    conteudo = t.mensagens[0].conteudo
    assert "Ana Ribeiro" in conteudo
    assert "pedro@exemplo.com.br" in conteudo


def test_reserva_perdida_reoferta_a_lista_recalculada():
    t = handle(
        Estado.RESERVANDO,
        LEAD_COMPLETO,
        ctx(slot_escolhido=SLOTS[0], reserva={"ok": False}),
        InboundMessage(),
    )
    assert t.novo_estado is Estado.CARREGANDO_HORARIOS
    assert chaves(t) == ["slot_perdido"]
    assert tipos_efeito(t) == [GerarSlots]


# --- comandos globais -------------------------------------------------------


ESTADOS_COM_ESPERA = [e for e in PERGUNTA_DO_ESTADO]


@pytest.mark.parametrize("estado", ESTADOS_COM_ESPERA)
def test_sair_funciona_em_qualquer_estado(estado):
    t = handle(estado, LEAD_COMPLETO, ctx(slots_ofertados=SLOTS), txt("sair"))
    assert t.novo_estado is Estado.OPT_OUT
    assert chaves(t) == ["opt_out"]
    assert RegistrarOptOut in tipos_efeito(t)


@pytest.mark.parametrize("estado", ESTADOS_COM_ESPERA)
def test_atendente_funciona_em_qualquer_estado(estado):
    t = handle(estado, LEAD_COMPLETO, ctx(slots_ofertados=SLOTS), txt("quero um atendente"))
    assert t.novo_estado is Estado.ESCALADO_HUMANO


def test_sair_antes_do_telefone_nao_registra_opt_out():
    # Nao se bloqueia um contato desconhecido: nao ha telefone nem e-mail para
    # suprimir, entao a conversa fecha sem sujar a lista de supressao.
    t = handle(Estado.AGUARDANDO_NOME, {}, ctx(), txt("sair"))
    assert t.novo_estado is Estado.OPT_OUT
    assert tipos_efeito(t) == [EncerrarConversa]


def test_sair_depois_do_telefone_registra_opt_out():
    t = handle(Estado.AGUARDANDO_EMAIL, {}, ctx(telefone="+5511987654321"), txt("sair"))
    assert RegistrarOptOut in tipos_efeito(t)


def test_voltar_retorna_ao_estado_anterior():
    t = handle(
        Estado.AGUARDANDO_FONTE,
        {},
        ctx(estado_anterior=Estado.AGUARDANDO_EMAIL.value),
        txt("voltar"),
    )
    assert t.novo_estado is Estado.AGUARDANDO_EMAIL
    assert chaves(t) == ["pergunta_email"]


def test_voltar_sem_historico_fica_onde_esta():
    t = handle(Estado.AGUARDANDO_EMAIL, {}, ctx(), txt("voltar"))
    assert t.novo_estado is Estado.AGUARDANDO_EMAIL


# --- escopo proibido --------------------------------------------------------


def test_fora_de_escopo_desvia_e_devolve_ao_fluxo():
    t = handle(Estado.AGUARDANDO_FONTE, {}, ctx(), txt("quanto vocês cobram?"))
    assert t.novo_estado is Estado.AGUARDANDO_FONTE
    assert chaves(t) == ["fora_de_escopo", "pergunta_fonte"]


def test_fora_de_escopo_nao_da_numero():
    t = handle(Estado.AGUARDANDO_FONTE, {}, ctx(), txt("qual a taxa de juros?"))
    conteudo = t.mensagens[0].conteudo.lower()
    assert "consultor" in conteudo
    assert "%" not in conteudo


def test_fora_de_escopo_nao_conta_como_erro_de_parsing():
    t = handle(Estado.AGUARDANDO_FONTE, {}, ctx(tentativas=2), txt("quanto custa?"))
    assert t.novo_estado is Estado.AGUARDANDO_FONTE


# --- integridade do mapa ----------------------------------------------------


def test_todo_estado_de_espera_sabe_repetir_a_propria_pergunta():
    esperando = set(Estado) - ESTADOS_TERMINAIS - ESTADOS_INTERNOS - {Estado.INICIO}
    assert esperando == set(PERGUNTA_DO_ESTADO)


def test_terminais_nao_transicionam():
    for estado in ESTADOS_TERMINAIS:
        t = handle(estado, LEAD_COMPLETO, ctx(), txt("oi"))
        assert t.novo_estado is estado
        assert t.mensagens == []
