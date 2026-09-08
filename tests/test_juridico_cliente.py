"""Cliente HTTP do juridico, contra um servidor de mentira.

O que se testa aqui é o transporte: cabeçalhos, leitura da resposta e — o que
mais importa — a classificação entre falha que melhora sozinha e falha que não
melhora. Essa classificação decide se um lead volta para a fila ou morre nela.
"""

import httpx
import pytest

from app.integrations.juridico.cliente import ClienteJuridico, ErroJuridico

BASE = "https://juridico.exemplo"
TOKEN = "token-de-servico"


def montar(handler) -> ClienteJuridico:
    return ClienteJuridico(
        base_url=BASE, token=TOKEN, transport=httpx.MockTransport(handler)
    )


def responder(status: int, corpo=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=corpo if corpo is not None else {})

    return handler


# --- cabecalhos -------------------------------------------------------------


async def test_toda_requisicao_leva_token_e_pede_json():
    vistos = {}

    def handler(request: httpx.Request) -> httpx.Response:
        vistos.update(request.headers)
        return httpx.Response(201, json={"id": 42})

    cliente = montar(handler)
    await cliente.criar_negociacao({"nome": "Pedro"})
    await cliente.fechar()

    assert vistos["authorization"] == f"Bearer {TOKEN}"
    # Sem este cabecalho o Laravel responde 302 para a tela de login em vez de
    # 401, e erro de validacao vira redirecionamento em vez de 422 — os dois
    # seriam lidos como permanentes e matariam o item na fila.
    assert vistos["accept"] == "application/json"


# --- negociacao -------------------------------------------------------------


async def test_criar_negociacao_devolve_o_id():
    cliente = montar(responder(201, {"id": 42, "criada": True}))
    assert await cliente.criar_negociacao({"nome": "Pedro"}) == "42"
    await cliente.fechar()


async def test_200_tambem_e_sucesso():
    # 200 e o servidor reconhecendo pela referencia_externa que este envio ja
    # havia chegado. O id devolvido e o mesmo, e reenviar nao duplicou nada.
    cliente = montar(responder(200, {"id": 42, "criada": False}))
    assert await cliente.criar_negociacao({"nome": "Pedro"}) == "42"
    await cliente.fechar()


async def test_resposta_sem_id_e_erro_temporario():
    cliente = montar(responder(201, {"criada": True}))
    with pytest.raises(ErroJuridico) as erro:
        await cliente.criar_negociacao({})
    await cliente.fechar()
    assert erro.value.temporario


async def test_caminhos_das_chamadas():
    caminhos: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        caminhos.append((request.method, request.url.path))
        return httpx.Response(201, json={"id": 7})

    cliente = montar(handler)
    await cliente.criar_negociacao({})
    await cliente.registrar_observacao("42", "qualificado")
    await cliente.criar_tarefa("42", {"assunto": "Reunião"})
    await cliente.fechar()

    assert caminhos == [
        ("POST", "/api/integracao/negociacoes"),
        ("POST", "/api/integracao/negociacoes/42/observacoes"),
        ("POST", "/api/integracao/negociacoes/42/tarefas"),
    ]


# --- classificacao dos erros ------------------------------------------------


async def test_validacao_recusada_e_permanente_e_diz_o_campo():
    # 422 nao melhora com repeticao: o payload esta errado. E quem for olhar a
    # fila precisa ler qual campo, senao nao tem o que consertar.
    corpo = {
        "message": "The given data was invalid.",
        "errors": {"fonte": ["The selected fonte is invalid."]},
    }
    cliente = montar(responder(422, corpo))
    with pytest.raises(ErroJuridico) as erro:
        await cliente.criar_negociacao({})
    await cliente.fechar()

    assert erro.value.temporario is False
    assert "fonte" in str(erro.value)
    assert "invalid" in str(erro.value)


async def test_token_recusado_e_temporario():
    # Um token trocado e problema de configuracao: alguem corrige o .env e o que
    # esta na fila precisa fluir. Tratar como permanente queimava as cinco
    # tentativas e matava o lead.
    cliente = montar(responder(401, {"message": "Unauthenticated."}))
    with pytest.raises(ErroJuridico) as erro:
        await cliente.criar_negociacao({})
    await cliente.fechar()
    assert erro.value.temporario


async def test_sem_permissao_e_temporario():
    cliente = montar(responder(403, {"message": "Sem permissão."}))
    with pytest.raises(ErroJuridico) as erro:
        await cliente.criar_negociacao({})
    await cliente.fechar()
    assert erro.value.temporario


async def test_negociacao_inexistente_e_permanente():
    cliente = montar(responder(404, {"message": "Não encontrado."}))
    with pytest.raises(ErroJuridico) as erro:
        await cliente.registrar_observacao("999", "texto")
    await cliente.fechar()
    assert erro.value.temporario is False


async def test_erro_do_servidor_e_temporario():
    cliente = montar(responder(500, {"message": "Server Error"}))
    with pytest.raises(ErroJuridico) as erro:
        await cliente.criar_tarefa("42", {})
    await cliente.fechar()
    assert erro.value.temporario


async def test_rede_caida_e_temporario():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    cliente = montar(handler)
    with pytest.raises(ErroJuridico) as erro:
        await cliente.criar_negociacao({})
    await cliente.fechar()

    assert erro.value.status is None
    assert erro.value.temporario


async def test_corpo_que_nao_e_json_ainda_vira_mensagem():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="<html>gateway</html>")

    cliente = montar(handler)
    with pytest.raises(ErroJuridico) as erro:
        await cliente.criar_negociacao({})
    await cliente.fechar()
    assert "gateway" in str(erro.value)


def test_tabela_de_recuperabilidade():
    # A regressao de 28/08/2026: 401 e 403 sao recuperaveis de proposito.
    assert ErroJuridico("x", status=None).temporario
    assert ErroJuridico("x", status=500).temporario
    assert ErroJuridico("x", status=401).temporario
    assert ErroJuridico("x", status=403).temporario
    assert ErroJuridico("x", status=429).temporario
    assert ErroJuridico("x", status=400).temporario is False
    assert ErroJuridico("x", status=404).temporario is False
    assert ErroJuridico("x", status=422).temporario is False


# --- usuarios ---------------------------------------------------------------


async def test_lista_usuarios():
    cliente = montar(responder(200, {"items": [{"id": 1, "nome": "Ana"}]}))
    usuarios = await cliente.listar_usuarios()
    await cliente.fechar()
    assert usuarios == [{"id": 1, "nome": "Ana"}]


async def test_contexto():
    cliente = montar(responder(200, {"funil_venda_id": 3}))
    assert (await cliente.contexto())["funil_venda_id"] == 3
    await cliente.fechar()
