"""Tradução do vocabulário da Marina para o do juridico.

Funções puras, sem rede. São a parte que mais muda quando o juridico mudar, e
testar isso não pode depender de um servidor no ar.

Duas regras que governam este arquivo:

1. **Nada se perde.** O juridico aceita menos valores do que a Marina coleta.
   Sempre que a tradução empobrece o dado, o valor verdadeiro vai junto no texto
   da qualificação, que é campo livre.
2. **Traduzir, não inventar.** Quando não há equivalente, cai num padrão
   explícito e o original fica registrado — nunca se descarta em silêncio.
"""

from __future__ import annotations

from app.enums import Fonte, TipoContrato

#: As ÚNICAS origens que o juridico aceita (`ORIGENS_LEAD` em crm.routes.ts).
#: Mandar qualquer outra coisa devolve 400 "Origem do lead invalida".
ORIGENS_JURIDICO: frozenset[str] = frozenset(
    {"INSTAGRAM_META_ADS", "GOOGLE_ADS", "TV", "INDICACAO"}
)

#: A Marina coleta 7 fontes; o juridico conhece 4. Este mapa é a perda, e ela é
#: deliberada e documentada — a fonte real vai no texto da qualificação.
FONTE_PARA_ORIGEM: dict[Fonte, str] = {
    Fonte.ANUNCIO: "INSTAGRAM_META_ADS",
    Fonte.GOOGLE: "GOOGLE_ADS",
    Fonte.INDICACAO: "INDICACAO",
    # Sem equivalente no juridico. INDICACAO é o padrão menos enganoso: não
    # afirma um canal pago que não existiu.
    Fonte.WHATSAPP: "INDICACAO",
    Fonte.EMAIL: "INDICACAO",
    Fonte.LINKEDIN: "INDICACAO",
    Fonte.OUTRO: "INDICACAO",
}

#: Fontes cuja tradução perde informação. Usado para decidir se o texto da
#: qualificação precisa explicitar a origem verdadeira.
FONTES_SEM_EQUIVALENTE: frozenset[Fonte] = frozenset(
    {Fonte.WHATSAPP, Fonte.EMAIL, Fonte.LINKEDIN, Fonte.OUTRO}
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


def origem_lead(fonte: str | None) -> str:
    """Fonte da Marina -> `origemLead` aceito pelo juridico."""
    if not fonte:
        return "INDICACAO"
    try:
        return FONTE_PARA_ORIGEM[Fonte(fonte)]
    except ValueError:
        # Fonte que a Marina passou a coletar e este mapa ainda não conhece.
        # Cair no padrão é melhor do que derrubar o envio do lead.
        return "INDICACAO"


def telefone_para_juridico(telefone_e164: str | None) -> str:
    """+5511987654321 -> 11987654321.

    O juridico guarda o telefone sem código de país e compara pelos últimos 8
    dígitos. Mandar em E.164 funcionaria na deduplicação, mas deixaria o número
    exibido diferente de todos os outros do CRM.
    """
    if not telefone_e164:
        return ""
    digitos = "".join(c for c in telefone_e164 if c.isdigit())
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        return digitos[2:]
    return digitos


def texto_qualificacao(lead: dict) -> str:
    """Resumo que vai no `observacao` do primeiro contato.

    É por aqui que a qualificação chega ao juridico: tipo de contrato e "tem o
    contrato em mãos" não têm campo próprio lá, e o `metadadosJson` é calculado
    no servidor, que ignora o que o cliente manda.
    """
    partes: list[str] = ["Lead qualificado pela Marina (automação de SDR)."]

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
            # O campo origemLead do juridico não consegue representar esta fonte,
            # então ela precisa aparecer escrita para não sumir.
            partes.append(f"Origem real: {rotulo} (registrada como INDICACAO).")
        else:
            partes.append(f"Origem: {rotulo}.")

    bruto = lead.get("fonte_raw")
    if bruto and bruto.strip().isdigit() is False:
        partes.append(f'Disse: "{bruto.strip()}".')

    return " ".join(partes)


def payload_lead(lead: dict) -> dict:
    """Corpo do POST /api/admin/leads.

    `consultorId` é omitido de propósito: sem ele o juridico aplica o rodízio
    dele, que é o comportamento desejado. A Marina decide QUANDO é a reunião; o
    juridico decide de QUEM é o lead.
    """
    return {
        "nomeCompleto": lead.get("nome") or "Lead sem nome",
        "email": lead.get("email") or "",
        "telefoneCelular": telefone_para_juridico(lead.get("telefone_e164")),
        "origemLead": origem_lead(lead.get("fonte")),
        "campanha": "MARINA",
    }


def payload_agendamento(agendamento: dict) -> dict:
    """Corpo do POST /api/admin/leads/:id/agendamentos."""
    corpo = {
        "dataHora": agendamento["inicio_utc"],
        "dataFim": agendamento.get("fim_utc"),
        "assunto": agendamento.get(
            "assunto", "Reunião de revisão de contrato (agendada pela Marina)"
        ),
        "observacao": agendamento.get("observacao"),
    }
    consultor = agendamento.get("juridico_consultor_id")
    if consultor:
        # Só quando o consultor da Marina está ligado a um usuário real do
        # juridico. Sem isso o juridico usa o consultor do lead, e o nome que a
        # Marina prometeu ao lead poderia não bater.
        corpo["consultorId"] = consultor
    return {chave: valor for chave, valor in corpo.items() if valor is not None}
