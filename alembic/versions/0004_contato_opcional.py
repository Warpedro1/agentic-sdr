"""Um contato basta: telefone deixa de ser obrigatorio.

Regra de negocio confirmada em 27/08/2026: o lead e cadastrado com nome e UM meio
de contato. Quem nao consegue informar um telefone valido e identificado pelo
e-mail.

O telefone continua sendo a identidade quando existe, e no Postgres o UNIQUE trata
cada NULL como distinto — entao varios leads sem telefone convivem.

O e-mail so identifica quem NAO tem telefone, e por isso a trava dele e PARCIAL.
Unicidade simples no e-mail seria forte demais: duas pessoas da mesma casa podem
dar o mesmo e-mail com telefones diferentes, e sao dois leads distintos.

Revision ID: 0004_contato_opcional
Revises: 0003_integracao_crm
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_contato_opcional"
down_revision: str | None = "0003_integracao_crm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "leads", "telefone_e164", existing_type=sa.String(20), nullable=True
    )
    op.create_index(
        "uq_leads_email_sem_telefone",
        "leads",
        ["email"],
        unique=True,
        postgresql_where=sa.text("telefone_e164 IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_leads_email_sem_telefone", table_name="leads")
    # Voltar exige que nao haja lead sem telefone; se houver, o ALTER falha — e
    # falhar e melhor do que inventar um telefone para caber na coluna.
    op.alter_column(
        "leads", "telefone_e164", existing_type=sa.String(20), nullable=False
    )
