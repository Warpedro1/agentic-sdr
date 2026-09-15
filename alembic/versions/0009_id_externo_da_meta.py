"""Identificador externo de mensagem com folga para a Meta.

`mensagens_recebidas.id_externo` nasceu com 64 caracteres, dimensionado pelo
`MessageSid` da Twilio (34). O `wamid` da Meta passa disso — o da primeira mensagem
real de teste tinha 78 — e gravar a marca de idempotencia estourava com
`StringDataRightTruncation`, devolvendo 500. A Meta reentrega o que recebe 500,
entao a mesma mensagem voltava e falhava de novo, sem nunca chegar ao engine.

A Meta nao documenta tamanho maximo para o `wamid`; 255 da mais de tres vezes o
observado. Alargar `varchar` no Postgres nao reescreve a tabela.

Revision ID: 0009_id_externo_da_meta
Revises: 0008_empresa_numero_meta
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_id_externo_da_meta"
down_revision = "0008_empresa_numero_meta"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "mensagens_recebidas",
        "id_externo",
        type_=sa.String(255),
        existing_type=sa.String(64),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Com um id da Meta ja gravado, estreitar falha. E o certo: truncar quebraria a
    # idempotencia em silencio, e a mesma mensagem seria atendida duas vezes.
    op.alter_column(
        "mensagens_recebidas",
        "id_externo",
        type_=sa.String(64),
        existing_type=sa.String(255),
        existing_nullable=False,
    )
