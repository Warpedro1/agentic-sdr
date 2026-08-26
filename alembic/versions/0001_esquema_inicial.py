"""Esquema inicial da Marina.

Revision ID: 0001_esquema_inicial
Revises:
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_esquema_inicial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("nome", sa.String(160)),
        sa.Column("telefone_e164", sa.String(20), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("fonte", sa.String(32)),
        sa.Column("fonte_raw", sa.Text()),
        sa.Column("tipo_contrato", sa.String(32)),
        sa.Column("tipo_contrato_raw", sa.Text()),
        sa.Column("possui_contrato", sa.Boolean()),
        sa.Column("consentimento_em", sa.DateTime(timezone=True)),
        sa.Column("canal", sa.String(20), nullable=False),
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
        # Deduplicacao: a chave do lead e o telefone.
        sa.UniqueConstraint("telefone_e164", name="uq_leads_telefone_e164"),
    )

    op.create_table(
        "consultores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("nome", sa.String(160), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("email", name="uq_consultores_email"),
    )

    op.create_table(
        "conversas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Nullable de proposito: a conversa existe antes do lead. Ver docstring
        # de app/models.py.
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("canal", sa.String(20), nullable=False),
        sa.Column("estado", sa.String(40), nullable=False),
        sa.Column("tentativas", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "contexto",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "ultimo_contato_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("encerrada_em", sa.DateTime(timezone=True)),
        sa.Column(
            "criada_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_conversas_lead_ultimo_contato", "conversas", ["lead_id", "ultimo_contato_em"]
    )

    op.create_table(
        "mensagens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversa_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direcao", sa.String(3), nullable=False),
        sa.Column("template_key", sa.String(60)),
        sa.Column("conteudo", sa.Text(), nullable=False),
        sa.Column(
            "criada_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_mensagens_conversa_criada", "mensagens", ["conversa_id", "criada_em"]
    )

    op.create_table(
        "regras_disponibilidade",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "consultor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consultores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dia_semana", sa.Integer(), nullable=False),
        sa.Column("hora_inicio", sa.Time(), nullable=False),
        sa.Column("hora_fim", sa.Time(), nullable=False),
        sa.Column("duracao_min", sa.Integer(), nullable=False, server_default=sa.text("60")),
        sa.Column("buffer_min", sa.Integer(), nullable=False, server_default=sa.text("15")),
        sa.UniqueConstraint(
            "consultor_id", "dia_semana", "hora_inicio", name="uq_regra_consultor_dia"
        ),
        sa.CheckConstraint("dia_semana BETWEEN 0 AND 6", name="ck_regra_dia_semana"),
        sa.CheckConstraint("hora_fim > hora_inicio", name="ck_regra_intervalo"),
    )

    op.create_table(
        "agendamentos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "consultor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consultores.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("inicio_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fim_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default=sa.text("'agendado'")
        ),
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
        sa.CheckConstraint("fim_utc > inicio_utc", name="ck_agendamento_intervalo"),
    )
    # A trava contra dupla marcacao. PARCIAL de proposito: so vale para status
    # 'agendado', entao cancelar um horario o devolve para a oferta.
    op.create_index(
        "uq_agendamento_consultor_inicio",
        "agendamentos",
        ["consultor_id", "inicio_utc"],
        unique=True,
        postgresql_where=sa.text("status = 'agendado'"),
    )
    op.create_index(
        "ix_agendamentos_consultor_inicio", "agendamentos", ["consultor_id", "inicio_utc"]
    )

    op.create_table(
        "documentos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("caminho", sa.Text(), nullable=False),
        sa.Column("mime", sa.String(120), nullable=False),
        sa.Column("tamanho_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "recebido_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "opt_outs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("telefone_e164", sa.String(20)),
        sa.Column("email", sa.String(320)),
        sa.Column("motivo", sa.Text()),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revogado_em", sa.DateTime(timezone=True)),
        # Nao se bloqueia um contato desconhecido: a linha so existe se houver
        # telefone ou e-mail para suprimir.
        sa.CheckConstraint(
            "telefone_e164 IS NOT NULL OR email IS NOT NULL",
            name="ck_opt_out_tem_contato",
        ),
    )
    op.create_index(
        "ix_opt_outs_telefone_criado", "opt_outs", ["telefone_e164", "criado_em"]
    )


def downgrade() -> None:
    op.drop_table("opt_outs")
    op.drop_table("documentos")
    op.drop_table("agendamentos")
    op.drop_table("regras_disponibilidade")
    op.drop_table("mensagens")
    op.drop_table("conversas")
    op.drop_table("consultores")
    op.drop_table("leads")
