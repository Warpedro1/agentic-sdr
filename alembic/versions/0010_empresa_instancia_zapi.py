"""Instancia da Z-API, por empresa.

Terceiro caminho de WhatsApp. A Z-API identifica o numero por uma instancia, e o id
dela vem em toda mensagem recebida (`instanceId`): e a chave de roteamento, como o
`phone_number_id` e no caminho da Meta. O token da instancia NAO entra no banco —
fica em `ZAPI_TOKEN_<SLUG>`, pelo mesmo motivo do token do juridico.

Aditiva e anulavel, com indice unico: uma instancia pertence a uma empresa so, e
empresa sem Z-API segue sem o campo.

Revision ID: 0010_empresa_instancia_zapi
Revises: 0009_id_externo_da_meta
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_empresa_instancia_zapi"
down_revision = "0009_id_externo_da_meta"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("empresas", sa.Column("zapi_instancia_id", sa.String(64), nullable=True))
    op.create_index(
        "uq_empresas_zapi_instancia_id", "empresas", ["zapi_instancia_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_empresas_zapi_instancia_id", table_name="empresas")
    op.drop_column("empresas", "zapi_instancia_id")
