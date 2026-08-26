"""Modelo de dados da Marina.

Duas regras que valem para o arquivo inteiro:

1. Toda data e hora e gravada em UTC (`DateTime(timezone=True)`). A conversao para
   America/Sao_Paulo acontece so na renderizacao.
2. `conversas.lead_id` e nullable de proposito. O fluxo pede o nome antes do
   telefone, mas a deduplicacao e por telefone: criar o lead antes disso geraria
   uma linha sem telefone e, no passo seguinte, a descoberta de que a pessoa ja
   tinha cadastro. O lead so e materializado quando o telefone e validado; ate la,
   nome, e-mail e consentimento ficam em `conversas.contexto`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Any:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _agora() -> Any:
    return mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = _uuid_pk()
    nome: Mapped[str | None] = mapped_column(String(160))
    telefone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))

    fonte: Mapped[str | None] = mapped_column(String(32))
    fonte_raw: Mapped[str | None] = mapped_column(Text)

    tipo_contrato: Mapped[str | None] = mapped_column(String(32))
    tipo_contrato_raw: Mapped[str | None] = mapped_column(Text)

    possui_contrato: Mapped[bool | None] = mapped_column(Boolean)

    # Carimbado no momento em que o consentimento foi dado (na saudacao), nao no
    # momento em que a linha foi criada. E esse carimbo que precisa ser defensavel.
    consentimento_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    canal: Mapped[str] = mapped_column(String(20), nullable=False)

    criado_em: Mapped[datetime] = _agora()
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    conversas: Mapped[list[Conversa]] = relationship(back_populates="lead")
    documentos: Mapped[list[Documento]] = relationship(back_populates="lead")
    agendamentos: Mapped[list[Agendamento]] = relationship(back_populates="lead")

    __table_args__ = (
        UniqueConstraint("telefone_e164", name="uq_leads_telefone_e164"),
    )


class Conversa(Base):
    __tablename__ = "conversas"

    id: Mapped[uuid.UUID] = _uuid_pk()
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE")
    )
    canal: Mapped[str] = mapped_column(String(20), nullable=False)
    estado: Mapped[str] = mapped_column(String(40), nullable=False)
    tentativas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contexto: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    ultimo_contato_em: Mapped[datetime] = _agora()
    encerrada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    criada_em: Mapped[datetime] = _agora()

    lead: Mapped[Lead | None] = relationship(back_populates="conversas")
    mensagens: Mapped[list[Mensagem]] = relationship(
        back_populates="conversa", order_by="Mensagem.criada_em"
    )

    __table_args__ = (
        Index("ix_conversas_lead_ultimo_contato", "lead_id", "ultimo_contato_em"),
    )


class Mensagem(Base):
    __tablename__ = "mensagens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversa_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversas.id", ondelete="CASCADE"), nullable=False
    )
    direcao: Mapped[str] = mapped_column(String(3), nullable=False)
    template_key: Mapped[str | None] = mapped_column(String(60))
    conteudo: Mapped[str] = mapped_column(Text, nullable=False)
    criada_em: Mapped[datetime] = _agora()

    conversa: Mapped[Conversa] = relationship(back_populates="mensagens")

    __table_args__ = (Index("ix_mensagens_conversa_criada", "conversa_id", "criada_em"),)


class Consultor(Base):
    __tablename__ = "consultores"

    id: Mapped[uuid.UUID] = _uuid_pk()
    nome: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    regras: Mapped[list[RegraDisponibilidade]] = relationship(
        back_populates="consultor"
    )
    agendamentos: Mapped[list[Agendamento]] = relationship(back_populates="consultor")

    __table_args__ = (UniqueConstraint("email", name="uq_consultores_email"),)


class RegraDisponibilidade(Base):
    __tablename__ = "regras_disponibilidade"

    id: Mapped[uuid.UUID] = _uuid_pk()
    consultor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("consultores.id", ondelete="CASCADE"), nullable=False
    )
    #: 0 = segunda ... 6 = domingo (mesma convencao de datetime.weekday()).
    dia_semana: Mapped[int] = mapped_column(Integer, nullable=False)
    hora_inicio: Mapped[time] = mapped_column(Time, nullable=False)
    hora_fim: Mapped[time] = mapped_column(Time, nullable=False)
    duracao_min: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    buffer_min: Mapped[int] = mapped_column(Integer, nullable=False, default=15)

    consultor: Mapped[Consultor] = relationship(back_populates="regras")

    __table_args__ = (
        UniqueConstraint(
            "consultor_id", "dia_semana", "hora_inicio", name="uq_regra_consultor_dia"
        ),
        CheckConstraint("dia_semana BETWEEN 0 AND 6", name="ck_regra_dia_semana"),
        CheckConstraint("hora_fim > hora_inicio", name="ck_regra_intervalo"),
    )


class Agendamento(Base):
    __tablename__ = "agendamentos"

    id: Mapped[uuid.UUID] = _uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    consultor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("consultores.id", ondelete="RESTRICT"), nullable=False
    )
    inicio_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fim_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="agendado")

    criado_em: Mapped[datetime] = _agora()
    atualizado_em: Mapped[datetime] = _agora()

    lead: Mapped[Lead] = relationship(back_populates="agendamentos")
    consultor: Mapped[Consultor] = relationship(back_populates="agendamentos")

    __table_args__ = (
        # Rede de seguranca contra dupla marcacao, no nivel do banco. Indice
        # PARCIAL: um horario cancelado libera o slot para nova reserva.
        Index(
            "uq_agendamento_consultor_inicio",
            "consultor_id",
            "inicio_utc",
            unique=True,
            postgresql_where=text("status = 'agendado'"),
        ),
        Index("ix_agendamentos_consultor_inicio", "consultor_id", "inicio_utc"),
        CheckConstraint("fim_utc > inicio_utc", name="ck_agendamento_intervalo"),
    )


class Documento(Base):
    __tablename__ = "documentos"

    id: Mapped[uuid.UUID] = _uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    caminho: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(String(120), nullable=False)
    tamanho_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    recebido_em: Mapped[datetime] = _agora()

    lead: Mapped[Lead] = relationship(back_populates="documentos")


class OptOut(Base):
    __tablename__ = "opt_outs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    telefone_e164: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(320))
    motivo: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = _agora()
    #: Preenchido quando a pessoa volta e confirma que quer retomar o atendimento.
    #: Opt-out ATIVO = registro mais recente do telefone com esta coluna vazia.
    revogado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_opt_outs_telefone_criado", "telefone_e164", "criado_em"),
        # Nao se bloqueia um contato desconhecido: a linha so existe se houver
        # telefone ou e-mail para suprimir.
        CheckConstraint(
            "telefone_e164 IS NOT NULL OR email IS NOT NULL",
            name="ck_opt_out_tem_contato",
        ),
    )


__all__ = [
    "Base",
    "Lead",
    "Conversa",
    "Mensagem",
    "Consultor",
    "RegraDisponibilidade",
    "Agendamento",
    "Documento",
    "OptOut",
]
