"""A conversa passa a saber com quem esta falando.

Num canal em que a mensagem chega sozinha (WhatsApp), o programa precisa descobrir
de qual conversa ela faz parte antes de existir lead — e `conversas.lead_id` e nulo
ate o telefone ser validado. Dai a coluna com a identidade do canal.

O indice e UNICO de proposito. Alem de servir a busca ("a conversa aberta deste
numero"), e o que impede duas mensagens simultaneas do mesmo lead criarem duas
conversas. E parcial porque a exclusividade so vale enquanto a conversa esta
aberta: o mesmo numero volta amanha e abre outra.

`mensagens_recebidas` e a idempotencia do id do fornecedor (o `MessageSid`, no
Twilio). Precisa sobreviver a restart, entao e tabela e nao memoria.

Revision ID: 0005_conversa_whatsapp
Revises: 0004_contato_opcional
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_conversa_whatsapp"
down_revision: str | None = "0004_contato_opcional"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversas", sa.Column("identidade_externa", sa.String(40), nullable=True)
    )
    op.create_index(
        "uq_conversa_aberta_por_identidade",
        "conversas",
        ["canal", "identidade_externa"],
        unique=True,
        postgresql_where=sa.text(
            "encerrada_em IS NULL AND identidade_externa IS NOT NULL"
        ),
    )
    op.create_table(
        "mensagens_recebidas",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("canal", sa.String(20), nullable=False),
        sa.Column("id_externo", sa.String(64), nullable=False),
        sa.Column(
            "recebida_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("canal", "id_externo", name="uq_mensagem_recebida_externa"),
    )


def downgrade() -> None:
    op.drop_table("mensagens_recebidas")
    op.drop_index("uq_conversa_aberta_por_identidade", table_name="conversas")
    op.drop_column("conversas", "identidade_externa")
