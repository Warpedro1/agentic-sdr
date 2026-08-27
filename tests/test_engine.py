"""O engine, contra o banco de verdade.

Aqui nao se testa decisao — isso e de test_machine.py, sem banco. Testa-se o que
so o engine faz: persistir, executar efeitos, reentrar nos estados internos e
registrar a transcricao.
"""

from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.channels.base import Anexo, InboundMessage
from app.channels.terminal import ScriptedAdapter
from app.engine import Engine
from app.enums import Direcao, Estado
from app.models import Agendamento, Consultor, Conversa, Documento, Lead, Mensagem, OptOut
from app.storage.local import LocalDocumentStorage

# Quarta-feira, 9h em Sao Paulo. Fixo para os horarios ofertados nao mudarem a
# cada execucao.
AGORA = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def txt(valor):
    return InboundMessage(texto=valor)


async def semear_consultores(sessao, quantos=1):
    nomes = ["Ana Ribeiro", "Bruno Tavares", "Carla Monteiro"][:quantos]
    for nome in nomes:
        consultor = Consultor(
            nome=nome, email=f"{nome.split()[0].lower()}@exemplo.com.br", ativo=True
        )
        sessao.add(consultor)
        await sessao.flush()
        for dia in range(0, 5):
            from app.models import RegraDisponibilidade

            sessao.add(
                RegraDisponibilidade(
                    consultor_id=consultor.id,
                    dia_semana=dia,
                    hora_inicio=time(9, 0),
                    hora_fim=time(18, 0),
                    duracao_min=60,
                    buffer_min=15,
                )
            )
    await sessao.commit()


def montar_engine(criar_sessao, entradas, armazenamento=None, agora=AGORA):
    adaptador = ScriptedAdapter(entradas)
    motor = Engine(
        criar_sessao=criar_sessao,
        adaptador=adaptador,
        armazenamento=armazenamento,
        relogio=lambda: agora,
    )
    return motor, adaptador


CAMINHO_FELIZ = [
    txt("Pedro Guerra"),
    txt("11 98765-4321"),
    txt("pedro@exemplo.com.br"),
    txt("5"),
    txt("1"),
    txt("2"),
    txt("1"),
    txt("1"),
]


@pytest.mark.asyncio
async def test_caminho_feliz_agenda_e_grava_tudo(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db)
    motor, adaptador = montar_engine(criar_sessao_db, CAMINHO_FELIZ)

    conversa_id = await motor.rodar()

    conversa = await sessao_db.get(Conversa, conversa_id)
    assert conversa.estado == Estado.AGENDADO.value
    assert conversa.encerrada_em is not None

    lead = (
        await sessao_db.execute(
            select(Lead).where(Lead.telefone_e164 == "+5511987654321")
        )
    ).scalar_one()
    assert lead.nome == "Pedro Guerra"
    assert lead.email == "pedro@exemplo.com.br"
    assert lead.fonte == "indicacao"
    assert lead.tipo_contrato == "consignado"
    assert lead.possui_contrato is False
    # Consentimento carimbado na saudacao, nao na criacao da linha.
    assert lead.consentimento_em == AGORA

    agendamento = (await sessao_db.execute(select(Agendamento))).scalar_one()
    assert agendamento.lead_id == lead.id
    assert agendamento.status == "agendado"

    assert adaptador.enviadas[-1].template_key == "agendado"


@pytest.mark.asyncio
async def test_lead_so_nasce_depois_do_telefone(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db)
    # Para depois do nome: nenhum lead deve existir ainda.
    motor, _ = montar_engine(criar_sessao_db, [txt("Pedro Guerra")])
    await motor.rodar()

    leads = (await sessao_db.execute(select(Lead))).scalars().all()
    assert leads == []

    conversa = (await sessao_db.execute(select(Conversa))).scalar_one()
    assert conversa.lead_id is None  # a conversa existe antes do lead
    assert conversa.contexto["nome"] == "Pedro Guerra"


