"""Tradução do vocabulário da Marina para o do juridico.

Funções puras, sem rede. São a parte que mais muda quando o juridico mudar, e
testar isso não pode depender de um servidor no ar.

Duas regras que governam este arquivo:

1. **Nada se perde.** O juridico aceita menos valores do que a Marina coleta.
   Sempre que a tradução empobrece o dado, o valor verdadeiro vai junto no texto
   da qualificação, que é campo livre.
2. **Traduzir, não inventar.** Quando não há equivalente, cai num padrão
   explícito e o original fica registrado — nunca se descarta em silêncio.

No juridico não existe "lead" como entidade: o que existe é uma **negociação**
pendurada num **contato**, e a reunião é uma **tarefa** presa a essa negociação.
"""

from __future__ import annotations

from app.enums import Fonte, TipoContrato

#: Limite da coluna `tarefas.assunto` no juridico. Estourar devolve 422, que é
#: erro permanente — o agendamento morreria na fila por causa de um nome longo.
MAX_ASSUNTO = 180

#: As ÚNICAS fontes que o juridico aceita (`Negociacao::FONTES`). Qualquer outro
#: valor é recusado na validação.
FONTES_CRM: frozenset[str] = frozenset(
    {
        "cliente_ativo",
        "contato_site",
        "contato_email",
        "contato_telefone",
        "email_marketing",
        "feiras_eventos",
        "buscadores",
        "indicacao_cliente",
        "indicacao_parcerias",
        "prospeccao_ativa",
        "redes_sociais",
    }
)

#: A Marina coleta 7 fontes; o juridico conhece 11, mas o recorte é outro — ele
#: pensa em canal de entrada, não em mídia. Onde o encaixe é exato, traduzimos;
#: onde não é, a fonte real vai escrita no texto da qualificação.
FONTE_PARA_CRM: dict[Fonte, str] = {
    Fonte.GOOGLE: "buscadores",
    Fonte.INDICACAO: "indicacao_cliente",
    Fonte.EMAIL: "contato_email",
    # O anúncio da Leve roda em Instagram/Meta; "redes sociais" é o mais próximo,
    # mas perde a informação de que foi anúncio pago.
    Fonte.ANUNCIO: "redes_sociais",
    Fonte.LINKEDIN: "redes_sociais",
    # Ganho em relação ao CRM antigo, que não tinha nada disso e engolia tudo
    # como "indicação": a conversa aconteceu mesmo por telefone/WhatsApp.
    Fonte.WHATSAPP: "contato_telefone",
    Fonte.OUTRO: "contato_telefone",
}

#: Fonte usada quando não há tradução possível. Descreve o canal por onde a
#: conversa de fato chegou, que é o mais honesto que dá para afirmar.
FONTE_PADRAO = "contato_telefone"

#: Fontes cuja tradução perde informação. Usado para decidir se o texto da
#: qualificação precisa explicitar a origem verdadeira.
FONTES_SEM_EQUIVALENTE: frozenset[Fonte] = frozenset(
    {Fonte.ANUNCIO, Fonte.LINKEDIN, Fonte.OUTRO}
)

ROTULO_TIPO_CONTRATO: dict[str, str] = {
    TipoContrato.CONSIGNADO.value: "empréstimo consignado",
    TipoContrato.PESSOAL.value: "empréstimo pessoal",
    TipoContrato.CARTAO_CREDITO.value: "cartão de crédito",
}

ROTULO_FONTE: dict[str, str] = {
    Fonte.ANUNCIO.value: "anúncio",
    Fonte.WHATSAPP.value: "WhatsApp",
    Fonte.EMAIL.value: "e-mail",
    Fonte.LINKEDIN.value: "LinkedIn",
    Fonte.INDICACAO.value: "indicação",
    Fonte.GOOGLE.value: "Google",
    Fonte.OUTRO.value: "outro",
}


def fonte_crm(fonte: str | None) -> str:
    """Fonte da Marina -> `fonte` aceita pelo juridico."""
    if not fonte:
        return FONTE_PADRAO
    try:
        return FONTE_PARA_CRM[Fonte(fonte)]
    except ValueError:
        # Fonte que a Marina passou a coletar e este mapa ainda não conhece.
        # Cair no padrão é melhor do que derrubar o envio do lead.
        return FONTE_PADRAO


