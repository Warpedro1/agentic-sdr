"""Fila de entrega para o CRM externo.

Genérico: não sabe o que é o juridico. Guarda intenções e devolve as pendentes
na ordem certa; quem entrega é um "entregador" específico de cada backend.

Duas garantias que este módulo dá, e que são o motivo de ele existir:

1. **A conversa nunca espera.** Enfileirar é uma escrita local e rápida. Se o
   CRM estiver fora do ar, o lead não fica travado no meio do atendimento.
2. **A ordem por lead é respeitada.** Não adianta mandar o agendamento antes de
   o lead existir lá. Se um item de um lead falha, os seguintes DAQUELE lead
   esperam — os de outros leads seguem normalmente.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CrmOutbox


class TipoEnvio(StrEnum):
    LEAD = "lead"
    QUALIFICACAO = "qualificacao"
    AGENDAMENTO = "agendamento"


@dataclass(frozen=True)
class Backoff:
    """Espera crescente entre tentativas.

    Sem teto, uma falha longa levaria a próxima tentativa para daqui a semanas;
    com teto de 30 min, o item volta a ser tentado num prazo útil assim que o
    outro sistema voltar.
    """

    base_segundos: int = 30
    teto_segundos: int = 1800

    def proxima(self, tentativas: int) -> timedelta:
        return timedelta(
            seconds=min(self.base_segundos * (2**max(tentativas - 1, 0)), self.teto_segundos)
        )


async def enfileirar(
    sessao: AsyncSession,
    tipo: TipoEnvio,
    payload: dict,
    *,
    empresa_id: uuid.UUID,
    lead_id: uuid.UUID | None = None,
    agora: datetime | None = None,
) -> uuid.UUID:
    """Grava a intencao de entrega. `empresa_id` diz para QUAL juridico ela vai.

    Obrigatorio e nomeado de proposito: cada empresa tem a sua instalacao do
    juridico, e um item sem destino seria entregue por quem drenar primeiro — no
    CRM da empresa errada.
    """
    item = CrmOutbox(
        tipo=tipo.value,
        empresa_id=empresa_id,
        lead_id=lead_id,
        payload=payload,
        tentativas=0,
        **({"proxima_tentativa_em": agora} if agora else {}),
    )
    sessao.add(item)
    await sessao.flush()
    return item.id


async def pendentes(
    sessao: AsyncSession, agora: datetime, max_tentativas: int, limite: int = 100
) -> list[CrmOutbox]:
    """Itens prontos para entrega, na ordem em que foram criados.

    Quem esgotou as tentativas fica de fora: continua no banco para inspeção,
    mas não fica sendo retentado para sempre.

    **`FOR UPDATE SKIP LOCKED` não é detalhe de performance — é correção.** A
    drenagem é disparada a cada marco da conversa, então várias rodadas correm ao
    mesmo tempo. Sem a reserva, todas liam o mesmo item pendente e todas o
    entregavam: um lead virava três cadastros no CRM, criados no mesmo
    milissegundo. Com ela, quem chegar depois simplesmente não enxerga o que já
    está sendo processado, e a linha fica travada até o commit da rodada.
    """
    resultado = await sessao.execute(
        select(CrmOutbox)
        .where(
            CrmOutbox.entregue_em.is_(None),
            CrmOutbox.proxima_tentativa_em <= agora,
            CrmOutbox.tentativas < max_tentativas,
        )
        .order_by(CrmOutbox.sequencia)
        .limit(limite)
        .with_for_update(skip_locked=True)
    )
    return list(resultado.scalars().all())


def marcar_entregue(item: CrmOutbox, agora: datetime) -> None:
    item.entregue_em = agora
    item.ultimo_erro = None


def marcar_falha(
    item: CrmOutbox,
    erro: str,
    agora: datetime,
    temporario: bool,
    max_tentativas: int,
    backoff: Backoff | None = None,
) -> None:
    """Registra a falha e decide se vale tentar de novo.

    Erro permanente (um 400 de payload inválido, por exemplo) não melhora com
    repetição: queima as tentativas de uma vez para o item sair da fila e virar
    algo que uma pessoa precisa olhar.
    """
    item.ultimo_erro = erro[:2000]
    if not temporario:
        item.tentativas = max_tentativas
        return
    item.tentativas += 1
    item.proxima_tentativa_em = agora + (backoff or Backoff()).proxima(item.tentativas)


async def falhados(sessao: AsyncSession, max_tentativas: int) -> list[CrmOutbox]:
    """O que desistiu de tentar. É a lista que alguém precisa olhar."""
    resultado = await sessao.execute(
        select(CrmOutbox)
        .where(CrmOutbox.entregue_em.is_(None), CrmOutbox.tentativas >= max_tentativas)
        .order_by(CrmOutbox.sequencia)
    )
    return list(resultado.scalars().all())
