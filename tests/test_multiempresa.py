"""O isolamento entre empresas, contado de uma vez só.

Cada instalação do juridico é um escritório diferente. Estes testes provam que o
dado de um não alcança o outro — em nenhuma direção, e mesmo diante de dado
corrompido ou chamada malformada.

Estão juntos de propósito: espalhados pelos arquivos de cada módulo, a história
some. Aqui ela se lê inteira.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.channels.base import InboundMessage, MensagemSaida
from app.crm import CrmSinkOutbox
from app.engine import Engine
from app.enums import Estado
from app.integrations.juridico.cliente import ClienteJuridico
from app.integrations.juridico.entregador import EntregadorJuridico
from app.models import Consultor, Conversa, CrmOutbox, Lead, OptOut, RegraDisponibilidade
from app.outbox import TipoEnvio, enfileirar

AGORA = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
TELEFONE = "+5511987654321"


class Empurrao:
    """Adaptador de push: guarda o que a Marina respondeu."""

    canal = "whatsapp"

    def __init__(self) -> None:
        self.enviadas: list[MensagemSaida] = []

    async def receber(self) -> InboundMessage | None:
        return None

    async def enviar(self, mensagem: MensagemSaida) -> None:
        self.enviadas.append(mensagem)


def _motor(criar_sessao, empresa, crm=None, entregador=None):
    return Engine(
        criar_sessao=criar_sessao,
        adaptador=Empurrao(),
        empresa=empresa,
        relogio=lambda: AGORA,
        crm=crm,
        entregador=entregador,
    )


def txt(valor: str) -> InboundMessage:
    return InboundMessage(texto=valor)


async def _consultor(sessao, empresa, nome="Ana Ribeiro"):
    consultor = Consultor(
        empresa_id=empresa.id,
        nome=nome,
        email=f"{nome.split()[0].lower()}@{empresa.slug}.com.br",
        ativo=True,
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
    return consultor


# --- cadastro e conversa ---------------------------------------------------


@pytest.mark.asyncio
async def test_o_mesmo_telefone_em_duas_empresas_gera_dois_cadastros(
    sessao_db, criar_sessao_db, empresa, outra_empresa
):
    """Cada escritório tem o seu cadastro, com histórico que não se mistura."""
    for alvo in (empresa, outra_empresa):
        motor = _motor(criar_sessao_db, alvo)
        await motor.atender(TELEFONE, txt("oi"))
        await motor.atender(TELEFONE, txt("Pedro Guerra"))
        await motor.atender(TELEFONE, txt("11 98765-4321"))

    leads = (await sessao_db.execute(select(Lead))).scalars().all()
    assert len(leads) == 2
    assert {lead.empresa_id for lead in leads} == {empresa.id, outra_empresa.id}


@pytest.mark.asyncio
async def test_a_pessoa_pode_ter_conversa_aberta_com_cada_empresa(
    sessao_db, criar_sessao_db, empresa, outra_empresa
):
    """Antes, a trava de "uma conversa aberta por número" era global."""
    primeiro = _motor(criar_sessao_db, empresa)
    segundo = _motor(criar_sessao_db, outra_empresa)

    await primeiro.atender(TELEFONE, txt("oi"))
    await segundo.atender(TELEFONE, txt("oi"))

    abertas = (
        (await sessao_db.execute(select(Conversa).where(Conversa.encerrada_em.is_(None))))
        .scalars()
        .all()
    )
    assert len(abertas) == 2
    assert {c.empresa_id for c in abertas} == {empresa.id, outra_empresa.id}


@pytest.mark.asyncio
async def test_responder_a_uma_empresa_nao_avanca_a_conversa_da_outra(
    sessao_db, criar_sessao_db, empresa, outra_empresa
):
    primeiro = _motor(criar_sessao_db, empresa)
    segundo = _motor(criar_sessao_db, outra_empresa)
    await primeiro.atender(TELEFONE, txt("oi"))
    await segundo.atender(TELEFONE, txt("oi"))

    # Só a primeira recebe o nome.
    await primeiro.atender(TELEFONE, txt("Pedro Guerra"))

    conversas = {
        c.empresa_id: c
        for c in (await sessao_db.execute(select(Conversa))).scalars().all()
    }
    assert conversas[empresa.id].estado == Estado.AGUARDANDO_TELEFONE.value
    assert conversas[outra_empresa.id].estado == Estado.AGUARDANDO_NOME.value


# --- descadastro ------------------------------------------------------------


@pytest.mark.asyncio
async def test_sair_de_uma_empresa_nao_bloqueia_a_outra(
    sessao_db, criar_sessao_db, empresa, outra_empresa
):
    """Descadastrar é uma decisão sobre AQUELE escritório, não sobre todos."""
    primeiro = _motor(criar_sessao_db, empresa)
    await primeiro.atender(TELEFONE, txt("oi"))
    await primeiro.atender(TELEFONE, txt("Pedro Guerra"))
    await primeiro.atender(TELEFONE, txt("11 98765-4321"))
    await primeiro.atender(TELEFONE, txt("sair"))

    registros = (await sessao_db.execute(select(OptOut))).scalars().all()
    assert len(registros) == 1
    assert registros[0].empresa_id == empresa.id

    # A outra empresa atende normalmente: sem retomada de opt-out no caminho.
    segundo = _motor(criar_sessao_db, outra_empresa)
    await segundo.atender(TELEFONE, txt("oi"))
    await segundo.atender(TELEFONE, txt("Pedro Guerra"))
    await segundo.atender(TELEFONE, txt("11 98765-4321"))

    conversa = (
        await sessao_db.execute(
            select(Conversa).where(Conversa.empresa_id == outra_empresa.id)
        )
    ).scalar_one()
    assert conversa.estado != Estado.AGUARDANDO_RETOMADA_OPT_OUT.value


# --- agenda -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_so_sao_ofertados_horarios_de_profissional_da_empresa(
    sessao_db, criar_sessao_db, empresa, outra_empresa
):
    """O caso que aconteceu de verdade antes desta separação existir."""
    da_empresa = await _consultor(sessao_db, empresa, "Ana Ribeiro")
    await _consultor(sessao_db, outra_empresa, "Bruno Tavares")

    motor = _motor(criar_sessao_db, empresa)
    for entrada in ("oi", "Pedro Guerra", "11 98765-4321", "5", "1", "2"):
        await motor.atender(TELEFONE, txt(entrada))

    conversa = (
        await sessao_db.execute(
            select(Conversa).where(Conversa.empresa_id == empresa.id)
        )
    ).scalar_one()
    ofertados = conversa.contexto.get("slots_ofertados") or []
    assert ofertados, "nenhum horário foi ofertado"

    todos = {uuid.UUID(c) for slot in ofertados for c in slot["consultores"]}
    assert todos == {da_empresa.id}


@pytest.mark.asyncio
async def test_reservar_recusa_profissional_de_outra_empresa(
    sessao_db, empresa, outra_empresa
):
    """A última barreira antes de gravar a reunião.

    O horário escolhido vem do contexto, que veio do banco como JSONB. Mesmo que
    chegue corrompido apontando para outro escritório, a reserva recusa.
    """
    from app.agenda.reserva import reservar

    alheio = await _consultor(sessao_db, outra_empresa, "Bruno Tavares")
    lead = Lead(empresa_id=empresa.id, telefone_e164=TELEFONE, canal="whatsapp")
    sessao_db.add(lead)
    await sessao_db.commit()

    resultado = await reservar(
        sessao_db,
        lead.id,
        alheio.id,
        AGORA + timedelta(days=1),
        AGORA + timedelta(days=1, hours=1),
        empresa.id,
    )
    assert not resultado.ok
    assert resultado.motivo == "consultor inexistente"


# --- entrega ----------------------------------------------------------------


def _entregador(criar_sessao, empresa, chamadas):
    def responder(request: httpx.Request) -> httpx.Response:
        chamadas.append(str(request.url.path))
        return httpx.Response(201, json={"id": 42})

    cliente = ClienteJuridico(
        base_url="https://juridico.exemplo",
        token="token-de-teste",
        transport=httpx.MockTransport(responder),
    )
    return (
        EntregadorJuridico(
            criar_sessao=criar_sessao,
            cliente=cliente,
            empresa_id=empresa.id,
            relogio=lambda: AGORA,
        ),
        cliente,
    )


@pytest.mark.asyncio
async def test_o_entregador_de_uma_empresa_nao_consome_item_da_outra(
    sessao_db, criar_sessao_db, empresa, outra_empresa
):
    """O vazamento mais grave que existia: a fila era um pote comum.

    A busca por pendentes RESERVA as linhas que lê. Sem o filtro por empresa, o
    entregador de um escritório consumia os itens do outro e os postava no CRM
    errado.
    """
    for alvo, telefone in ((empresa, "+5511900000001"), (outra_empresa, "+5511900000002")):
        lead = Lead(empresa_id=alvo.id, telefone_e164=telefone, nome="X", canal="whatsapp")
        sessao_db.add(lead)
        await sessao_db.flush()
        await enfileirar(
            sessao_db,
            TipoEnvio.LEAD,
            {"lead_id": str(lead.id), "nome": "X", "telefone_e164": telefone},
            empresa_id=alvo.id,
            lead_id=lead.id,
            agora=AGORA,
        )
    await sessao_db.commit()

    entregador, cliente = _entregador(criar_sessao_db, empresa, [])
    resumo = await entregador.drenar()
    await cliente.fechar()

    assert resumo["entregues"] == 1

    pendentes_da_outra = await sessao_db.scalar(
        select(func.count())
        .select_from(CrmOutbox)
        .where(CrmOutbox.empresa_id == outra_empresa.id, CrmOutbox.entregue_em.is_(None))
    )
    assert pendentes_da_outra == 1


@pytest.mark.asyncio
async def test_item_com_lead_de_outra_empresa_falha_em_definitivo(
    sessao_db, criar_sessao_db, empresa, outra_empresa
):
    """Chegar aqui é defeito de programação, não indisponibilidade.

    Por isso o erro é permanente: queima as tentativas de uma vez e vira algo que
    uma pessoa olha, em vez de ficar tentando para sempre.
    """
    alheio = Lead(
        empresa_id=outra_empresa.id,
        telefone_e164="+5511900000003",
        nome="X",
        canal="whatsapp",
    )
    sessao_db.add(alheio)
    await sessao_db.flush()
    # Item marcado como da primeira empresa, mas apontando para lead da segunda.
    await enfileirar(
        sessao_db,
        TipoEnvio.LEAD,
        {"lead_id": str(alheio.id), "nome": "X"},
        empresa_id=empresa.id,
        lead_id=alheio.id,
        agora=AGORA,
    )
    await sessao_db.commit()

    entregador, cliente = _entregador(criar_sessao_db, empresa, [])
    resumo = await entregador.drenar()
    await cliente.fechar()

    assert resumo["falhas"] == 1
    item = (await sessao_db.execute(select(CrmOutbox))).scalar_one()
    await sessao_db.refresh(item)
    assert item.tentativas >= 5, "erro permanente precisa queimar as tentativas"
    assert item.entregue_em is None


@pytest.mark.asyncio
async def test_a_fila_registra_para_qual_empresa_o_item_vai(
    sessao_db, criar_sessao_db, empresa
):
    motor = _motor(criar_sessao_db, empresa, crm=CrmSinkOutbox(criar_sessao_db, empresa.id))
    await motor.atender(TELEFONE, txt("oi"))
    await motor.atender(TELEFONE, txt("Pedro Guerra"))
    await motor.atender(TELEFONE, txt("11 98765-4321"))

    itens = (await sessao_db.execute(select(CrmOutbox))).scalars().all()
    assert itens
    assert all(item.empresa_id == empresa.id for item in itens)


# --- a porta dos fundos -----------------------------------------------------


def test_a_configuracao_nao_pode_passar_por_empresa():
    """O acidente que passou em silêncio, e que agora é impossível.

    A `Configuracao` tem, por coincidência, os mesmos nomes de atributo que a
    checagem de integração lê. Sem esta recusa, entregar a configuração no lugar
    da empresa voltava a usar a credencial global — furando pela porta dos fundos
    a regra de que cada empresa entrega no juridico dela.
    """
    from app.config import Configuracao
    from app.integrations.juridico.fabrica import integracao_ativa

    config = Configuracao(
        juridico_ativo=True,
    )
    with pytest.raises(TypeError, match="espera uma empresa"):
        integracao_ativa(config, config)