def telefone_para_crm(telefone_e164: str | None) -> str:
    """+5511987654321 -> 11987654321.

    O juridico guarda telefone só com dígitos e sem código de país. Mandar em
    E.164 deixaria o número exibido diferente de todos os outros do CRM, e é
    também neste formato que ele procura contato já cadastrado.
    """
    if not telefone_e164:
        return ""
    digitos = "".join(c for c in telefone_e164 if c.isdigit())
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        return digitos[2:]
    return digitos


def texto_qualificacao(lead: dict, canal: str | None = None) -> str:
    """Resumo que vira observação no histórico da negociação.

    É por aqui que a qualificação chega ao juridico: tipo de contrato e "tem o
    contrato em mãos" não têm campo próprio lá.
    """
    partes: list[str] = ["Lead qualificado pela Marina (automação de SDR)."]

    if canal:
        partes.append(f"Primeiro contato por {canal}.")

    tipo = lead.get("tipo_contrato")
    if tipo:
        rotulo = ROTULO_TIPO_CONTRATO.get(tipo, tipo)
        partes.append(f"Quer revisar: {rotulo}.")

    possui = lead.get("possui_contrato")
    if possui is True:
        partes.append("Tem o contrato em mãos.")
    elif possui is False:
        partes.append("NÃO tem o contrato em mãos — orientar sobre a segunda via.")

    fonte = lead.get("fonte")
    if fonte:
        rotulo = ROTULO_FONTE.get(fonte, fonte)
        try:
            perdeu = Fonte(fonte) in FONTES_SEM_EQUIVALENTE
        except ValueError:
            perdeu = True
        if perdeu:
            registrada = fonte_crm(fonte)
            # A lista de fontes do juridico não consegue representar esta
            # origem, então ela precisa aparecer escrita para não sumir.
            partes.append(f"Origem real: {rotulo} (registrada como {registrada}).")
        else:
            partes.append(f"Origem: {rotulo}.")

    bruto = lead.get("fonte_raw")
    if bruto and bruto.strip().isdigit() is False:
        partes.append(f'Disse: "{bruto.strip()}".')

    return " ".join(partes)


def payload_negociacao(lead: dict, canal: str | None = None) -> dict:
    """Corpo do POST que cria a negociação.

    `referencia_externa` é o id do lead aqui dentro, e é o que impede que um
    reenvio vire duas negociações: um tempo esgotado depois do commit remoto é
    indistinguível de uma falha, e a Marina, corretamente, tenta de novo.

    Nada de responsável: a negociação nasce no nome do usuário de serviço, que é
    como "em aberto" se representa naquele CRM.
    """
    payload = {
        "referencia_externa": str(lead.get("lead_id") or ""),
        "nome": lead.get("nome") or "Lead sem nome",
        "telefone": telefone_para_crm(lead.get("telefone_e164")),
        "fonte": fonte_crm(lead.get("fonte")),
        "observacao": texto_qualificacao(lead, canal),
    }
    email = lead.get("email")
    if email:
        # Omitir é mais honesto que mandar vazio: o juridico valida o formato.
        payload["email"] = email
    return payload


def payload_tarefa(agendamento: dict, lead: dict) -> dict:
    """Corpo do POST que cria a reunião como tarefa da negociação.

    Duas decisões desta tradução:

    **Ninguém é indicado como responsável.** O juridico exige responsável em toda
    tarefa e o define a partir de quem chamou a API, então "em aberto" é uma
    tarefa parada no nome do usuário de serviço. Quem tiver disponibilidade troca
    o responsável para si e a tarefa passa a ser dele.

    **Quem é o lead vai também no texto.** A tarefa já nasce presa à negociação,
    mas quem abre o calendário vê o assunto antes de qualquer outra coisa.
    """
    nome = lead.get("nome") or "Lead sem nome"
    contato = lead.get("telefone_e164") or lead.get("email") or "sem contato"

    linhas = ["Reunião agendada pela Marina.", f"Lead: {nome} — {contato}."]
    qualificacao = texto_qualificacao(lead)
    if qualificacao:
        linhas.append(qualificacao)
    linhas.append("Tarefa em aberto: quem for atender, assuma como responsável.")

    return {
        "assunto": f"Reunião — {nome} ({contato})"[:MAX_ASSUNTO],
        "tipo": "reuniao",
        # O engine já converteu para o fuso de exibição; o juridico grava hora de
        # parede, e os dois lados estão em America/Sao_Paulo.
        "agendada_para": f"{agendamento['data_iso']} {agendamento['hora']}:00",
        "observacao": "\n".join(linhas),
    }
