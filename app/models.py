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
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
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


class Empresa(Base):
    """Uma empresa atendida pela Marina — e, do outro lado, uma instalacao do juridico.

    Cada instalacao do juridico representa UMA empresa e seus funcionarios. E esta
    linha que liga as duas pontas: o numero de WhatsApp por onde a mensagem chega e
    o endereco do juridico para onde o lead vai.

    **O segredo de acesso nao mora aqui.** `juridico_token_env` guarda o NOME da
    variavel de ambiente que carrega o token. Uma coluna com o token apareceria em
    `pg_dump`, em replica e em qualquer `SELECT *` — e dumps de desenvolvimento
    circulam entre maquinas.
    """

    __tablename__ = "empresas"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: Chave estavel: linha de comando, nome de variavel de ambiente, log.
    slug: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Razao social, como o CRM da propria empresa a chama. Entra na saudacao, para
    #: o lead ouvir sempre a mesma denominacao dos dois lados.
    nome: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Como citar a empresa no meio da frase ("Como voce chegou ate a Leve?").
    #: Vazio cai para `nome`.
    nome_curto: Mapped[str | None] = mapped_column(String(60))
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: E.164 SEM o prefixo `whatsapp:`. E a chave de roteamento: a mensagem e
    #: atribuida a empresa dona do numero que a recebeu.
    #:
    #: Nulo e unico ao mesmo tempo: no Postgres cada NULL e distinto, entao varias
    #: empresas convivem sem numero (a que so roda no terminal, a que foi cadastrada
    #: antes de o remetente ser liberado) e o roteamento nunca casa com nulo.
    whatsapp_numero: Mapped[str | None] = mapped_column(String(20))

    juridico_ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    juridico_base_url: Mapped[str] = mapped_column(
        String(255), nullable=False, default=""
    )
    #: NOME da variavel de ambiente com o token — nunca o token.
    juridico_token_env: Mapped[str] = mapped_column(
        String(80), nullable=False, default=""
    )
    #: Id da empresa no juridico, capturado no cadastro. Diagnostico apenas: e local
    #: daquela instalacao, entao vale 1 em quase todas e nao serve como chave.
    crm_empresa_id: Mapped[str | None] = mapped_column(String(64))

    criado_em: Mapped[datetime] = _agora()
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("slug", name="uq_empresas_slug"),
        Index("uq_empresas_whatsapp_numero", "whatsapp_numero", unique=True),
    )


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: A empresa dona desta linha. Obrigatoria: e o que impede uma consulta
    #: alcancar o dado de outra empresa.
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("empresas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    nome: Mapped[str | None] = mapped_column(String(160))
    #: Um contato basta: quem nao consegue informar um telefone valido e
    #: identificado pelo e-mail. Por isso os dois sao opcionais aqui — mas a
    #: conversa nunca cadastra um lead sem NENHUM dos dois: ela escala antes.
    telefone_e164: Mapped[str | None] = mapped_column(String(20))
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

    #: Id deste lead no CRM externo (hoje o `juridico`). Escalar de proposito:
    #: o lead pertence a UMA empresa e cada empresa tem uma instalacao do juridico,
    #: entao nao existe o caso de um mesmo lead viver em dois backends.
    crm_lead_id: Mapped[str | None] = mapped_column(String(64), index=True)

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

    # O telefone e a identidade DENTRO DE UMA EMPRESA — e no Postgres o UNIQUE
    # trata cada vazio como distinto, entao varios leads sem telefone convivem.
    #
    # Por empresa, e nao global: cada instalacao do juridico e um escritorio
    # diferente, e a mesma pessoa atendida por dois deles sao dois cadastros com
    # historicos que nao se misturam.
    #
    # O e-mail so identifica quem NAO tem telefone, e por isso a trava dele e
    # parcial. Unicidade simples no e-mail seria forte demais: duas pessoas da
    # mesma casa podem dar o mesmo e-mail e telefones diferentes, e sao dois leads.
    __table_args__ = (
        UniqueConstraint(
            "empresa_id", "telefone_e164", name="uq_leads_empresa_telefone"
        ),
        Index(
            "uq_leads_email_sem_telefone",
            "empresa_id",
            "email",
            unique=True,
            postgresql_where=text("telefone_e164 IS NULL"),
        ),
    )


class Conversa(Base):
    __tablename__ = "conversas"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: A empresa dona desta linha. Obrigatoria: e o que impede uma consulta
    #: alcancar o dado de outra empresa.
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("empresas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE")
    )
    canal: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Quem esta do outro lado, do jeito que o canal identifica: o telefone E.164
    #: no WhatsApp, vazio no terminal.
    #:
    #: Existe porque `lead_id` e nulo ate o telefone ser validado, e num canal em
    #: que a mensagem chega sozinha e preciso descobrir de qual conversa ela faz
    #: parte ANTES de existir lead. No WhatsApp essa identidade chega junto com a
    #: primeira mensagem.
    identidade_externa: Mapped[str | None] = mapped_column(String(40))
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
        back_populates="conversa", order_by="Mensagem.sequencia"
    )

    __table_args__ = (
        Index("ix_conversas_lead_ultimo_contato", "lead_id", "ultimo_contato_em"),
        # Serve a duas coisas ao mesmo tempo: achar a conversa aberta de um numero
        # em uma consulta, e impedir que duas mensagens simultaneas do mesmo lead
        # criem duas conversas. E parcial porque so vale enquanto a conversa esta
        # aberta — o mesmo numero volta amanha e abre outra.
        Index(
            "uq_conversa_aberta_por_identidade",
            "empresa_id",
            "canal",
            "identidade_externa",
            unique=True,
            postgresql_where=text(
                "encerrada_em IS NULL AND identidade_externa IS NOT NULL"
            ),
        ),
    )


