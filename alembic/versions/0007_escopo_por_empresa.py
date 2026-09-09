"""O isolamento entre empresas passa a ser garantido pelo banco.

Ate aqui o dado NASCIA rotulado, mas nada impedia uma consulta alcancar o de outra
empresa. Esta migration troca as travas para que o vazamento seja impossivel, e nao
apenas evitado pelo codigo.

Tres coisas acontecem, nesta ordem:

1. **Os registros sem dono ganham uma empresa.** Toda linha criada entre a
   implantacao do cadastro e a da fiacao nasceu sem empresa — isso acontece em
   qualquer implantacao real, nao so em desenvolvimento. Atribui quando e
   INEQUIVOCO (uma empresa cadastrada, ou a apontada por `EMPRESA_PADRAO_SLUG`) e
   RECUSA quando ha ambiguidade: atribuir ao escritorio errado e pior do que parar.

2. **A empresa vira obrigatoria** nas seis tabelas alcancaveis sem um pai ja
   escopado. `agendamentos`, `mensagens` e `documentos` ficam de fora porque sempre
   chegam por `consultor_id`, `conversa_id` ou `lead_id`.

3. **As travas de unicidade passam a ser por empresa.** Todas ficam mais FROUXAS,
   nunca mais apertadas — nenhum dado existente pode viola-las. O unico aperto e o
   NOT NULL do item 2.

A chave estrangeira composta de `regras_disponibilidade` merece nota: `empresa_id`
la e desnormalizacao (a empresa e derivavel do consultor), e sem a chave composta
seria um segundo lugar onde a verdade pode divergir. Divergencia em agenda vira
horario oferecido pela empresa errada.

**O downgrade pode falhar de proposito**: se duas empresas ja atenderem a mesma
pessoa, recriar a unicidade global de telefone e impossivel sem escolher qual
cadastro apagar. Falhar e melhor do que escolher.

Revision ID: 0007_escopo_por_empresa
Revises: 0006_empresas
Create Date: 2026-09-09
"""

import os
import pathlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_escopo_por_empresa"
down_revision: str | None = "0006_empresas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABELAS: tuple[str, ...] = (
    "leads",
    "conversas",
    "consultores",
    "regras_disponibilidade",
    "opt_outs",
    "crm_outbox",
)

_ASPAS = "\"'"


def _ambiente() -> dict[str, str]:
    """Ambiente com o `.env` do diretorio atual como fallback. Ver `0006`."""
    valores: dict[str, str] = {}
    arquivo = pathlib.Path(".env")
    if arquivo.is_file():
        for linha in arquivo.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            valores[chave.strip()] = valor.strip().strip(_ASPAS)
    valores.update({c: v for c, v in os.environ.items() if v})
    return valores


def _empresa_para_orfaos(conexao) -> str | None:
    """A empresa que recebe as linhas sem dono, ou None quando nao ha orfao.

    Levanta quando ha orfao e a escolha e ambigua.
    """
    orfaos = {
        tabela: conexao.scalar(
            sa.text(f"SELECT count(*) FROM {tabela} WHERE empresa_id IS NULL")  # noqa: S608
        )
        for tabela in TABELAS
    }
    total = sum(orfaos.values())
    if not total:
        return None

    slug = (_ambiente().get("EMPRESA_PADRAO_SLUG") or "").strip()
    if slug:
        alvo = conexao.scalar(
            sa.text("SELECT id::text FROM empresas WHERE slug = :slug"), {"slug": slug}
        )
        if alvo is None:
            raise RuntimeError(
                f"EMPRESA_PADRAO_SLUG aponta para '{slug}', que nao esta cadastrada."
            )
        return alvo

    candidatas = conexao.execute(
        sa.text("SELECT id::text, slug FROM empresas ORDER BY slug")
    ).all()
    if len(candidatas) == 1:
        return candidatas[0][0]

    detalhe = ", ".join(f"{t}={n}" for t, n in orfaos.items() if n)
    conhecidas = ", ".join(s for _, s in candidatas) or "nenhuma"
    raise RuntimeError(
        f"Ha {total} registro(s) sem empresa ({detalhe}) e "
        f"{len(candidatas)} empresa(s) cadastrada(s) ({conhecidas}). "
        "Nao da para adivinhar de quem sao: atribuir ao escritorio errado e pior "
        "do que parar. Defina EMPRESA_PADRAO_SLUG=<slug> e rode de novo, ou "
        "atribua manualmente antes."
    )


