"""Cliente HTTP do juridico: autenticação e as chamadas que a Marina usa.

O detalhe que governa o desenho da autenticação: **o juridico limita o login a
10 tentativas por 15 minutos, com bloqueio progressivo depois de falhas.** Um
cliente que fizesse login por requisição seria bloqueado no primeiro dia de uso
real e ficaria fora do ar por minutos.

Por isso: login uma vez, token guardado em memória, renovação pelo refreshToken
(o token de acesso dura 15 minutos, a sessão dura 30 dias). Login só volta a
acontecer se a renovação também falhar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app.logging import obter_logger

logger = obter_logger("juridico.cliente")

#: Renovar um pouco antes de expirar evita a corrida entre "token ainda vale" e
#: "token expirou no meio da requisição".
FOLGA_RENOVACAO = timedelta(seconds=60)


class ErroJuridico(Exception):
    """Falha ao falar com o juridico."""

    def __init__(self, mensagem: str, status: int | None = None) -> None:
        super().__init__(mensagem)
        self.status = status

    #: Falhas que MELHORAM sozinhas ou com uma correção de configuração, e por
    #: isso merecem nova tentativa. 401/403 estão aqui de propósito: para um
    #: usuário de serviço, "credenciais inválidas" é problema de configuração —
    #: alguém troca a senha no .env e o que estava na fila precisa fluir. Tratar
    #: como permanente matava o lead: uma única recusa queimava as cinco
    #: tentativas e o item ficava morto, sem nem o flush manual recuperar.
    STATUS_RECUPERAVEIS = frozenset({401, 403, 429})

    @property
    def temporario(self) -> bool:
        """Vale tentar de novo? Rede, 5xx, e as recusas que uma correção resolve."""
        return (
            self.status is None
            or self.status >= 500
            or self.status in self.STATUS_RECUPERAVEIS
        )


class LeadDuplicado(ErroJuridico):
    """O juridico recusou o lead porque já existe alguém com aquele contato.

    Não é erro: é a resposta que diz "essa pessoa já está aqui". O juridico
    devolve 400 e NÃO devolve o registro existente, então quem chama precisa ir
    buscar o lead pelo telefone.
    """


@dataclass
class Sessao:
    token: str
    refresh_token: str
    expira_em: datetime
    #: Id do proprio usuario de serviço no juridico. A tarefa da reunião nasce
    #: no nome dele — é assim que "em aberto" é representado, já que o juridico
    #: exige responsável em toda tarefa.
    usuario_id: str | None = None

    def valido(self, agora: datetime) -> bool:
        return agora + FOLGA_RENOVACAO < self.expira_em


class ClienteJuridico:
    def __init__(
        self,
        base_url: str,
        login: str,
        senha: str,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        relogio=None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._login = login
        self._senha = senha
        self._sessao: Sessao | None = None
        self._relogio = relogio or (lambda: datetime.now(UTC))
        self._http = httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout_s, transport=transport
        )

    async def fechar(self) -> None:
        await self._http.aclose()

    # --- autenticacao -------------------------------------------------------

    async def _autenticar(self) -> Sessao:
        resposta = await self._http.post(
            "/api/auth/login", json={"login": self._login, "senha": self._senha}
        )
        if resposta.status_code == 429:
            raise ErroJuridico(
                "login bloqueado por excesso de tentativas", resposta.status_code
            )
        if resposta.status_code != 200:
            raise ErroJuridico(
                f"login recusado: {self._detalhe(resposta)}", resposta.status_code
            )
        return self._sessao_da_resposta(resposta.json())

    async def _renovar(self, refresh_token: str) -> Sessao | None:
        resposta = await self._http.post(
            "/api/auth/refresh", json={"refreshToken": refresh_token}
        )
        if resposta.status_code != 200:
            return None
        return self._sessao_da_resposta(resposta.json())

    def _sessao_da_resposta(self, corpo: dict) -> Sessao:
        token = corpo.get("token")
        if not token:
            raise ErroJuridico("resposta de autenticação sem token")
        # `accessTokenExpiresIn` vem em segundos. Se faltar, assume o padrao do
        # juridico (15 min) em vez de tratar o token como eterno.
        segundos = int(corpo.get("accessTokenExpiresIn") or 900)
        usuario_id = corpo.get("id")
        return Sessao(
            token=token,
            refresh_token=str(corpo.get("refreshToken") or ""),
            expira_em=self._relogio() + timedelta(seconds=segundos),
            # O refresh nao repete o id; preserva o que o login trouxe.
            usuario_id=str(usuario_id)
            if usuario_id
            else (self._sessao.usuario_id if self._sessao else None),
        )

    async def usuario_id(self) -> str | None:
        """Id do usuário de serviço, garantindo que há sessão."""
        await self._token()
        return self._sessao.usuario_id if self._sessao else None

    async def _token(self) -> str:
        agora = self._relogio()
        if self._sessao and self._sessao.valido(agora):
            return self._sessao.token

        if self._sessao and self._sessao.refresh_token:
            renovada = await self._renovar(self._sessao.refresh_token)
            if renovada is not None:
                self._sessao = renovada
                return renovada.token
            logger.info("juridico_refresh_falhou_refazendo_login")

        self._sessao = await self._autenticar()
        return self._sessao.token

    # --- requisicao ---------------------------------------------------------

    @staticmethod
    def _detalhe(resposta: httpx.Response) -> str:
        try:
            return str(resposta.json().get("message") or resposta.text)[:300]
        except ValueError:
            return resposta.text[:300]

    async def _requisitar(
        self, metodo: str, caminho: str, *, json: dict | None = None, params: dict | None = None
    ) -> httpx.Response:
        token = await self._token()
        cabecalhos = {"Authorization": f"Bearer {token}"}
        try:
            resposta = await self._http.request(
                metodo, caminho, json=json, params=params, headers=cabecalhos
            )
        except httpx.HTTPError as erro:
            raise ErroJuridico(f"falha de rede: {erro}") from erro

        if resposta.status_code == 401:
            # Sessão revogada do outro lado. Uma segunda chance com token novo,
            # e só uma: repetir em laço viraria martelo no rate limit do login.
            self._sessao = None
            token = await self._token()
            resposta = await self._http.request(
                metodo, caminho, json=json, params=params, headers={"Authorization": f"Bearer {token}"}
            )
        return resposta

    # --- operacoes ----------------------------------------------------------

    async def listar_consultores(self, filial_id: str | None = None) -> list[dict]:
        """Consultores elegíveis — a mesma fonte que alimenta o rodízio do juridico."""
        params = {"filialId": filial_id} if filial_id else None
        resposta = await self._requisitar(
            "GET", "/api/admin/leads/responsaveis", params=params
        )
        if resposta.status_code != 200:
            raise ErroJuridico(
                f"nao foi possivel listar consultores: {self._detalhe(resposta)}",
                resposta.status_code,
            )
        corpo = resposta.json()
        return corpo if isinstance(corpo, list) else corpo.get("items", [])

    async def criar_lead(self, payload: dict) -> str:
        resposta = await self._requisitar("POST", "/api/admin/leads", json=payload)
        if resposta.status_code in (200, 201):
            return str(resposta.json()["id"])
        detalhe = self._detalhe(resposta)
        if resposta.status_code == 400 and "já cadastrado" in detalhe:
            raise LeadDuplicado(detalhe, resposta.status_code)
        raise ErroJuridico(f"criacao de lead recusada: {detalhe}", resposta.status_code)

    async def buscar_lead_por_telefone(self, telefone: str) -> str | None:
        """Usado quando a criação bate na deduplicação do juridico.

        O juridico compara telefone pelos últimos 8 dígitos, então a busca usa o
        mesmo trecho — buscar pelo número inteiro erraria por causa de formatação.
        """
        if not telefone:
            return None
        resposta = await self._requisitar(
            "GET", "/api/admin/leads", params={"q": telefone[-8:]}
        )
        if resposta.status_code != 200:
            raise ErroJuridico(
                f"busca de lead falhou: {self._detalhe(resposta)}", resposta.status_code
            )
        itens = resposta.json().get("items") or []
        alvo = "".join(c for c in telefone if c.isdigit())[-8:]
        for item in itens:
            registrado = "".join(c for c in str(item.get("telefoneCelular") or "") if c.isdigit())
            if registrado.endswith(alvo):
                return str(item["id"])
        return None

    async def registrar_primeiro_contato(
        self, lead_id: str, observacao: str, canal: str = "WHATSAPP"
    ) -> None:
        resposta = await self._requisitar(
            "POST",
            f"/api/admin/leads/{lead_id}/primeiro-contato",
            json={"canal": canal, "observacao": observacao},
        )
        if resposta.status_code not in (200, 201):
            raise ErroJuridico(
                f"primeiro contato recusado: {self._detalhe(resposta)}",
                resposta.status_code,
            )

    async def criar_tarefa(self, payload: dict) -> str:
        """Cria a reunião como tarefa de CRM, em aberto.

        Não é agendamento com dono: a tarefa nasce no nome do próprio usuário de
        serviço e fica esperando quem tiver disponibilidade pegar. Ela aparece no
        calendário do juridico com a etiqueta CRM, como qualquer outra.
        """
        resposta = await self._requisitar(
            "POST", "/api/admin/crm/tarefas", json=payload
        )
        if resposta.status_code not in (200, 201):
            raise ErroJuridico(
                f"tarefa recusada: {self._detalhe(resposta)}", resposta.status_code
            )
        return str(resposta.json()["id"])