class Mensagem(Base):
    __tablename__ = "mensagens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversa_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversas.id", ondelete="CASCADE"), nullable=False
    )
    #: Ordem dentro da conversa. NAO da para confiar em `criada_em` para isso: o
    #: `now()` do Postgres e o timestamp da TRANSACAO, entao mensagens gravadas
    #: juntas ficam com a mesma hora e o desempate cairia no UUID, que e
    #: aleatorio. A transcricao e o dataset de avaliacao do projeto — ordem
    #: errada corrompe o dataset.
    sequencia: Mapped[int] = mapped_column(Integer, nullable=False)
    direcao: Mapped[str] = mapped_column(String(3), nullable=False)
    template_key: Mapped[str | None] = mapped_column(String(60))
    conteudo: Mapped[str] = mapped_column(Text, nullable=False)
    criada_em: Mapped[datetime] = _agora()

    conversa: Mapped[Conversa] = relationship(back_populates="mensagens")

    __table_args__ = (
        Index("ix_mensagens_conversa_criada", "conversa_id", "criada_em"),
        UniqueConstraint("conversa_id", "sequencia", name="uq_mensagem_sequencia"),
    )


class MensagemRecebida(Base):
    """Idempotencia das mensagens que chegam de um canal externo.

    O fornecedor pode entregar a mesma mensagem duas vezes (retentativa, uma
    resposta lenta que ele considerou perdida). Sem esta trava, a mesma mensagem
    viraria dois passos da conversa — e no meio de uma pergunta com contador de
    tentativas isso muda o rumo do atendimento.

    E tabela, e nao um conjunto em memoria, porque a garantia precisa sobreviver a
    um restart do processo.
    """

    __tablename__ = "mensagens_recebidas"

    id: Mapped[uuid.UUID] = _uuid_pk()
    canal: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Identificador do fornecedor. No Twilio e o `MessageSid`.
    id_externo: Mapped[str] = mapped_column(String(64), nullable=False)
    recebida_em: Mapped[datetime] = _agora()

    __table_args__ = (
        UniqueConstraint("canal", "id_externo", name="uq_mensagem_recebida_externa"),
    )