def upgrade() -> None:
    conexao = op.get_bind()

    # 1. Os sem dono.
    alvo = _empresa_para_orfaos(conexao)
    if alvo is not None:
        for tabela in TABELAS:
            conexao.execute(
                sa.text(  # noqa: S608
                    f"UPDATE {tabela} SET empresa_id = :e WHERE empresa_id IS NULL"
                ),
                {"e": alvo},
            )

    # 2. A empresa passa a ser obrigatoria.
    for tabela in TABELAS:
        op.alter_column(
            tabela,
            "empresa_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=False,
        )

    # 3a. Leads: o mesmo telefone em duas empresas sao dois cadastros.
    op.drop_constraint("uq_leads_telefone_e164", "leads", type_="unique")
    op.create_unique_constraint(
        "uq_leads_empresa_telefone", "leads", ["empresa_id", "telefone_e164"]
    )
    op.drop_index("uq_leads_email_sem_telefone", table_name="leads")
    op.create_index(
        "uq_leads_email_sem_telefone",
        "leads",
        ["empresa_id", "email"],
        unique=True,
        postgresql_where=sa.text("telefone_e164 IS NULL"),
    )

    # 3b. Conversas: uma aberta por numero POR EMPRESA.
    op.drop_index("uq_conversa_aberta_por_identidade", table_name="conversas")
    op.create_index(
        "uq_conversa_aberta_por_identidade",
        "conversas",
        ["empresa_id", "canal", "identidade_externa"],
        unique=True,
        postgresql_where=sa.text(
            "encerrada_em IS NULL AND identidade_externa IS NOT NULL"
        ),
    )

    # 3c. Consultores: a mesma pessoa pode atender dois escritorios.
    op.drop_constraint("uq_consultores_email", "consultores", type_="unique")
    op.create_unique_constraint(
        "uq_consultores_empresa_email", "consultores", ["empresa_id", "email"]
    )
    # Alvo da chave composta abaixo. Parece redundante com a primaria e nao e.
    op.create_unique_constraint(
        "uq_consultores_id_empresa", "consultores", ["id", "empresa_id"]
    )
    op.drop_constraint(
        "regras_disponibilidade_consultor_id_fkey",
        "regras_disponibilidade",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_regra_consultor_empresa",
        "regras_disponibilidade",
        "consultores",
        ["consultor_id", "empresa_id"],
        ["id", "empresa_id"],
        ondelete="CASCADE",
    )

    # 3d. A empresa vira a PRIMEIRA coluna: e o primeiro predicado de igualdade
    #     tanto da busca por itens pendentes quanto da de descadastro.
    op.drop_index("ix_outbox_pendentes", table_name="crm_outbox")
    op.create_index(
        "ix_outbox_pendentes",
        "crm_outbox",
        ["empresa_id", "entregue_em", "proxima_tentativa_em"],
    )
    op.drop_index("ix_opt_outs_telefone_criado", table_name="opt_outs")
    op.create_index(
        "ix_opt_outs_telefone_criado",
        "opt_outs",
        ["empresa_id", "telefone_e164", "criado_em"],
    )


def downgrade() -> None:
    op.drop_index("ix_opt_outs_telefone_criado", table_name="opt_outs")
    op.create_index(
        "ix_opt_outs_telefone_criado", "opt_outs", ["telefone_e164", "criado_em"]
    )
    op.drop_index("ix_outbox_pendentes", table_name="crm_outbox")
    op.create_index(
        "ix_outbox_pendentes", "crm_outbox", ["entregue_em", "proxima_tentativa_em"]
    )

    op.drop_constraint(
        "fk_regra_consultor_empresa", "regras_disponibilidade", type_="foreignkey"
    )
    op.create_foreign_key(
        "regras_disponibilidade_consultor_id_fkey",
        "regras_disponibilidade",
        "consultores",
        ["consultor_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_consultores_id_empresa", "consultores", type_="unique")
    op.drop_constraint("uq_consultores_empresa_email", "consultores", type_="unique")
    # Falha de proposito se a mesma pessoa ja atender duas empresas.
    op.create_unique_constraint("uq_consultores_email", "consultores", ["email"])

    op.drop_index("uq_conversa_aberta_por_identidade", table_name="conversas")
    op.create_index(
        "uq_conversa_aberta_por_identidade",
        "conversas",
        ["canal", "identidade_externa"],
        unique=True,
        postgresql_where=sa.text(
            "encerrada_em IS NULL AND identidade_externa IS NOT NULL"
        ),
    )

    op.drop_index("uq_leads_email_sem_telefone", table_name="leads")
    op.create_index(
        "uq_leads_email_sem_telefone",
        "leads",
        ["email"],
        unique=True,
        postgresql_where=sa.text("telefone_e164 IS NULL"),
    )
    op.drop_constraint("uq_leads_empresa_telefone", "leads", type_="unique")
    # Idem: falha se duas empresas ja atenderem a mesma pessoa, e falhar e melhor
    # do que escolher qual dos dois cadastros apagar.
    op.create_unique_constraint("uq_leads_telefone_e164", "leads", ["telefone_e164"])

    for tabela in TABELAS:
        op.alter_column(
            tabela,
            "empresa_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )
