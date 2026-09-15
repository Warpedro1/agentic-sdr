"""Identificador do numero na Meta, por empresa.

A Cloud API enderaca o envio pelo `phone_number_id`, nao pelo numero: a URL e
`/{versao}/{phone_number_id}/messages`. E o mesmo identificador vem em toda
mensagem recebida, entao passa a ser tambem a chave de roteamento para o caminho
da Meta — o numero continua roteando o caminho da Twilio.

Aditiva e anulavel: empresa que so atende pela Twilio ou so pelo terminal segue
sem o campo, e o indice unico nao casa com nulo no Postgres.

Revision ID: 0008_empresa_numero_meta
Revises: 0007_escopo_por_empresa
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_empresa_numero_meta"
down_revision = "0007_escopo_por_empresa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("empresas", sa.Column("whatsapp_numero_id", sa.String(40), nullable=True))
    op.create_index(
        "uq_empresas_whatsapp_numero_id", "empresas", ["whatsapp_numero_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_empresas_whatsapp_numero_id", table_name="empresas")
    op.drop_column("empresas", "whatsapp_numero_id")