@pytest.mark.asyncio
async def test_transcricao_registra_entrada_e_saida(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db)
    motor, _ = montar_engine(criar_sessao_db, CAMINHO_FELIZ)
    conversa_id = await motor.rodar()

    mensagens = (
        (
            await sessao_db.execute(
                select(Mensagem)
                .where(Mensagem.conversa_id == conversa_id)
                .order_by(Mensagem.criada_em, Mensagem.id)
            )
        )
        .scalars()
        .all()
    )

    entradas = [m for m in mensagens if m.direcao == Direcao.ENTRADA.value]
    saidas = [m for m in mensagens if m.direcao == Direcao.SAIDA.value]

    # Toda mensagem do lead esta la...
    assert [m.conteudo for m in entradas] == [e.texto for e in CAMINHO_FELIZ]
    # ...e toda resposta da Marina tambem, com o template que a produziu.
    assert saidas
    assert all(m.template_key for m in saidas)
    assert "boas_vindas" in [m.template_key for m in saidas]
    assert "agendado" in [m.template_key for m in saidas]


@pytest.mark.asyncio
async def test_contexto_persistido_nao_leva_lixo_transitorio(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db)
    motor, _ = montar_engine(criar_sessao_db, CAMINHO_FELIZ)
    conversa_id = await motor.rodar()

    conversa = await sessao_db.get(Conversa, conversa_id)
    # Resultado de I/O recem-feito nao vai para o JSONB: persistir daria a
    # impressao de que continua valido depois.
    for chave in ("agora", "politicas", "reconhecimento", "reserva"):
        assert chave not in conversa.contexto


@pytest.mark.asyncio
async def test_voltar_funciona_ponta_a_ponta(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db)
    motor, adaptador = montar_engine(
        criar_sessao_db,
        [
            txt("Pedro Guerra"),
            txt("11 98765-4321"),
            txt("pedro@exemplo.com.br"),
            txt("voltar"),
        ],
    )
    await motor.rodar()
    # O engine e quem mantem estado_anterior; a maquina so consulta.
    assert adaptador.enviadas[-1].template_key == "pergunta_email"


@pytest.mark.asyncio
async def test_sair_registra_opt_out(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db)
    motor, adaptador = montar_engine(
        criar_sessao_db, [txt("Pedro Guerra"), txt("11 98765-4321"), txt("sair")]
    )
    await motor.rodar()

    registro = (await sessao_db.execute(select(OptOut))).scalar_one()
    assert registro.telefone_e164 == "+5511987654321"
    assert registro.revogado_em is None
    assert adaptador.enviadas[-1].template_key == "opt_out"


@pytest.mark.asyncio
async def test_sair_antes_do_telefone_nao_suja_a_lista(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db)
    motor, _ = montar_engine(criar_sessao_db, [txt("sair")])
    await motor.rodar()
    assert (await sessao_db.execute(select(OptOut))).scalars().all() == []


