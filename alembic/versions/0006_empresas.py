"""A Marina passa a saber o que e uma empresa.

Cada instalacao do juridico representa UMA empresa e seus funcionarios, e a Marina
conversa com varias. Esta migration cria o cadastro dessas empresas e prepara as
tabelas para dizer a qual delas cada linha pertence.

Aditiva de proposito: a coluna nasce NULA em todo lugar, e nada no comportamento
muda. Quem passa a preencher e a fiacao, na etapa seguinte; a obrigatoriedade e a
troca das travas de unicidade vem depois disso, na `0007`. Dividir assim e o que
permite reverter cada etapa sozinha.

**O token nao entra no banco.** `juridico_token_env` guarda o NOME da variavel de
ambiente que o carrega. Uma coluna com o segredo apareceria em `pg_dump`, em replica
e em qualquer `SELECT *` — e dumps de desenvolvimento circulam entre maquinas.

O backfill le `os.environ` diretamente, e NUNCA `app.config`: migration e registro
historico e precisa continuar rodando daqui a um ano, depois que as chaves de
configuracao que ela consultaria ja tiverem mudado de forma.

Revision ID: 0006_empresas
Revises: 0005_conversa_whatsapp
Create Date: 2026-09-09
"""

import os
import pathlib
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_empresas"
down_revision: str | None = "0005_conversa_whatsapp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: As tabelas consultadas sem um pai ja escopado no WHERE — sao essas que precisam
#: dizer de quem sao. `agendamentos`, `mensagens` e `documentos` ficam de fora
#: porque sempre chegam por `consultor_id`, `conversa_id` ou `lead_id`.
TABELAS: tuple[str, ...] = (
    "leads",
    "conversas",
    "consultores",
    "regras_disponibilidade",
    "opt_outs",
    "crm_outbox",
)


def _slug_para_env(slug: str) -> str:
    return "JURIDICO_TOKEN_" + slug.upper().replace("-", "_")


def _ambiente() -> dict[str, str]:
    """Variaveis de ambiente, com o `.env` do diretorio atual como fallback.

    Le o arquivo cru de proposito, em vez de `app.config`: o formato do `.env` e
    uma convencao estavel, enquanto as chaves do `Configuracao` vao mudar de forma
    nas etapas seguintes — e uma migration precisa continuar rodando depois disso.

    Sem o fallback, `alembic upgrade head` em desenvolvimento acharia tudo vazio
    (o pydantic le o `.env` mas nao exporta nada para o processo) e o backfill
    sairia pela metade, sem ninguem entender por que.
    """
    valores: dict[str, str] = {}
    arquivo = pathlib.Path(".env")
    if arquivo.is_file():
        for linha in arquivo.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            valores[chave.strip()] = valor.strip().strip("\"'")
    valores.update({c: v for c, v in os.environ.items() if v})
    return valores


def upgrade() -> None:
    op.create_table(
        "empresas",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=40), nullable=False),
        sa.Column("nome", sa.String(length=160), nullable=False),
        sa.Column("nome_curto", sa.String(length=60), nullable=True),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("whatsapp_numero", sa.String(length=20), nullable=True),
        sa.Column(
            "juridico_ativo", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "juridico_base_url", sa.String(length=255), nullable=False, server_default=""
        ),
        sa.Column(
            "juridico_token_env", sa.String(length=80), nullable=False, server_default=""
        ),
        sa.Column("crm_empresa_id", sa.String(length=64), nullable=True),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_empresas_slug"),
    )
    # Unico e nullable: no Postgres cada NULL e distinto, entao varias empresas
    # convivem sem numero e o roteamento nunca casa com nulo.
    op.create_index(
        "uq_empresas_whatsapp_numero", "empresas", ["whatsapp_numero"], unique=True
    )

    ambiente = _ambiente()
    conexao = op.get_bind()
    # Instalacao nova nao ganha uma empresa fantasma: so ha o que atribuir se ja
    # existir dado.
    tem_dados = any(
        conexao.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {tabela})"))  # noqa: S608
        for tabela in TABELAS
    )

    empresa_id: uuid.UUID | None = None
    slug = ambiente.get("EMPRESA_PADRAO_SLUG", "leve").strip() or "leve"
    if tem_dados:
        empresa_id = uuid.uuid4()
        numero = (ambiente.get("TWILIO_WHATSAPP_FROM") or "").strip() or None
        conexao.execute(
            sa.text(
                "INSERT INTO empresas (id, slug, nome, nome_curto, ativo,"
                " whatsapp_numero, juridico_ativo, juridico_base_url, juridico_token_env)"
                " VALUES (:id, :slug, :nome, :curto, true, :numero, :jur, :url, :env)"
            ),
            {
                "id": empresa_id,
                "slug": slug,
                "nome": ambiente.get(
                    "EMPRESA_PADRAO_NOME", "Leve Soluções Financeiras e Empresariais"
                ),
                "curto": ambiente.get("EMPRESA_PADRAO_NOME_CURTO", "Leve"),
                "numero": numero,
                "jur": ambiente.get("JURIDICO_ATIVO", "false").strip().lower() == "true",
                "url": ambiente.get("JURIDICO_BASE_URL", ""),
                "env": _slug_para_env(slug),
            },
        )

    for tabela in TABELAS:
        op.add_column(
            tabela,
            sa.Column("empresa_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        # RESTRICT, nao CASCADE: apagar uma empresa que ainda tem lead precisa
        # falhar. CASCADE aqui apagaria uma base inteira por um DELETE errado.
        op.create_foreign_key(
            f"fk_{tabela}_empresa", tabela, "empresas", ["empresa_id"], ["id"],
            ondelete="RESTRICT",
        )
        if empresa_id is not None:
            conexao.execute(
                sa.text(f"UPDATE {tabela} SET empresa_id = :e"),  # noqa: S608
                {"e": empresa_id},
            )
        op.create_index(f"ix_{tabela}_empresa_id", tabela, ["empresa_id"])

    if tem_dados:
        print(  # noqa: T201 - e a unica forma de o operador ver isto
            "\n"
            + "=" * 72
            + f"\nOs dados existentes foram atribuidos a empresa '{slug}'."
            "\nComplete o cadastro e defina o token antes de ligar a integracao:"
            f"\n  {_slug_para_env(slug)}=<token do juridico>   no .env"
            f"\n  python scripts/cadastrar_empresa.py --slug {slug} --listar"
            "\n" + "=" * 72 + "\n"
        )


def downgrade() -> None:
    for tabela in reversed(TABELAS):
        op.drop_index(f"ix_{tabela}_empresa_id", table_name=tabela)
        op.drop_constraint(f"fk_{tabela}_empresa", tabela, type_="foreignkey")
        op.drop_column(tabela, "empresa_id")
    op.drop_index("uq_empresas_whatsapp_numero", table_name="empresas")
    op.drop_table("empresas")
