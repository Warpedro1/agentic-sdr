"""A maquina de estados da Marina. FUNCAO PURA.

Regra que governa este arquivo inteiro: nada aqui toca banco, rede, disco ou
relogio. `handle` recebe tudo o que precisa e devolve o que deve acontecer; quem
executa e o engine.

Isso nao e purismo. E o que permite `tests/test_machine.py` cobrir todas as
transicoes em milissegundos, sem subir Postgres — e `tests/test_machine_pura.py`
falha se alguem importar sqlalchemy, os, pathlib ou zoneinfo aqui.

Consequencias praticas, todas deliberadas:

- O horario atual chega em `ctx["agora"]`, nao de `datetime.now()`. Testes ficam
  deterministicos.
- Os limites (tentativas, janela de retomada) chegam em `ctx["politicas"]`, nao
  de `app.config`, que leria o .env.
- Datas ja chegam formatadas em `ctx["slots_ofertados"]`, porque converter para
  America/Sao_Paulo leria a base de fusos do sistema.
- Um anexo chega resolvido em `entrada.anexo`, porque descobrir se um texto e um
  caminho de arquivo exigiria olhar o disco.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.channels.base import Anexo, InboundMessage, MensagemSaida
from app.enums import ESTADOS_INTERNOS, ESTADOS_TERMINAIS, Estado
from app.guards import ComandoGlobal, comando_global, fora_de_escopo
from app.parsers import (
    parse_email,
    parse_fonte,
    parse_nome,
    parse_opcao_menu,
    parse_sim_nao,
    parse_telefone,
    parse_tipo_contrato,
    primeiro_nome,
    quer_pular,
)
from app.templates import CatalogoMensagens

# --- efeitos ----------------------------------------------------------------


@dataclass(frozen=True)
class Efeito:
    """Algo que precisa de I/O. A maquina descreve; o engine executa."""


@dataclass(frozen=True)
class ResolverIdentidade(Efeito):
    """Find-or-create do lead pelo telefone + opt-out ativo + ultima conversa.

    Uma operacao so, numa transacao so: o lead nasce aqui, e nao antes, porque a
    deduplicacao e por telefone e o telefone so existe agora.
    """

    telefone: str
    nome: str | None
    email: str | None
    consentimento_em: datetime | None
    canal: str


@dataclass(frozen=True)
class GerarSlots(Efeito):
    """Calcula os horarios livres e ja resolve quem atenderia cada um."""


@dataclass(frozen=True)
class ReservarSlot(Efeito):
    indice: int


@dataclass(frozen=True)
class SalvarDocumento(Efeito):
    anexo: Anexo


@dataclass(frozen=True)
class RegistrarOptOut(Efeito):
    motivo: str


@dataclass(frozen=True)
class RevogarOptOut(Efeito):
    pass


@dataclass(frozen=True)
class EscalarParaHumano(Efeito):
    motivo: str


@dataclass(frozen=True)
class EncerrarConversa(Efeito):
    pass


# --- transicao --------------------------------------------------------------


@dataclass(frozen=True)
class Transicao:
    novo_estado: Estado
    lead_updates: dict[str, Any] = field(default_factory=dict)
    ctx_updates: dict[str, Any] = field(default_factory=dict)
    mensagens: list[MensagemSaida] = field(default_factory=list)
    efeitos: list[Efeito] = field(default_factory=list)


@dataclass(frozen=True)
class Politicas:
    max_tentativas: int = 3
    janela_retomada_horas: int = 24
    horizonte_dias: int = 7


# --- mapa do fluxo ----------------------------------------------------------

#: Campo do lead -> estado que o coleta -> template da pergunta. A ordem E o fluxo:
#: quando um lead e reconhecido, a Marina caminha por esta lista e para no
#: primeiro campo que ainda nao sabe.
SEQUENCIA_COLETA: tuple[tuple[str, Estado, str], ...] = (
    ("email", Estado.AGUARDANDO_EMAIL, "pergunta_email"),
    ("fonte", Estado.AGUARDANDO_FONTE, "pergunta_fonte"),
    ("tipo_contrato", Estado.AGUARDANDO_TIPO_CONTRATO, "pergunta_tipo_contrato"),
    ("possui_contrato", Estado.AGUARDANDO_TEM_CONTRATO, "pergunta_contrato"),
)

#: Pergunta que cada estado repete quando o lead responde algo que nao da para
#: entender, e quando ele digita "voltar".
PERGUNTA_DO_ESTADO: dict[Estado, str] = {
    Estado.AGUARDANDO_NOME: "pergunta_nome",
    Estado.AGUARDANDO_TELEFONE: "pergunta_telefone",
    Estado.AGUARDANDO_EMAIL: "pergunta_email",
    Estado.AGUARDANDO_FONTE: "pergunta_fonte",
    Estado.AGUARDANDO_TIPO_CONTRATO: "pergunta_tipo_contrato",
    Estado.AGUARDANDO_TEM_CONTRATO: "pergunta_contrato",
    Estado.AGUARDANDO_ENVIO_CONTRATO: "pedido_contrato",
    Estado.AGUARDANDO_ESCOLHA_HORARIO: "oferta_horarios",
    Estado.AGUARDANDO_CONFIRMACAO: "confirmacao",
    Estado.AGUARDANDO_RETOMADA_OPT_OUT: "retomada_opt_out",
    Estado.AGUARDANDO_RETOMADA_CONVERSA: "retomada_conversa",
}


# --- helpers puros ----------------------------------------------------------


def _politicas(ctx: dict) -> Politicas:
    bruto = ctx.get("politicas")
    if isinstance(bruto, Politicas):
        return bruto
    if isinstance(bruto, dict):
        return Politicas(**bruto)
    return Politicas()


def _nome_de(lead: dict, ctx: dict) -> str:
    nome = lead.get("nome") or ctx.get("nome") or ""
    return primeiro_nome(nome) if nome else "tudo bem"


def _lista_slots(ctx: dict) -> str:
    slots = ctx.get("slots_ofertados") or []
    return "\n".join(
        f"{indice} - {slot['data_extenso']} às {slot['hora']}"
        for indice, slot in enumerate(slots, start=1)
    )


def _valores_do_template(chave: str, lead: dict, ctx: dict) -> dict[str, Any]:
    """Cada template so recebe o que ele declara. Faltar variavel e erro, nao vazio."""
    politicas = _politicas(ctx)
    escolhido = ctx.get("slot_escolhido") or {}
    return {
        "pergunta_telefone": lambda: {"nome": _nome_de(lead, ctx)},
        "contrato_recebido": lambda: {"nome": _nome_de(lead, ctx)},
        "retomada_conversa": lambda: {"nome": _nome_de(lead, ctx)},
        "retorno_reconhecido": lambda: {"nome": _nome_de(lead, ctx)},
        "oferta_horarios": lambda: {"lista_slots": _lista_slots(ctx)},
        "sem_horarios": lambda: {
            "dias": politicas.horizonte_dias,
            "telefone": lead.get("telefone_e164") or ctx.get("telefone") or "",
        },
        "escalonamento": lambda: {
            "telefone": lead.get("telefone_e164") or ctx.get("telefone") or ""
        },
        "confirmacao": lambda: {
            "data_extenso": escolhido.get("data_extenso", ""),
            "hora": escolhido.get("hora", ""),
            "consultor": escolhido.get("consultor_nome", ""),
        },
        "agendado": lambda: {
            "nome": _nome_de(lead, ctx),
            "data_extenso": escolhido.get("data_extenso", ""),
            "hora": escolhido.get("hora", ""),
            "consultor": escolhido.get("consultor_nome", ""),
            "email": lead.get("email") or ctx.get("email") or "",
        },
    }.get(chave, dict)()


def _msg(catalogo: CatalogoMensagens, chave: str, lead: dict, ctx: dict) -> MensagemSaida:
    return catalogo.render(chave, **_valores_do_template(chave, lead, ctx))


def _repetir_pergunta(
    catalogo: CatalogoMensagens, estado: Estado, lead: dict, ctx: dict
) -> MensagemSaida:
    chave = PERGUNTA_DO_ESTADO.get(estado, "pergunta_nome")
    return _msg(catalogo, chave, lead, ctx)


def _falha_de_parsing(
    catalogo: CatalogoMensagens, estado: Estado, lead: dict, ctx: dict
) -> Transicao:
    """Nao entendeu: repete a pergunta. Na 3a vez, entrega para um humano.

    Sem este limite o lead fica em loop e abandona em silencio, que e a pior
    saida possivel — ninguem fica sabendo que ele desistiu.
    """
    politicas = _politicas(ctx)
    tentativas = int(ctx.get("tentativas", 0)) + 1

    if tentativas >= politicas.max_tentativas:
        return Transicao(
            novo_estado=Estado.ESCALADO_HUMANO,
            ctx_updates={"tentativas": 0},
            mensagens=[_msg(catalogo, "escalonamento", lead, ctx)],
            efeitos=[
                EscalarParaHumano(motivo=f"{politicas.max_tentativas} falhas em {estado}"),
                EncerrarConversa(),
            ],
        )

    repeticao = _repetir_pergunta(catalogo, estado, lead, ctx).conteudo
    return Transicao(
        novo_estado=estado,
        ctx_updates={"tentativas": tentativas},
        mensagens=[catalogo.render("nao_entendi", repeticao=repeticao)],
    )


def _avancar_coleta(
    catalogo: CatalogoMensagens, lead: dict, ctx: dict, atualizacoes: dict
) -> Transicao:
    """Vai para o proximo campo que a Marina ainda nao sabe.

    Quando todos ja estao preenchidos — caso de um lead reconhecido — cai direto
    no carregamento de horarios, sem repetir pergunta nenhuma.
    """
    conhecido = {**lead, **atualizacoes}
    for campo, estado, chave in SEQUENCIA_COLETA:
        if conhecido.get(campo) is None:
            return Transicao(
                novo_estado=estado,
                lead_updates=atualizacoes,
                ctx_updates={"tentativas": 0},
                mensagens=[_msg(catalogo, chave, conhecido, ctx)],
            )

    return Transicao(
        novo_estado=Estado.CARREGANDO_HORARIOS,
        lead_updates=atualizacoes,
        ctx_updates={"tentativas": 0},
        efeitos=[GerarSlots()],
    )


def _encerrar_por_opt_out(catalogo: CatalogoMensagens, lead: dict, ctx: dict) -> Transicao:
    """Encerra e suprime o contato — mas so registra se houver o que suprimir.

    Se a pessoa digita SAIR antes de informar o telefone, nao ha contato conhecido:
    a conversa fecha e nenhuma linha vai para opt_outs. Bloquear um desconhecido
    nao significa nada, e enche a lista de supressao de linhas vazias.
    """
    telefone = lead.get("telefone_e164") or ctx.get("telefone")
    email = lead.get("email") or ctx.get("email")

    efeitos: list[Efeito] = []
    if telefone or email:
        efeitos.append(RegistrarOptOut(motivo="pedido do lead"))
    efeitos.append(EncerrarConversa())

    return Transicao(
        novo_estado=Estado.OPT_OUT,
        mensagens=[_msg(catalogo, "opt_out", lead, ctx)],
        efeitos=efeitos,
    )


# --- estados internos -------------------------------------------------------


def _resolver_identidade(catalogo: CatalogoMensagens, lead: dict, ctx: dict) -> Transicao:
    reconhecimento = ctx.get("reconhecimento") or {}
    conhecido = reconhecimento.get("lead_conhecido")
    politicas = _politicas(ctx)

    if reconhecimento.get("opt_out_ativo"):
        return Transicao(
            novo_estado=Estado.AGUARDANDO_RETOMADA_OPT_OUT,
            ctx_updates={"tentativas": 0},
            mensagens=[_msg(catalogo, "retomada_opt_out", lead, ctx)],
        )

    if conhecido:
        ultima = reconhecimento.get("ultima_conversa_em")
        ultimo_estado = reconhecimento.get("ultimo_estado")
        agora = ctx.get("agora")
        retomavel = (
            ultima is not None
            and agora is not None
            and ultimo_estado is not None
            and Estado(ultimo_estado) not in ESTADOS_INTERNOS | ESTADOS_TERMINAIS
            and (agora - ultima).total_seconds() / 3600 < politicas.janela_retomada_horas
        )
        if retomavel:
            return Transicao(
                novo_estado=Estado.AGUARDANDO_RETOMADA_CONVERSA,
                ctx_updates={"tentativas": 0},
                mensagens=[_msg(catalogo, "retomada_conversa", conhecido, ctx)],
            )

        # Passou da janela: conversa nova, mas sem reperguntar o que ja se sabe.
        avanco = _avancar_coleta(catalogo, conhecido, ctx, {})
        return Transicao(
            novo_estado=avanco.novo_estado,
            lead_updates=avanco.lead_updates,
            ctx_updates=avanco.ctx_updates,
            mensagens=[
                _msg(catalogo, "retorno_reconhecido", conhecido, ctx),
                *avanco.mensagens,
            ],
            efeitos=avanco.efeitos,
        )

    return _avancar_coleta(catalogo, lead, ctx, {})


def _carregar_horarios(catalogo: CatalogoMensagens, lead: dict, ctx: dict) -> Transicao:
    slots = ctx.get("slots_ofertados") or []
    if not slots:
        return Transicao(
            novo_estado=Estado.ESCALADO_HUMANO,
            mensagens=[_msg(catalogo, "sem_horarios", lead, ctx)],
            efeitos=[
                EscalarParaHumano(motivo="sem horarios no horizonte"),
                EncerrarConversa(),
            ],
        )
    return Transicao(
        novo_estado=Estado.AGUARDANDO_ESCOLHA_HORARIO,
        ctx_updates={"tentativas": 0},
        mensagens=[_msg(catalogo, "oferta_horarios", lead, ctx)],
    )


def _reservando(catalogo: CatalogoMensagens, lead: dict, ctx: dict) -> Transicao:
    reserva = ctx.get("reserva") or {}
    if reserva.get("ok"):
        return Transicao(
            novo_estado=Estado.AGENDADO,
            mensagens=[_msg(catalogo, "agendado", lead, ctx)],
            efeitos=[EncerrarConversa()],
        )

    # Alguem pegou o horario entre a oferta e a confirmacao. Recalcula e reoferta.
    return Transicao(
        novo_estado=Estado.CARREGANDO_HORARIOS,
        ctx_updates={"slot_escolhido": None, "tentativas": 0},
        mensagens=[_msg(catalogo, "slot_perdido", lead, ctx)],
        efeitos=[GerarSlots()],
    )


# --- entrada principal ------------------------------------------------------


def handle(
    estado: Estado,
    lead: dict,
    ctx: dict,
    entrada: InboundMessage,
    catalogo: CatalogoMensagens | None = None,
) -> Transicao:
    """Decide o que acontece. Nao executa nada."""
    catalogo = catalogo or _catalogo_padrao()
    texto = entrada.texto

    # 0. Estado interno: o engine acabou de preencher o ctx, a entrada e vazia.
    if estado in ESTADOS_INTERNOS:
        if estado is Estado.RESOLVENDO_IDENTIDADE:
            return _resolver_identidade(catalogo, lead, ctx)
        if estado is Estado.CARREGANDO_HORARIOS:
            return _carregar_horarios(catalogo, lead, ctx)
        return _reservando(catalogo, lead, ctx)

    # 1. Anexo pula os guardrails de texto — eles so existem para texto.
    if entrada.anexo is None:
        # 2. Comandos globais, validos em qualquer estado.
        comando = comando_global(texto)
        if comando is ComandoGlobal.SAIR:
            return _encerrar_por_opt_out(catalogo, lead, ctx)
        if comando is ComandoGlobal.HUMANO:
            return Transicao(
                novo_estado=Estado.ESCALADO_HUMANO,
                mensagens=[_msg(catalogo, "escalonamento", lead, ctx)],
                efeitos=[
                    EscalarParaHumano(motivo="pedido do lead"),
                    EncerrarConversa(),
                ],
            )
        if comando is ComandoGlobal.VOLTAR:
            anterior = ctx.get("estado_anterior")
            destino = Estado(anterior) if anterior else estado
            return Transicao(
                novo_estado=destino,
                ctx_updates={"tentativas": 0},
                mensagens=[_repetir_pergunta(catalogo, destino, lead, ctx)],
            )

        # 3. Escopo proibido: desvia sem responder e devolve o lead ao fluxo.
        if fora_de_escopo(texto):
            return Transicao(
                novo_estado=estado,
                mensagens=[
                    _msg(catalogo, "fora_de_escopo", lead, ctx),
                    _repetir_pergunta(catalogo, estado, lead, ctx),
                ],
            )

    # 4. Parser do estado.
    return _por_estado(estado, catalogo, lead, ctx, entrada)


def _por_estado(
    estado: Estado,
    catalogo: CatalogoMensagens,
    lead: dict,
    ctx: dict,
    entrada: InboundMessage,
) -> Transicao:
    texto = entrada.texto or ""

    if estado is Estado.INICIO:
        return Transicao(
            novo_estado=Estado.AGUARDANDO_NOME,
            ctx_updates={"consentimento_em": ctx.get("agora"), "tentativas": 0},
            mensagens=[
                _msg(catalogo, "boas_vindas", lead, ctx),
                _msg(catalogo, "pergunta_nome", lead, ctx),
            ],
        )

    if estado is Estado.AGUARDANDO_NOME:
        nome = parse_nome(texto)
        if nome is None:
            return _falha_de_parsing(catalogo, estado, lead, ctx)
        contexto = {**ctx, "nome": nome}
        return Transicao(
            novo_estado=Estado.AGUARDANDO_TELEFONE,
            ctx_updates={"nome": nome, "tentativas": 0},
            mensagens=[_msg(catalogo, "pergunta_telefone", lead, contexto)],
        )

    if estado is Estado.AGUARDANDO_TELEFONE:
        telefone = parse_telefone(texto)
        if telefone is None:
            politicas = _politicas(ctx)
            tentativas = int(ctx.get("tentativas", 0)) + 1
            if tentativas >= politicas.max_tentativas:
                return _falha_de_parsing(catalogo, estado, lead, ctx)
            return Transicao(
                novo_estado=estado,
                ctx_updates={"tentativas": tentativas},
                mensagens=[_msg(catalogo, "erro_telefone", lead, ctx)],
            )
        return Transicao(
            novo_estado=Estado.RESOLVENDO_IDENTIDADE,
            ctx_updates={"telefone": telefone, "tentativas": 0},
            efeitos=[
                ResolverIdentidade(
                    telefone=telefone,
                    nome=ctx.get("nome"),
                    email=ctx.get("email"),
                    consentimento_em=ctx.get("consentimento_em"),
                    canal=ctx.get("canal", "terminal"),
                )
            ],
        )

    if estado is Estado.AGUARDANDO_RETOMADA_OPT_OUT:
        resposta = parse_sim_nao(texto)
        if resposta is None:
            return _falha_de_parsing(catalogo, estado, lead, ctx)
        if not resposta:
            return Transicao(
                novo_estado=Estado.OPT_OUT,
                mensagens=[_msg(catalogo, "opt_out_mantido", lead, ctx)],
                efeitos=[EncerrarConversa()],
            )
        reconhecido = (ctx.get("reconhecimento") or {}).get("lead_conhecido") or lead
        avanco = _avancar_coleta(catalogo, reconhecido, ctx, {})
        return Transicao(
            novo_estado=avanco.novo_estado,
            lead_updates=avanco.lead_updates,
            ctx_updates=avanco.ctx_updates,
            mensagens=avanco.mensagens,
            efeitos=[RevogarOptOut(), *avanco.efeitos],
        )

    if estado is Estado.AGUARDANDO_RETOMADA_CONVERSA:
        resposta = parse_sim_nao(texto)
        if resposta is None:
            return _falha_de_parsing(catalogo, estado, lead, ctx)
        reconhecido = (ctx.get("reconhecimento") or {}).get("lead_conhecido") or lead
        if resposta:
            anterior = (ctx.get("reconhecimento") or {}).get("ultimo_estado")
            destino = Estado(anterior) if anterior else Estado.AGUARDANDO_EMAIL
            return Transicao(
                novo_estado=destino,
                ctx_updates={"tentativas": 0},
                mensagens=[_repetir_pergunta(catalogo, destino, reconhecido, ctx)],
            )
        # "Comecar do zero" nao apaga o cadastro: refaz a coleta do inicio.
        return Transicao(
            novo_estado=Estado.AGUARDANDO_EMAIL,
            ctx_updates={"tentativas": 0},
            mensagens=[_msg(catalogo, "pergunta_email", reconhecido, ctx)],
        )

    if estado is Estado.AGUARDANDO_EMAIL:
        email = parse_email(texto)
        if email is None:
            return _falha_de_parsing(catalogo, estado, lead, ctx)
        return _avancar_coleta(catalogo, lead, {**ctx, "email": email}, {"email": email})

    if estado is Estado.AGUARDANDO_FONTE:
        resultado = parse_fonte(texto)
        if resultado is None:
            return _falha_de_parsing(catalogo, estado, lead, ctx)
        fonte, original = resultado
        return _avancar_coleta(
            catalogo, lead, ctx, {"fonte": fonte.value, "fonte_raw": original}
        )

    if estado is Estado.AGUARDANDO_TIPO_CONTRATO:
        resultado = parse_tipo_contrato(texto)
        if resultado is None:
            politicas = _politicas(ctx)
            tentativas = int(ctx.get("tentativas", 0)) + 1
            if tentativas >= politicas.max_tentativas:
                return _falha_de_parsing(catalogo, estado, lead, ctx)
            return Transicao(
                novo_estado=estado,
                ctx_updates={"tentativas": tentativas},
                mensagens=[_msg(catalogo, "erro_tipo_contrato", lead, ctx)],
            )
        tipo, original = resultado
        return _avancar_coleta(
            catalogo,
            lead,
            ctx,
            {"tipo_contrato": tipo.value, "tipo_contrato_raw": original},
        )

    if estado is Estado.AGUARDANDO_TEM_CONTRATO:
        resposta = parse_sim_nao(texto)
        if resposta is None:
            return _falha_de_parsing(catalogo, estado, lead, ctx)
        if resposta:
            return Transicao(
                novo_estado=Estado.AGUARDANDO_ENVIO_CONTRATO,
                lead_updates={"possui_contrato": True},
                ctx_updates={"tentativas": 0},
                mensagens=[_msg(catalogo, "pedido_contrato", lead, ctx)],
            )
        return Transicao(
            novo_estado=Estado.CARREGANDO_HORARIOS,
            lead_updates={"possui_contrato": False},
            ctx_updates={"tentativas": 0},
            mensagens=[_msg(catalogo, "sem_contrato", lead, ctx)],
            efeitos=[GerarSlots()],
        )

    if estado is Estado.AGUARDANDO_ENVIO_CONTRATO:
        if entrada.anexo is not None:
            return Transicao(
                novo_estado=Estado.CARREGANDO_HORARIOS,
                ctx_updates={"tentativas": 0},
                mensagens=[_msg(catalogo, "contrato_recebido", lead, ctx)],
                efeitos=[SalvarDocumento(anexo=entrada.anexo), GerarSlots()],
            )
        if quer_pular(texto):
            return Transicao(
                novo_estado=Estado.CARREGANDO_HORARIOS,
                ctx_updates={"tentativas": 0},
                mensagens=[_msg(catalogo, "contrato_pulado", lead, ctx)],
                efeitos=[GerarSlots()],
            )
        return _falha_de_parsing(catalogo, estado, lead, ctx)

    if estado is Estado.AGUARDANDO_ESCOLHA_HORARIO:
        slots = ctx.get("slots_ofertados") or []
        escolha = parse_opcao_menu(texto, len(slots))
        if escolha is None:
            return _falha_de_parsing(catalogo, estado, lead, ctx)
        slot = slots[escolha - 1]
        contexto = {**ctx, "slot_escolhido": slot}
        return Transicao(
            novo_estado=Estado.AGUARDANDO_CONFIRMACAO,
            ctx_updates={"slot_escolhido": slot, "tentativas": 0},
            mensagens=[_msg(catalogo, "confirmacao", lead, contexto)],
        )

    if estado is Estado.AGUARDANDO_CONFIRMACAO:
        resposta = parse_sim_nao(texto)
        if resposta is None:
            return _falha_de_parsing(catalogo, estado, lead, ctx)
        if resposta:
            slot = ctx.get("slot_escolhido") or {}
            return Transicao(
                novo_estado=Estado.RESERVANDO,
                ctx_updates={"tentativas": 0},
                efeitos=[ReservarSlot(indice=int(slot.get("indice", 1)))],
            )
        return Transicao(
            novo_estado=Estado.CARREGANDO_HORARIOS,
            ctx_updates={"slot_escolhido": None, "tentativas": 0},
            efeitos=[GerarSlots()],
        )

    # Terminais nao transicionam.
    return Transicao(novo_estado=estado)


_CATALOGO: CatalogoMensagens | None = None


def _catalogo_padrao() -> CatalogoMensagens:
    global _CATALOGO
    if _CATALOGO is None:
        _CATALOGO = CatalogoMensagens()
    return _CATALOGO
