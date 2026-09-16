"""O engine atendendo por mensagem recebida, e não por laço.

`rodar()` conduz uma conversa e fica esperando; `atender()` recebe UMA mensagem e
volta. A diferença que importa está aqui: a `Execucao` não sobrevive entre
chamadas — ela é reconstruída do banco a cada mensagem. É isso que permite
milhares de conversas em paralelo, e é onde mora o risco.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.channels.base import InboundMessage, MensagemSaida
from app.config import Configuracao
from app.engine import Engine
from app.enums import Estado
from app.models import Conversa, Lead, Mensagem

AGORA = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
TELEFONE = "+5511987654321"


class AdaptadorDeEmpurrao:
    """Canal de push de mentira: ninguém puxa, e as respostas ficam guardadas."""

    canal = "whatsapp"

    def __init__(self) -> None:
        self.enviadas: list[MensagemSaida] = []

    async def receber(self) -> InboundMessage | None:
        return None

    async def enviar(self, mensagem: MensagemSaida) -> None:
        self.enviadas.append(mensagem)


def montar(criar_sessao, empresa, agora=AGORA) -> tuple[Engine, AdaptadorDeEmpurrao]:
    adaptador = AdaptadorDeEmpurrao()
    motor = Engine(
        criar_sessao=criar_sessao,
        adaptador=adaptador,
        empresa=empresa,
        config=Configuracao(juridico_ativo=False),
        relogio=lambda: agora,
    )
    return motor, adaptador


def txt(valor: str) -> InboundMessage:
    return InboundMessage(texto=valor)


@pytest.mark.asyncio
async def test_primeira_mensagem_abre_a_conversa_e_so_saúda(sessao_db, criar_sessao_db, empresa):
    motor, adaptador = montar(criar_sessao_db, empresa)
    conversa_id = await motor.atender(TELEFONE, txt("oi"))

    conversa = await sessao_db.get(Conversa, conversa_id)
    assert conversa.identidade_externa == TELEFONE
    assert conversa.canal == "whatsapp"
    # O "oi" abriu a conversa e disparou a saudação. Se ele tivesse sido
    # consumido como resposta, viraria tentativa de nome — e queimaria uma das
    # três antes de a Marina ter perguntado qualquer coisa.
    assert conversa.estado == Estado.AGUARDANDO_NOME.value
    assert conversa.tentativas == 0
    assert [m.template_key for m in adaptador.enviadas] == [
        "boas_vindas",
        "pergunta_nome",
    ]

    # Mesmo não sendo consumido, o "oi" está na transcrição: é o que o lead
    # mandou.
    entradas = (
        (
            await sessao_db.execute(
                select(Mensagem).where(
                    Mensagem.conversa_id == conversa_id, Mensagem.direcao == "in"
                )
            )
        )
        .scalars()
        .all()
    )
    assert [m.conteudo for m in entradas] == ["oi"]


@pytest.mark.asyncio
async def test_mensagens_seguintes_avancam_a_mesma_conversa(sessao_db, criar_sessao_db, empresa):
    motor, _ = montar(criar_sessao_db, empresa)
    for texto in ["oi", "Pedro Guerra", "11 98765-4321"]:
        conversa_id = await motor.atender(TELEFONE, txt(texto))

    quantas = await sessao_db.scalar(select(func.count()).select_from(Conversa))
    assert quantas == 1

    conversa = await sessao_db.get(Conversa, conversa_id)
    assert conversa.lead_id is not None
    # O contador da transcrição foi reidratado a cada mensagem: sem buraco e sem
    # repetição, apesar de cada turno ser um processo do zero.
    sequencias = (
        (
            await sessao_db.execute(
                select(Mensagem.sequencia)
                .where(Mensagem.conversa_id == conversa_id)
                .order_by(Mensagem.sequencia)
            )
        )
        .scalars()
        .all()
    )
    assert sequencias == list(range(1, len(sequencias) + 1))


@pytest.mark.asyncio
async def test_consentimento_sobrevive_a_reidratacao(sessao_db, criar_sessao_db, empresa):
    """O JSONB devolve texto; a coluna do lead quer data.

    O consentimento é carimbado na saudação e só é usado duas mensagens depois,
    quando o lead nasce. No terminal ele ficava na memória do laço; aqui passa
    pelo banco e volta como string.
    """
    motor, _ = montar(criar_sessao_db, empresa)
    for texto in ["oi", "Pedro Guerra", "11 98765-4321"]:
        await motor.atender(TELEFONE, txt(texto))

    lead = (await sessao_db.execute(select(Lead))).scalar_one()
    assert lead.consentimento_em == AGORA


@pytest.mark.asyncio
async def test_conversa_encerrada_nao_recebe_mensagem_nova(sessao_db, criar_sessao_db, empresa):
    motor, _ = montar(criar_sessao_db, empresa)
    primeira = await motor.atender(TELEFONE, txt("oi"))

    # SAIR encerra por opt-out.
    await motor.atender(TELEFONE, txt("sair"))
    encerrada = await sessao_db.get(Conversa, primeira)
    assert encerrada.encerrada_em is not None

    segunda = await motor.atender(TELEFONE, txt("oi de novo"))
    assert segunda != primeira


@pytest.mark.asyncio
async def test_conversa_parada_ha_tempo_demais_recomeca(sessao_db, criar_sessao_db, empresa):
    motor, _ = montar(criar_sessao_db, empresa)
    primeira = await motor.atender(TELEFONE, txt("oi"))

    # Um dia e meio depois: retomar no meio de uma pergunta, sem contexto
    # nenhum, é pior do que começar de novo.
    depois, _ = montar(criar_sessao_db, empresa, agora=AGORA + timedelta(hours=36))
    segunda = await depois.atender(TELEFONE, txt("oi"))

    assert segunda != primeira
    antiga = await sessao_db.get(Conversa, primeira)
    assert antiga.encerrada_em is not None


@pytest.mark.asyncio
async def test_dentro_da_janela_a_conversa_continua(sessao_db, criar_sessao_db, empresa):
    motor, _ = montar(criar_sessao_db, empresa)
    primeira = await motor.atender(TELEFONE, txt("oi"))

    depois, _ = montar(criar_sessao_db, empresa, agora=AGORA + timedelta(hours=12))
    segunda = await depois.atender(TELEFONE, txt("Pedro Guerra"))

    assert segunda == primeira


@pytest.mark.asyncio
async def test_numeros_diferentes_sao_conversas_diferentes(sessao_db, criar_sessao_db, empresa):
    motor, _ = montar(criar_sessao_db, empresa)
    uma = await motor.atender(TELEFONE, txt("oi"))
    outra = await motor.atender("+5511900000000", txt("oi"))

    assert uma != outra
    quantas = await sessao_db.scalar(select(func.count()).select_from(Conversa))
    assert quantas == 2


@pytest.mark.asyncio
async def test_duas_mensagens_juntas_nao_duplicam_a_conversa(sessao_db, criar_sessao_db, empresa):
    """A corrida que só existe num canal de push.

    Duas mensagens do mesmo número chegando ao mesmo tempo: sem a trava, cada uma
    abriria a sua conversa, ou uma sobrescreveria o estado da outra.
    """
    primeiro, _ = montar(criar_sessao_db, empresa)
    segundo, _ = montar(criar_sessao_db, empresa)

    resultados = await asyncio.gather(
        primeiro.atender(TELEFONE, txt("oi")),
        segundo.atender(TELEFONE, txt("Pedro Guerra")),
    )

    quantas = await sessao_db.scalar(select(func.count()).select_from(Conversa))
    assert quantas == 1
    assert resultados[0] == resultados[1]

    # As duas entradas estão na transcrição, com sequências distintas: uma
    # esperou a outra em vez de sobrescrevê-la.
    sequencias = (
        (
            await sessao_db.execute(
                select(Mensagem.sequencia).where(Mensagem.direcao == "in")
            )
        )
        .scalars()
        .all()
    )
    assert len(sequencias) == len(set(sequencias)) == 2


async def test_a_trava_e_solta_mesmo_com_o_pool_cheio(sessao_db, criar_sessao_db, empresa):
    """A trava de sessao precisa sair pela MESMA conexao que a pegou.

    Com a conexao devolvida ao pool no meio do turno, o unlock saia por outra, nao
    soltava nada, e a trava ficava esquecida numa conexao ociosa: a mensagem
    seguinte daquele numero esperava para sempre. Em 16/09/2026 a Marina parou de
    responder assim, depois de dois leads conversando ao mesmo tempo aquecerem o
    pool.
    """
    # Varias conexoes ociosas no pool, como depois de atendimentos simultaneos.
    sessoes = [criar_sessao_db() for _ in range(4)]
    for sessao in sessoes:
        await sessao.execute(select(1))
    for sessao in sessoes:
        await sessao.close()

    motor, _ = montar(criar_sessao_db, empresa)
    await motor.atender(TELEFONE, txt("oi"))

    presas = await sessao_db.scalar(
        text(
            "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
            "WHERE l.locktype = 'advisory' AND l.granted AND d.datname = current_database()"
        )
    )
    assert presas == 0

    # E a prova pelo sintoma: a mensagem seguinte do mesmo numero e atendida.
    seguinte, _ = montar(criar_sessao_db, empresa)
    await asyncio.wait_for(seguinte.atender(TELEFONE, txt("Pedro Guerra")), timeout=10)


@pytest.mark.asyncio
async def test_retomada_volta_ao_estado_exato_da_conversa_anterior(
    sessao_db, criar_sessao_db, empresa
):
    """`reconhecimento` é transitório, e a retomada o lê depois de esperar.

    No terminal a chave sobrevivia na memória do laço. Aqui ela é refeita do
    banco na reidratação — senão o lead que responde "sim" cairia no caminho
    genérico de coleta em vez de voltar onde parou.
    """
    lead = Lead(
        empresa_id=empresa.id,
        telefone_e164=TELEFONE,
        nome="Pedro Guerra",
        fonte="indicacao",
        tipo_contrato="consignado",
        consentimento_em=AGORA - timedelta(hours=5),
        canal="whatsapp",
    )
    sessao_db.add(lead)
    await sessao_db.flush()
    sessao_db.add(
        Conversa(
            empresa_id=empresa.id,
            lead_id=lead.id,
            canal="whatsapp",
            estado=Estado.AGUARDANDO_TIPO_CONTRATO.value,
            tentativas=0,
            contexto={},
            ultimo_contato_em=AGORA - timedelta(hours=5),
            encerrada_em=AGORA - timedelta(hours=5),
        )
    )
    await sessao_db.commit()

    motor, _ = montar(criar_sessao_db, empresa)
    for texto in ["oi", "Pedro Guerra", "11 98765-4321"]:
        conversa_id = await motor.atender(TELEFONE, txt(texto))

    conversa = await sessao_db.get(Conversa, conversa_id)
    assert conversa.estado == Estado.AGUARDANDO_RETOMADA_CONVERSA.value

    await motor.atender(TELEFONE, txt("sim"))
    await sessao_db.refresh(conversa)
    # O lead já tem tipo_contrato: a coleta genérica pularia direto para a
    # pergunta seguinte. Voltar para AGUARDANDO_TIPO_CONTRATO só acontece se o
    # estado anterior foi reconstruído.
    assert conversa.estado == Estado.AGUARDANDO_TIPO_CONTRATO.value
