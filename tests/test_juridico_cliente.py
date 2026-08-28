"""Cliente do juridico, contra um servidor falso.

Nenhum teste aqui precisa do juridico no ar. O que se verifica é o
comportamento que só apareceria em produção depois de doer: o cliente NÃO pode
fazer login por requisição (o juridico bloqueia em 10 tentativas por 15 min), e
o 400 de duplicidade tem que ser lido como "essa pessoa já existe", não como
falha.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.integrations.juridico.cliente import (
    ClienteJuridico,
    ErroJuridico,
    LeadDuplicado,
)

AGORA = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


#: Id do proprio usuario de servico, como o juridico devolve no login.
USUARIO_ID = "usuario-marina"


class JuridicoFalso:
    """Servidor de mentira que conta quantas vezes cada rota foi chamada."""

    def __init__(self, **rotas):
        self.chamadas: list[tuple[str, str]] = []
        self.rotas = rotas
        self.logins = 0
        self.refreshes = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))

        if caminho == "/api/auth/login":
            self.logins += 1
            resposta = self.rotas.get("login")
            if callable(resposta):
                return resposta(self.logins)
            return httpx.Response(
                200,
                json={
                    "token": f"token-{self.logins}",
                    "refreshToken": f"refresh-{self.logins}",
                    "accessTokenExpiresIn": 900,
                    "id": USUARIO_ID,
                },
            )

        if caminho == "/api/auth/refresh":
            self.refreshes += 1
            resposta = self.rotas.get("refresh")
            if callable(resposta):
                return resposta(self.refreshes)
            return httpx.Response(
                200,
                json={
                    "token": f"token-r{self.refreshes}",
                    "refreshToken": f"refresh-r{self.refreshes}",
                    "accessTokenExpiresIn": 900,
                },
            )

        for chave, resposta in self.rotas.items():
            if chave in ("login", "refresh"):
                continue
            if caminho.startswith(chave):
                return resposta(request) if callable(resposta) else resposta

        return httpx.Response(404, json={"message": "rota nao mapeada no falso"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def montar(falso: JuridicoFalso, relogio=None) -> ClienteJuridico:
    return ClienteJuridico(
        base_url="https://juridico.exemplo",
        login="marina",
        senha="segredo",
        transport=falso.transport(),
        relogio=relogio or (lambda: AGORA),
    )


# --- autenticacao -----------------------------------------------------------


@pytest.mark.asyncio
async def test_faz_login_uma_vez_so_para_varias_chamadas():
    # ESTE e o teste que importa: o juridico bloqueia o login em 10 tentativas
    # por 15 minutos. Login por requisicao derrubaria a Marina no primeiro dia.
    falso = JuridicoFalso(
        **{"/api/admin/leads/responsaveis": httpx.Response(200, json=[])}
    )
    cliente = montar(falso)

    for _ in range(5):
        await cliente.listar_consultores()

    assert falso.logins == 1
    await cliente.fechar()


@pytest.mark.asyncio
async def test_renova_pelo_refresh_em_vez_de_refazer_login():
    falso = JuridicoFalso(
        **{"/api/admin/leads/responsaveis": httpx.Response(200, json=[])}
    )
    momento = {"agora": AGORA}
    cliente = montar(falso, relogio=lambda: momento["agora"])

    await cliente.listar_consultores()
    assert falso.logins == 1

    # Passa do prazo do token de acesso (15 min).
    momento["agora"] = AGORA + timedelta(minutes=20)
    await cliente.listar_consultores()

    assert falso.refreshes == 1
    assert falso.logins == 1  # nao refez login
    await cliente.fechar()


@pytest.mark.asyncio
async def test_refresh_negado_cai_para_login():
    falso = JuridicoFalso(
        refresh=lambda n: httpx.Response(401, json={"message": "sessao expirada"}),
        **{"/api/admin/leads/responsaveis": httpx.Response(200, json=[])},
    )
    momento = {"agora": AGORA}
    cliente = montar(falso, relogio=lambda: momento["agora"])

    await cliente.listar_consultores()
    momento["agora"] = AGORA + timedelta(days=40)
    await cliente.listar_consultores()

    assert falso.logins == 2
    await cliente.fechar()


@pytest.mark.asyncio
async def test_login_bloqueado_vira_erro_temporario():
    falso = JuridicoFalso(
        login=lambda n: httpx.Response(429, json={"message": "Muitas tentativas"})
    )
    cliente = montar(falso)

    with pytest.raises(ErroJuridico) as capturado:
        await cliente.listar_consultores()
    # Temporario = a fila deve tentar de novo mais tarde, nao descartar.
    assert capturado.value.temporario is True
    await cliente.fechar()


@pytest.mark.asyncio
async def test_credencial_errada_e_temporaria():
    """Esta regra foi INVERTIDA em 28/08/2026, e vale registrar os dois lados.

    Antes: 401 era permanente, com o argumento de que insistir com senha errada
    gasta o limite de login do juridico (10 por 15 min, com bloqueio progressivo).

    O que aconteceu na prática: uma única recusa queimava as cinco tentativas de
    uma vez e o lead ficava MORTO na fila — nem corrigir a senha e rodar o flush
    trazia de volta. Perder o lead é pior do que o risco do rate limit.

    E o risco é menor do que parecia: o cliente guarda a sessão, então uma
    rodada de entrega inteira faz **um** login; e o backoff espaça as rodadas até
    30 minutos. Isso fica muito abaixo do limite do juridico.
    """
    falso = JuridicoFalso(
        login=lambda n: httpx.Response(401, json={"message": "Credenciais inválidas"})
    )
    cliente = montar(falso)

    with pytest.raises(ErroJuridico) as capturado:
        await cliente.listar_consultores()
    assert capturado.value.temporario is True
    await cliente.fechar()


@pytest.mark.asyncio
async def test_token_revogado_no_meio_e_refeito_uma_vez():
    estado = {"primeira": True}

    def leads(request):
        if estado["primeira"]:
            estado["primeira"] = False
            return httpx.Response(401, json={"message": "SESSION_REVOKED"})
        return httpx.Response(201, json={"id": "lead-1"})

    falso = JuridicoFalso(**{"/api/admin/leads": leads})
    cliente = montar(falso)

    assert await cliente.criar_lead({"nomeCompleto": "Pedro"}) == "lead-1"
    assert falso.logins == 2
    await cliente.fechar()


# --- leads ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cria_lead():
    falso = JuridicoFalso(
        **{"/api/admin/leads": httpx.Response(201, json={"id": "lead-abc"})}
    )
    cliente = montar(falso)
    assert await cliente.criar_lead({"nomeCompleto": "Pedro"}) == "lead-abc"
    await cliente.fechar()


@pytest.mark.asyncio
async def test_duplicidade_nao_e_falha_e_sim_ja_existe():
    falso = JuridicoFalso(
        **{
            "/api/admin/leads": httpx.Response(
                400, json={"message": "TELEFONE já cadastrado para outro lead."}
            )
        }
    )
    cliente = montar(falso)
    with pytest.raises(LeadDuplicado):
        await cliente.criar_lead({"nomeCompleto": "Pedro"})
    await cliente.fechar()


@pytest.mark.asyncio
async def test_outro_400_continua_sendo_erro_de_verdade():
    falso = JuridicoFalso(
        **{
            "/api/admin/leads": httpx.Response(
                400, json={"message": "Origem do lead invalida."}
            )
        }
    )
    cliente = montar(falso)
    with pytest.raises(ErroJuridico) as capturado:
        await cliente.criar_lead({"nomeCompleto": "Pedro"})
    assert not isinstance(capturado.value, LeadDuplicado)
    assert capturado.value.temporario is False
    await cliente.fechar()


@pytest.mark.asyncio
async def test_busca_lead_casa_pelos_ultimos_oito_digitos():
    # O juridico compara telefone por trecho final. Buscar o numero inteiro
    # erraria por causa de formatacao (parenteses, traco, codigo do pais).
    def leads(request):
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": "outro", "telefoneCelular": "(11) 91111-2222"},
                    {"id": "certo", "telefoneCelular": "(11) 98765-4321"},
                ]
            },
        )

    falso = JuridicoFalso(**{"/api/admin/leads": leads})
    cliente = montar(falso)
    assert await cliente.buscar_lead_por_telefone("11987654321") == "certo"
    await cliente.fechar()


@pytest.mark.asyncio
async def test_busca_sem_resultado_devolve_nada():
    falso = JuridicoFalso(**{"/api/admin/leads": httpx.Response(200, json={"items": []})})
    cliente = montar(falso)
    assert await cliente.buscar_lead_por_telefone("11987654321") is None
    await cliente.fechar()


@pytest.mark.asyncio
async def test_primeiro_contato_e_tarefa_em_aberto():
    # A reuniao vai como TAREFA de CRM, nao como agendamento com dono.
    falso = JuridicoFalso(
        **{
            "/api/admin/leads/lead-1/primeiro-contato": httpx.Response(200, json={"id": "lead-1"}),
            "/api/admin/crm/tarefas": httpx.Response(201, json={"id": "tarefa-1"}),
        }
    )
    cliente = montar(falso)
    await cliente.registrar_primeiro_contato("lead-1", "qualificação", canal="WHATSAPP")
    assert await cliente.criar_tarefa({"assunto": "x"}) == "tarefa-1"
    await cliente.fechar()


@pytest.mark.asyncio
async def test_login_guarda_o_proprio_usuario():
    # Sem saber o proprio id, a Marina nao consegue criar a tarefa: o juridico
    # exige responsavel, e "em aberto" e justamente a tarefa parada no nome dela.
    falso = JuridicoFalso()
    cliente = montar(falso)
    assert await cliente.usuario_id() == USUARIO_ID
    await cliente.fechar()


@pytest.mark.asyncio
async def test_juridico_fora_do_ar_e_temporario():
    def cai(request):
        raise httpx.ConnectError("connection refused")

    falso = JuridicoFalso(**{"/api/admin/leads": cai})
    cliente = montar(falso)
    with pytest.raises(ErroJuridico) as capturado:
        await cliente.criar_lead({"nomeCompleto": "Pedro"})
    assert capturado.value.temporario is True
    await cliente.fechar()


@pytest.mark.asyncio
async def test_erro_500_e_temporario():
    falso = JuridicoFalso(**{"/api/admin/leads": httpx.Response(500, text="boom")})
    cliente = montar(falso)
    with pytest.raises(ErroJuridico) as capturado:
        await cliente.criar_lead({"nomeCompleto": "Pedro"})
    assert capturado.value.temporario is True
    await cliente.fechar()


def test_credencial_recusada_e_recuperavel():
    """Regressao de um caso real: o lead do Pedro ficou morto na fila.

    Um unico 401 marcava o item como falha PERMANENTE, o que queimava as cinco
    tentativas de uma vez. O lead nunca chegava ao CRM e nem o flush manual
    trazia de volta — mesmo depois de a credencial ser corrigida.
    """
    assert ErroJuridico("login recusado", 401).temporario
    assert ErroJuridico("sem permissao", 403).temporario
    assert ErroJuridico("bloqueado", 429).temporario
    assert ErroJuridico("indisponivel", 503).temporario
    assert ErroJuridico("conexao caiu", None).temporario
    # Um payload que o juridico recusa nao melhora com repeticao: continua
    # queimando as tentativas para virar algo que uma pessoa olha.
    assert not ErroJuridico("origem invalida", 400).temporario
    assert not ErroJuridico("nao encontrado", 404).temporario
