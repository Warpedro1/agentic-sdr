"""Ordem explicita da transcricao.

`criada_em` nao serve para ordenar: o `now()` do Postgres devolve o timestamp da
TRANSACAO, entao mensagens gravadas no mesmo passo ficam com a hora identica e o
desempate cai no UUID da chave primaria, que e aleatorio. Na pratica a saudacao
podia aparecer depois da primeira pergunta.

Como a transcricao e o dataset contra o qual a versao futura com IA vai ser
comparada, a ordem precisa ser um dado, e nao um efeito da resolucao do relogio.

Revision ID: 0002_sequencia_mensagem
Revises: 0001_esquema_inicial
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_sequencia_mensagem"
down_revision: str | None = "0001_esquema_inicial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mensagens", sa.Column("sequencia", sa.Integer(), nullable=True))

    # Conversas ja gravadas recebem uma ordem plausivel a partir do que existe.
    op.execute(
        """
        UPDATE mensagens AS m
        SET sequencia = ordenadas.posicao
        FROM (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY conversa_id ORDER BY criada_em, id
                   ) AS posicao
            FROM mensagens
        ) AS ordenadas
        WHERE m.id = ordenadas.id
        """
    )

    op.alter_column("mensagens", "sequencia", nullable=False)
    op.create_unique_constraint(
        "uq_mensagem_sequencia", "mensagens", ["conversa_id", "sequencia"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_mensagem_sequencia", "mensagens", type_="unique")
    op.drop_column("mensagens", "sequencia")
