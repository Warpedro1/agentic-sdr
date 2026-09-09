"""A Marina atende várias empresas, e precisa saber de quem é cada mensagem.

Cada instalação do juridico é uma empresa. Estes testes cobrem as três perguntas
que a leitura de empresa responde — de quem é este número, quem é este slug, e
qual empresa uma ferramenta deve usar — mais o sigilo da credencial.
"""

from __future__ import annotations

import uuid

import pytest

from app.empresas import (
    EmpresaCtx,
    EmpresaDesconhecida,
    listar_ativas,
    por_numero,
    por_slug,
    resolver_argumento,
    slug_para_env,
)
from app.models import Empresa


def _linha(**campos) -> Empresa:
    padrao = {
        "slug": "acme",
        "nome": "Acme Assessoria Juridica",
        "nome_curto": "Acme",
        "ativo": True,
        "juridico_ativo": False,
        "juridico_base_url": "",
        "juridico_token_env": "",
    }
    return Empresa(**{**padrao, **campos})


# --- a credencial nao pode vazar ------------------------------------------


def test_o_token_nao_aparece_na_representacao_da_empresa():
    """O vazamento mais provável é alguém escrever `logger.info(empresa=empresa)`."""
    empresa = EmpresaCtx(
        id=uuid.uuid4(),
        slug="acme",
        nome="Acme",
        nome_curto="Acme",
        whatsapp_numero="+5511999990000",
        juridico_ativo=True,
        juridico_base_url="https://acme.test",
        juridico_token="1|SEGREDO-QUE-NAO-PODE-VAZAR",
    )
    assert "SEGREDO" not in repr(empresa)


def test_o_nome_da_variavel_de_ambiente_sai_do_slug():
    assert slug_para_env("acme") == "JURIDICO_TOKEN_ACME"
    # Hífen não é válido em nome de variável de ambiente.
    assert slug_para_env("acme-sp") == "JURIDICO_TOKEN_ACME_SP"


def test_citar_a_empresa_cai_para_o_nome_completo_sem_nome_curto():
    empresa = EmpresaCtx(
        id=uuid.uuid4(),
        slug="acme",
        nome="Acme Assessoria Juridica",
        nome_curto="",
        whatsapp_numero=None,
        juridico_ativo=False,
        juridico_base_url="",
    )
    assert empresa.curto == "Acme Assessoria Juridica"


# --- de quem e este numero -------------------------------------------------


@pytest.mark.asyncio
async def test_o_numero_que_recebeu_a_mensagem_identifica_a_empresa(sessao_db):
    sessao_db.add(_linha(whatsapp_numero="+5511999990000"))
    await sessao_db.commit()

    achada = await por_numero(sessao_db, "+5511999990000")
    assert achada is not None
    assert achada.slug == "acme"


@pytest.mark.asyncio
async def test_o_numero_e_aceito_com_ou_sem_o_prefixo_do_canal(sessao_db):
    """A operadora manda `whatsapp:+55...`; a linha guarda só o E.164."""
    sessao_db.add(_linha(whatsapp_numero="+5511999990000"))
    await sessao_db.commit()

    assert await por_numero(sessao_db, "whatsapp:+5511999990000") is not None


@pytest.mark.asyncio
async def test_numero_desconhecido_nao_identifica_empresa_nenhuma(sessao_db):
    sessao_db.add(_linha(whatsapp_numero="+5511999990000"))
    await sessao_db.commit()

    assert await por_numero(sessao_db, "+5511000000000") is None


@pytest.mark.asyncio
async def test_empresa_desativada_nao_recebe_mensagem(sessao_db):
    """Desativar precisa parar o atendimento sem apagar o histórico."""
    sessao_db.add(_linha(whatsapp_numero="+5511999990000", ativo=False))
    await sessao_db.commit()

    assert await por_numero(sessao_db, "+5511999990000") is None


@pytest.mark.asyncio
async def test_numero_vazio_nao_casa_com_empresa_sem_numero(sessao_db):
    """Empresa sem número existe (a que só roda no terminal) e não pode ser sorteada."""
    sessao_db.add(_linha(whatsapp_numero=None))
    await sessao_db.commit()

    assert await por_numero(sessao_db, "") is None
    assert await por_numero(sessao_db, "whatsapp:") is None


# --- qual empresa a ferramenta deve usar ----------------------------------


@pytest.mark.asyncio
async def test_com_uma_empresa_so_ela_e_assumida(sessao_db):
    """Quem opera uma instalação de uma empresa não paga o custo do multiempresa."""
    sessao_db.add(_linha())
    await sessao_db.commit()

    empresa = await resolver_argumento(sessao_db, None)
    assert empresa.slug == "acme"


@pytest.mark.asyncio
async def test_com_duas_empresas_a_ferramenta_recusa_adivinhar(sessao_db):
    """Escolher a errada aqui escreveria no CRM do cliente errado."""
    sessao_db.add(_linha(slug="acme"))
    sessao_db.add(_linha(slug="beta", nome="Beta"))
    await sessao_db.commit()

    with pytest.raises(EmpresaDesconhecida) as erro:
        await resolver_argumento(sessao_db, None)
    # A mensagem tem que dizer quais são, senão o operador fica adivinhando.
    assert "acme" in str(erro.value)
    assert "beta" in str(erro.value)


@pytest.mark.asyncio
async def test_slug_inexistente_lista_os_que_existem(sessao_db):
    sessao_db.add(_linha(slug="acme"))
    await sessao_db.commit()

    with pytest.raises(EmpresaDesconhecida) as erro:
        await resolver_argumento(sessao_db, "fantasma")
    assert "acme" in str(erro.value)


@pytest.mark.asyncio
async def test_sem_empresa_nenhuma_a_ferramenta_aponta_o_cadastro(sessao_db):
    with pytest.raises(EmpresaDesconhecida) as erro:
        await resolver_argumento(sessao_db, None)
    assert "cadastrar_empresa" in str(erro.value)


@pytest.mark.asyncio
async def test_listar_ativas_ignora_as_desativadas(sessao_db):
    sessao_db.add(_linha(slug="acme"))
    sessao_db.add(_linha(slug="beta", nome="Beta", ativo=False))
    await sessao_db.commit()

    assert [e.slug for e in await listar_ativas(sessao_db)] == ["acme"]


@pytest.mark.asyncio
async def test_a_credencial_e_lida_do_ambiente_pelo_nome_guardado(
    sessao_db, monkeypatch
):
    """O banco guarda o nome da variável; o valor nunca encosta no Postgres."""
    monkeypatch.setenv("JURIDICO_TOKEN_ACME", "1|token-de-teste")
    sessao_db.add(_linha(juridico_token_env="JURIDICO_TOKEN_ACME"))
    await sessao_db.commit()

    empresa = await por_slug(sessao_db, "acme")
    assert empresa is not None
    assert empresa.juridico_token == "1|token-de-teste"


@pytest.mark.asyncio
async def test_credencial_ausente_nao_quebra_a_leitura(sessao_db):
    """Empresa mal configurada é problema de operação, não exceção em runtime."""
    sessao_db.add(_linha(juridico_token_env="JURIDICO_TOKEN_QUE_NAO_EXISTE"))
    await sessao_db.commit()

    empresa = await por_slug(sessao_db, "acme")
    assert empresa is not None
    assert empresa.juridico_token == ""
