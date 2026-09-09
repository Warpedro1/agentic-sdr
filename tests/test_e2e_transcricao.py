"""Teste de ponta a ponta: a conversa inteira, comparada com um snapshot.

Este e o teste que protege a COPY. Os outros verificam decisao e persistencia;
este congela o texto exato que o lead le, do "olá" ao agendamento.

Quando alguem ajustar o messages.yaml — que e justamente para poder ser ajustado
sem tocar em codigo — este teste falha e mostra o diff. Se a mudanca for
desejada, regenere:

    ATUALIZAR_SNAPSHOT=1 pytest tests/test_e2e_transcricao.py

O relogio e congelado porque a conversa cita datas de reuniao. Sem isso, o
snapshot mudaria todo dia.
"""

import os
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.channels.base import InboundMessage
from app.channels.terminal import ScriptedAdapter
from app.engine import Engine
from app.models import Consultor, Mensagem, RegraDisponibilidade

# Quarta-feira, 09:00 em Sao Paulo.
AGORA = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

SNAPSHOT = Path(__file__).parent / "snapshots" / "conversa_caminho_feliz.txt"

# Com telefone valido a Marina NAO pergunta o e-mail: um contato basta. Este e o
# caminho mais curto e o mais comum — e por isso o que o snapshot congela.
ROTEIRO = [
    "Pedro Guerra",
    "11 98765-4321",
    "5",  # fonte: indicacao
    "1",  # tipo de contrato: consignado
    "2",  # nao tem o contrato em maos
    "1",  # primeiro horario
    "1",  # confirma
]


async def _semear(sessao, empresa):
    consultor = Consultor(
        empresa_id=empresa.id, nome="Ana Ribeiro", email="ana@exemplo.com.br", ativo=True
    )
    sessao.add(consultor)
    await sessao.flush()
    for dia in range(0, 5):
        sessao.add(
            RegraDisponibilidade(
                empresa_id=empresa.id,
                consultor_id=consultor.id,
                dia_semana=dia,
                hora_inicio=time(9, 0),
                hora_fim=time(18, 0),
                duracao_min=60,
                buffer_min=15,
            )
        )
    await sessao.commit()


def _formatar(mensagens) -> str:
    linhas = []
    for mensagem in mensagens:
        if mensagem.direcao == "in":
            linhas.append(f"<< {mensagem.conteudo}")
        else:
            linhas.append(f">> [{mensagem.template_key}]")
            for linha in mensagem.conteudo.splitlines():
                linhas.append(f"   {linha}")
        linhas.append("")
    return "\n".join(linhas).rstrip() + "\n"


@pytest.mark.asyncio
async def test_transcricao_do_caminho_feliz(sessao_db, criar_sessao_db, empresa):
    await _semear(sessao_db, empresa)

    motor = Engine(
        criar_sessao=criar_sessao_db,
        empresa=empresa,
        adaptador=ScriptedAdapter([InboundMessage(texto=t) for t in ROTEIRO]),
        relogio=lambda: AGORA,
    )
    conversa_id = await motor.rodar()

    mensagens = (
        (
            await sessao_db.execute(
                select(Mensagem)
                .where(Mensagem.conversa_id == conversa_id)
                .order_by(Mensagem.sequencia)
            )
        )
        .scalars()
        .all()
    )
    transcricao = _formatar(mensagens)

    if os.environ.get("ATUALIZAR_SNAPSHOT"):
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(transcricao, encoding="utf-8")
        pytest.skip(f"snapshot regravado em {SNAPSHOT.name}")

    assert SNAPSHOT.is_file(), (
        f"snapshot ausente. Gere com: ATUALIZAR_SNAPSHOT=1 pytest {__file__}"
    )
    esperado = SNAPSHOT.read_text(encoding="utf-8")
    assert transcricao == esperado


@pytest.mark.asyncio
async def test_a_marina_nunca_fala_de_dinheiro(sessao_db, criar_sessao_db, empresa):
    """Guardrail de compliance lido na transcricao inteira, nao template a template.

    Se alguem colocar um numero na copy — um percentual, um valor — ele aparece
    aqui, em qualquer mensagem do fluxo.
    """
    await _semear(sessao_db, empresa)
    motor = Engine(
        criar_sessao=criar_sessao_db,
        empresa=empresa,
        adaptador=ScriptedAdapter(
            [InboundMessage(texto=t) for t in ["Pedro Guerra", "quanto vou receber?", "sair"]]
        ),
        relogio=lambda: AGORA,
    )
    conversa_id = await motor.rodar()

    saidas = (
        (
            await sessao_db.execute(
                select(Mensagem).where(
                    Mensagem.conversa_id == conversa_id, Mensagem.direcao == "out"
                )
            )
        )
        .scalars()
        .all()
    )
    texto = " ".join(m.conteudo for m in saidas).lower()

    assert "fora_de_escopo" in [m.template_key for m in saidas]
    for proibido in ("r$", "%", " reais", "taxa de", "juros de"):
        assert proibido not in texto, f"a Marina falou de dinheiro: {proibido!r}"