@pytest.mark.asyncio
async def test_lead_que_volta_e_reconhecido(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db)

    primeiro, _ = montar_engine(
        criar_sessao_db, [txt("Pedro Guerra"), txt("11 98765-4321"), txt("sair")]
    )
    await primeiro.rodar()

    # Volta depois do SAIR: precisa confirmar antes de continuar.
    segundo, adaptador = montar_engine(
        criar_sessao_db,
        [txt("Pedro Guerra"), txt("11 98765-4321")],
        agora=AGORA + timedelta(hours=1),
    )
    await segundo.rodar()
    assert adaptador.enviadas[-1].template_key == "retomada_opt_out"

    # So existe UM lead: a deduplicacao e por telefone.
    assert len((await sessao_db.execute(select(Lead))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_retomar_opt_out_revoga_o_registro(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db)
    primeiro, _ = montar_engine(
        criar_sessao_db, [txt("Pedro Guerra"), txt("11 98765-4321"), txt("sair")]
    )
    await primeiro.rodar()

    segundo, _ = montar_engine(
        criar_sessao_db,
        [txt("Pedro Guerra"), txt("11 98765-4321"), txt("sim")],
        agora=AGORA + timedelta(hours=1),
    )
    await segundo.rodar()

    registro = (await sessao_db.execute(select(OptOut))).scalar_one()
    assert registro.revogado_em is not None


@pytest.mark.asyncio
async def test_nome_divergente_nao_sobrescreve_o_cadastro(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db)
    primeiro, _ = montar_engine(
        criar_sessao_db, [txt("Pedro Guerra"), txt("11 98765-4321"), txt("sair")]
    )
    await primeiro.rodar()

    segundo, _ = montar_engine(
        criar_sessao_db,
        [txt("Pedr Guera"), txt("11 98765-4321"), txt("nao")],
        agora=AGORA + timedelta(hours=1),
    )
    conversa_id = await segundo.rodar()

    lead = (await sessao_db.execute(select(Lead))).scalar_one()
    assert lead.nome == "Pedro Guerra"  # o cadastro vence

    conversa = await sessao_db.get(Conversa, conversa_id)
    assert conversa.contexto["nome_informado_divergente"] == "Pedr Guera"


@pytest.mark.asyncio
async def test_documento_e_arquivado_e_registrado(
    sessao_db, criar_sessao_db, tmp_path, monkeypatch
):
    await semear_consultores(sessao_db)

    origem = tmp_path / "contrato.pdf"
    origem.write_bytes(b"%PDF-1.4 conteudo de teste")

    from app.config import Configuracao

    config = Configuracao(storage_dir=str(tmp_path / "guardados"))
    armazenamento = LocalDocumentStorage(config)

    motor, adaptador = montar_engine(
        criar_sessao_db,
        [
            txt("Pedro Guerra"),
            txt("11 98765-4321"),
            txt("pedro@exemplo.com.br"),
            txt("5"),
            txt("1"),
            txt("1"),  # tenho o contrato
            InboundMessage(
                anexo=Anexo("contrato.pdf", "application/pdf", origem.stat().st_size, str(origem))
            ),
            txt("1"),
            txt("1"),
        ],
        armazenamento=armazenamento,
    )
    await motor.rodar()

    documento = (await sessao_db.execute(select(Documento))).scalar_one()
    assert documento.mime == "application/pdf"
    assert documento.tamanho_bytes == origem.stat().st_size

    from pathlib import Path

    guardado = Path(documento.caminho)
    assert guardado.is_file()
    # O arquivo original nao e movido nem alterado.
    assert origem.is_file()
    assert guardado.read_bytes() == origem.read_bytes()

    assert "contrato_recebido" in [m.template_key for m in adaptador.enviadas]


@pytest.mark.asyncio
async def test_sem_consultor_nenhum_escala_para_humano(sessao_db, criar_sessao_db):
    # Nenhum consultor semeado: nao ha horario possivel.
    motor, adaptador = montar_engine(
        criar_sessao_db,
        [
            txt("Pedro Guerra"),
            txt("11 98765-4321"),
            txt("pedro@exemplo.com.br"),
            txt("5"),
            txt("1"),
            txt("2"),
        ],
    )
    conversa_id = await motor.rodar()

    conversa = await sessao_db.get(Conversa, conversa_id)
    assert conversa.estado == Estado.ESCALADO_HUMANO.value
    assert adaptador.enviadas[-1].template_key == "sem_horarios"


@pytest.mark.asyncio
async def test_horario_ofertado_respeita_antecedencia(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db)
    motor, adaptador = montar_engine(
        criar_sessao_db,
        [
            txt("Pedro Guerra"),
            txt("11 98765-4321"),
            txt("pedro@exemplo.com.br"),
            txt("5"),
            txt("1"),
            txt("2"),
        ],
    )
    conversa_id = await motor.rodar()

    conversa = await sessao_db.get(Conversa, conversa_id)
    slots = conversa.contexto["slots_ofertados"]
    assert slots
    assert len(slots) <= 5
    for slot in slots:
        assert datetime.fromisoformat(slot["inicio_utc"]) >= AGORA + timedelta(hours=2)


@pytest.mark.asyncio
async def test_slots_distribuem_entre_consultores(sessao_db, criar_sessao_db):
    await semear_consultores(sessao_db, quantos=3)
    motor, _ = montar_engine(
        criar_sessao_db,
        [
            txt("Pedro Guerra"),
            txt("11 98765-4321"),
            txt("pedro@exemplo.com.br"),
            txt("5"),
            txt("1"),
            txt("2"),
        ],
    )
    conversa_id = await motor.rodar()
    conversa = await sessao_db.get(Conversa, conversa_id)
    slots = conversa.contexto["slots_ofertados"]
    # O lead escolhe um HORARIO; cada horario ja vem com um consultor resolvido.
    assert all(s["consultor_nome"] for s in slots)
    assert len({s["inicio_utc"] for s in slots}) == len(slots)