class Consultor(Base):
    __tablename__ = "consultores"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: A empresa dona desta linha. Obrigatoria: e o que impede uma consulta
    #: alcancar o dado de outra empresa.
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("empresas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    nome: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Usuario correspondente no CRM externo. E o que faz o nome que a Marina
    #: promete ao lead ser o mesmo que aparece na agenda do outro sistema.
    crm_usuario_id: Mapped[str | None] = mapped_column(String(64), index=True)

    regras: Mapped[list[RegraDisponibilidade]] = relationship(
        back_populates="consultor"
    )
    agendamentos: Mapped[list[Agendamento]] = relationship(back_populates="consultor")

    __table_args__ = (
        # Por empresa: a mesma pessoa pode atender dois escritorios.
        UniqueConstraint("empresa_id", "email", name="uq_consultores_empresa_email"),
        # Parece redundante com a chave primaria, e nao e: existe para ser o alvo
        # da chave estrangeira composta de `regras_disponibilidade`, que e o que
        # impede uma regra de agenda apontar para consultor de outra empresa.
        UniqueConstraint("id", "empresa_id", name="uq_consultores_id_empresa"),
    )


class RegraDisponibilidade(Base):
    __tablename__ = "regras_disponibilidade"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: A empresa dona desta linha. Obrigatoria: e o que impede uma consulta
    #: alcancar o dado de outra empresa.
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("empresas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Sem chave estrangeira propria: quem garante a integridade e a chave
    #: COMPOSTA em `__table_args__`, que amarra consultor e empresa juntos.
    consultor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: 0 = segunda ... 6 = domingo (mesma convencao de datetime.weekday()).
    dia_semana: Mapped[int] = mapped_column(Integer, nullable=False)
    hora_inicio: Mapped[time] = mapped_column(Time, nullable=False)
    hora_fim: Mapped[time] = mapped_column(Time, nullable=False)
    duracao_min: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    buffer_min: Mapped[int] = mapped_column(Integer, nullable=False, default=15)

    consultor: Mapped[Consultor] = relationship(back_populates="regras")

    __table_args__ = (
        # Composta de proposito: torna IMPOSSIVEL uma regra de agenda apontar para
        # consultor de outra empresa. `empresa_id` aqui e desnormalizacao (a
        # empresa e derivavel do consultor), e sem esta chave seria um segundo
        # lugar onde a verdade pode divergir — divergencia em agenda vira horario
        # oferecido pela empresa errada.
        ForeignKeyConstraint(
            ["consultor_id", "empresa_id"],
            ["consultores.id", "consultores.empresa_id"],
            name="fk_regra_consultor_empresa",
            ondelete="CASCADE",
        ),
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
    #: A empresa dona desta linha. Obrigatoria: e o que impede uma consulta
    #: alcancar o dado de outra empresa.
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("empresas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
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


class CrmOutbox(Base):
    """Fila de entrega para o CRM externo.

    A conversa NAO pode travar porque outro sistema esta lento ou fora do ar.
    O engine grava a intencao aqui — operacao local e rapida — e a entrega
    acontece a parte, com nova tentativa. Se o CRM voltar depois, tudo chega.

    Sem isto, uma indisponibilidade do juridico viraria uma indisponibilidade da
    Marina, que e o oposto do que se quer de uma automacao de atendimento.
    """

    __tablename__ = "crm_outbox"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: Ordem de entrega. Mesmo motivo de `Mensagem.sequencia`: `criado_em` usa o
    #: `now()` do Postgres, que e o timestamp da TRANSACAO — tres itens gravados
    #: no mesmo passo ficam com a hora identica e a ordem cairia no UUID, que e
    #: aleatorio. Numa fila isso significa tentar agendar antes de o lead existir.
    sequencia: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )
    #: A empresa dona desta linha. Obrigatoria: e o que impede uma consulta
    #: alcancar o dado de outra empresa.
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("empresas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: "lead", "qualificacao" ou "agendamento".
    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE")
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    tentativas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proxima_tentativa_em: Mapped[datetime] = _agora()
    entregue_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ultimo_erro: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = _agora()

    __table_args__ = (
        Index("ix_outbox_pendentes", "entregue_em", "proxima_tentativa_em"),
        Index("ix_outbox_lead_sequencia", "lead_id", "sequencia"),
    )


__all__ = [
    "Base",
    "Empresa",
    "Lead",
    "Conversa",
    "Mensagem",
    "Consultor",
    "RegraDisponibilidade",
    "Agendamento",
    "Documento",
    "OptOut",
    "CrmOutbox",
]
