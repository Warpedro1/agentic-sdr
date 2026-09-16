"""Leitura das empresas atendidas pela Marina.

Cada instalacao do juridico e uma empresa. Este modulo responde tres perguntas, e
so essas: de quem e este numero de WhatsApp, quem e a empresa deste slug, e quais
empresas estao ativas.

Devolve um `EmpresaCtx` congelado, e nao a linha do ORM, por dois motivos concretos:
a linha morre junto com a sessao que a leu — e o Engine sobrevive a ela —, e o token
precisa ser resolvido do ambiente uma vez, na leitura, em vez de a cada uso.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Empresa
from app.segredos import ler as ler_segredo

#: Prefixo da variavel de ambiente com o token da instancia da Z-API.
PREFIXO_TOKEN_ZAPI = "ZAPI_TOKEN_"


class EmpresaDesconhecida(Exception):
    """Pediram uma empresa que nao existe, ou nao disseram qual entre varias."""


@dataclass(frozen=True)
class EmpresaCtx:
    """Fotografia imutavel de uma empresa, desamarrada da sessao que a leu."""

    id: uuid.UUID
    slug: str
    nome: str
    nome_curto: str
    whatsapp_numero: str | None
    juridico_ativo: bool
    juridico_base_url: str
    #: `repr=False` nao e cosmetico: e o que impede o token de vazar quando alguem
    #: escrever `logger.info("...", empresa=empresa)`.
    juridico_token: str = field(default="", repr=False)
    crm_empresa_id: str | None = None
    #: Identificador do numero na Meta. E por ele que o envio pela Cloud API e
    #: enderecado, e e por ele que a mensagem recebida encontra sua empresa. Fica
    #: no fim, com padrao, para quem ja constroi uma empresa sem ele continuar
    #: funcionando — a Twilio e o terminal nao precisam deste campo.
    whatsapp_numero_id: str | None = None
    #: Identificador da instancia na Z-API: a chave de roteamento daquele caminho.
    zapi_instancia_id: str | None = None
    #: Token da instancia, lido de `ZAPI_TOKEN_<SLUG>`. Vai na URL de envio da Z-API,
    #: e por isso tem o mesmo cuidado do token do juridico.
    zapi_token: str = field(default="", repr=False)

    @property
    def curto(self) -> str:
        """Como citar a empresa no meio de uma frase."""
        return self.nome_curto or self.nome


def slug_para_env(slug: str, prefixo: str = "JURIDICO_TOKEN_") -> str:
    """O nome da variavel de ambiente que carrega um token desta empresa.

    O prefixo diz QUAL token: `JURIDICO_TOKEN_` para o CRM, `ZAPI_TOKEN_` para a
    instancia da Z-API.
    """
    return prefixo + slug.upper().replace("-", "_")


def _congelar(linha: Empresa) -> EmpresaCtx:
    return EmpresaCtx(
        id=linha.id,
        slug=linha.slug,
        nome=linha.nome,
        nome_curto=linha.nome_curto or "",
        whatsapp_numero=linha.whatsapp_numero,
        whatsapp_numero_id=linha.whatsapp_numero_id,
        juridico_ativo=linha.juridico_ativo,
        juridico_base_url=linha.juridico_base_url,
        juridico_token=ler_segredo(linha.juridico_token_env),
        crm_empresa_id=linha.crm_empresa_id,
        zapi_instancia_id=linha.zapi_instancia_id,
        # So quem tem instancia carrega o token: empresa sem Z-API nao guarda na
        # memoria um segredo que nunca vai usar.
        zapi_token=(
            ler_segredo(slug_para_env(linha.slug, PREFIXO_TOKEN_ZAPI))
            if linha.zapi_instancia_id
            else ""
        ),
    )


def _so_numero(valor: str) -> str:
    """Aceita tanto `whatsapp:+55...` quanto `+55...`."""
    return valor.strip().removeprefix("whatsapp:").strip()


async def por_numero(sessao: AsyncSession, numero: str) -> EmpresaCtx | None:
    """A empresa dona do numero que recebeu a mensagem.

    Numero vazio devolve None sem consultar: no Postgres a comparacao com NULL nunca
    casa, mas gastar a consulta para descobrir isso e desnecessario.
    """
    limpo = _so_numero(numero or "")
    if not limpo:
        return None
    linha = await sessao.scalar(
        select(Empresa).where(
            Empresa.whatsapp_numero == limpo, Empresa.ativo.is_(True)
        )
    )
    return _congelar(linha) if linha else None


async def por_numero_id(sessao: AsyncSession, numero_id: str) -> EmpresaCtx | None:
    """A empresa dona do numero que recebeu, quando quem entrega e a Meta.

    O papel e o mesmo de `por_numero`, e o criterio de desvio tambem: nao achou,
    ninguem e atendido e nada e gravado. Muda so a chave — a Cloud API identifica o
    numero por um id proprio, que e o que vem em `metadata.phone_number_id` e o que
    a URL de envio exige. Casar pelo numero exibido seria pior: ele chega sem o `+`
    e sem garantia de formato.
    """
    limpo = (numero_id or "").strip()
    if not limpo:
        return None
    linha = await sessao.scalar(
        select(Empresa).where(
            Empresa.whatsapp_numero_id == limpo, Empresa.ativo.is_(True)
        )
    )
    return _congelar(linha) if linha else None


async def por_zapi_instancia(
    sessao: AsyncSession, instancia_id: str
) -> EmpresaCtx | None:
    """A empresa dona da instancia que recebeu, quando quem entrega e a Z-API.

    Gemeo de `por_numero_id`, com a chave daquele provedor: o `instanceId` que vem
    em toda chamada. Casar pelo `connectedPhone` seria pior pelo mesmo motivo de la —
    formato sem garantia —, e ainda confundiria com o numero de outro caminho.
    """
    limpo = (instancia_id or "").strip()
    if not limpo:
        return None
    linha = await sessao.scalar(
        select(Empresa).where(
            Empresa.zapi_instancia_id == limpo, Empresa.ativo.is_(True)
        )
    )
    return _congelar(linha) if linha else None


async def por_slug(sessao: AsyncSession, slug: str) -> EmpresaCtx | None:
    linha = await sessao.scalar(select(Empresa).where(Empresa.slug == (slug or "").strip()))
    return _congelar(linha) if linha else None


async def por_id(sessao: AsyncSession, empresa_id: uuid.UUID) -> EmpresaCtx | None:
    linha = await sessao.get(Empresa, empresa_id)
    return _congelar(linha) if linha else None


async def listar_ativas(sessao: AsyncSession) -> list[EmpresaCtx]:
    linhas = await sessao.scalars(
        select(Empresa).where(Empresa.ativo.is_(True)).order_by(Empresa.slug)
    )
    return [_congelar(linha) for linha in linhas]


async def resolver_argumento(sessao: AsyncSession, slug: str | None) -> EmpresaCtx:
    """A empresa que uma ferramenta de linha de comando deve usar.

    Com uma empresa cadastrada, ela e assumida — quem opera uma instalacao de uma
    empresa so nao paga o custo do multiempresa. Com duas ou mais e sem `--empresa`,
    **recusa adivinhar**: escolher a errada aqui escreve no CRM do cliente errado.
    """
    if slug:
        empresa = await por_slug(sessao, slug)
        if empresa is None:
            conhecidas = ", ".join(e.slug for e in await listar_ativas(sessao)) or "nenhuma"
            raise EmpresaDesconhecida(
                f"empresa '{slug}' nao encontrada. Cadastradas: {conhecidas}. "
                "Cadastre com `python scripts/cadastrar_empresa.py`."
            )
        return empresa

    ativas = await listar_ativas(sessao)
    if not ativas:
        raise EmpresaDesconhecida(
            "nenhuma empresa cadastrada. Rode `python scripts/cadastrar_empresa.py`."
        )
    if len(ativas) > 1:
        raise EmpresaDesconhecida(
            "ha mais de uma empresa cadastrada: "
            + ", ".join(e.slug for e in ativas)
            + ". Diga qual com --empresa <slug>."
        )
    return ativas[0]
