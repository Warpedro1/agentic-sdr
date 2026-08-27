"""Ligacao com o CRM externo e fila de entrega.

Nomes genericos (`crm_`, e nao `juridico_`) de proposito: a Marina vai atender
mais de um sistema. Hoje o CRM externo e o juridico; a coluna guarda o id la.

Revision ID: 0003_integracao_crm
Revises: 0002_sequencia_mensagem
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_integracao_crm"
down_revision: str | None = "0002_sequencia_mensagem"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("crm_lead_id", sa.String(64), nullable=True))
    op.create_index("ix_leads_crm_lead_id", "leads", ["crm_lead_id"])

    op.add_column(
        "consultores", sa.Column("crm_usuario_id", sa.String(64), nullable=True)
    )
    op.create_index("ix_consultores_crm_usuario_id", "consultores", ["crm_usuario_id"])

    op.create_table(
        "crm_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Ordem de entrega: `criado_em` nao serve, porque o now() do Postgres e o
        # timestamp da transacao e tres itens gravados juntos empatariam.
        sa.Column(
            "sequencia", sa.BigInteger(), sa.Identity(always=False), nullable=False
        ),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("tentativas", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "proxima_tentativa_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("entregue_em", sa.DateTime(timezone=True)),
        sa.Column("ultimo_erro", sa.Text()),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("sequencia", name="uq_outbox_sequencia"),
    )
    op.create_index(
        "ix_outbox_pendentes", "crm_outbox", ["entregue_em", "proxima_tentativa_em"]
    )
    op.create_index("ix_outbox_lead_sequencia", "crm_outbox", ["lead_id", "sequencia"])


def downgrade() -> None:
    op.drop_table("crm_outbox")
    op.drop_index("ix_consultores_crm_usuario_id", table_name="consultores")
    op.drop_column("consultores", "crm_usuario_id")
    op.drop_index("ix_leads_crm_lead_id", table_name="leads")
    op.drop_column("leads", "crm_lead_id")
