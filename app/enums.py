"""Vocabulario do dominio.

Os estados sao a espinha dorsal da maquina. RESOLVENDO_IDENTIDADE e um estado
interno: a maquina nunca espera o lead digitar nele, ela so o atravessa quando o
engine devolve o resultado do efeito ResolverIdentidade.
"""

from enum import StrEnum


class Estado(StrEnum):
    INICIO = "INICIO"
    AGUARDANDO_NOME = "AGUARDANDO_NOME"
    AGUARDANDO_TELEFONE = "AGUARDANDO_TELEFONE"
    RESOLVENDO_IDENTIDADE = "RESOLVENDO_IDENTIDADE"
    AGUARDANDO_RETOMADA_OPT_OUT = "AGUARDANDO_RETOMADA_OPT_OUT"
    AGUARDANDO_RETOMADA_CONVERSA = "AGUARDANDO_RETOMADA_CONVERSA"
    AGUARDANDO_EMAIL = "AGUARDANDO_EMAIL"
    AGUARDANDO_FONTE = "AGUARDANDO_FONTE"
    AGUARDANDO_TIPO_CONTRATO = "AGUARDANDO_TIPO_CONTRATO"
    AGUARDANDO_TEM_CONTRATO = "AGUARDANDO_TEM_CONTRATO"
    AGUARDANDO_ENVIO_CONTRATO = "AGUARDANDO_ENVIO_CONTRATO"
    CARREGANDO_HORARIOS = "CARREGANDO_HORARIOS"
    AGUARDANDO_ESCOLHA_HORARIO = "AGUARDANDO_ESCOLHA_HORARIO"
    AGUARDANDO_CONFIRMACAO = "AGUARDANDO_CONFIRMACAO"
    RESERVANDO = "RESERVANDO"
    AGENDADO = "AGENDADO"
    OPT_OUT = "OPT_OUT"
    ESCALADO_HUMANO = "ESCALADO_HUMANO"


#: Estados que encerram a conversa. Nao ha transicao saindo deles.
ESTADOS_TERMINAIS: frozenset[Estado] = frozenset(
    {Estado.AGENDADO, Estado.OPT_OUT, Estado.ESCALADO_HUMANO}
)

#: Estados internos: a maquina nao espera digitacao neles. Cada um marca uma
#: fronteira de I/O — a maquina emite o efeito, o engine executa e preenche o
#: ctx, e entao reinvoca handle() com entrada vazia. E assim que a funcao
#: continua pura mesmo precisando de banco no meio do fluxo.
ESTADOS_INTERNOS: frozenset[Estado] = frozenset(
    {
        Estado.RESOLVENDO_IDENTIDADE,
        Estado.CARREGANDO_HORARIOS,
        Estado.RESERVANDO,
    }
)


class Direcao(StrEnum):
    ENTRADA = "in"
    SAIDA = "out"


class StatusAgendamento(StrEnum):
    AGENDADO = "agendado"
    CANCELADO = "cancelado"
    REALIZADO = "realizado"


class Fonte(StrEnum):
    ANUNCIO = "anuncio"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    LINKEDIN = "linkedin"
    INDICACAO = "indicacao"
    GOOGLE = "google"
    OUTRO = "outro"


class TipoContrato(StrEnum):
    CONSIGNADO = "consignado"
    PESSOAL = "pessoal"
    CARTAO_CREDITO = "cartao_credito"


class Canal(StrEnum):
    TERMINAL = "terminal"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
