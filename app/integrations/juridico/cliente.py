"""Cliente HTTP do juridico: as chamadas que a Marina usa.

Autenticação é um token fixo, emitido uma vez no juridico
(`php artisan integracao:usuario`) e guardado no `.env`. Não há login nem
renovação: nada a expirar no meio de um atendimento, e nada de rate limit de
login para administrar.

O token pertence a um usuário de serviço lá dentro, e é dele que o juridico tira
a empresa e as permissões. Ou seja: quem manda no escopo é o cadastro remoto,
não este arquivo.
"""

from __future__ import annotations

import httpx

from app.logging import obter_logger

logger = obter_logger("juridico.cliente")


class ErroJuridico(Exception):
    """Falha ao falar com o juridico."""

    def __init__(self, mensagem: str, status: int | None = None) -> None:
        super().__init__(mensagem)
        self.status = status

    #: Falhas que MELHORAM sozinhas ou com uma correção de configuração, e por
    #: isso merecem nova tentativa. 401/403 estão aqui de propósito: para um
    #: usuário de serviço, "não autorizado" é problema de configuração — alguém
    #: troca o token no .env e o que estava na fila precisa fluir. Tratar como
    #: permanente matava o lead: uma única recusa queimava as cinco tentativas e
    #: o item ficava morto, sem nem o flush manual recuperar.
    STATUS_RECUPERAVEIS = frozenset({401, 403, 429})

    @property
    def temporario(self) -> bool:
        """Vale tentar de novo? Rede, 5xx, e as recusas que uma correção resolve."""
        return (
            self.status is None
            or self.status >= 500
            or self.status in self.STATUS_RECUPERAVEIS
        )


class ClienteJuridico:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_s,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                # Obrigatório, não cosmético: sem este cabeçalho o Laravel trata
                # a chamada como navegador. Um token inválido viraria 302 para a
                # tela de login e um erro de validação viraria redirecionamento —
                # ambos lidos aqui como resposta definitiva, matando o item na
                # fila em vez de deixá-lo tentar de novo.
                "Accept": "application/json",
            },
        )

    async def fechar(self) -> None:
        await self._http.aclose()

    # --- transporte ---------------------------------------------------------

    @staticmethod
    def _detalhe(resposta: httpx.Response) -> str:
        """Mensagem legível a partir do corpo de erro do Laravel.

        Achatar `errors` importa: é este texto que fica em
        `crm_outbox.ultimo_erro` e que uma pessoa lê no `flush_outbox.py
        --falhados`. "Os dados fornecidos são inválidos" sem dizer qual campo
        não ajuda ninguém a consertar nada.
        """
        try:
            corpo = resposta.json()
        except ValueError:
            return resposta.text[:300]

        if not isinstance(corpo, dict):
            return str(corpo)[:300]

        mensagem = str(corpo.get("message") or "")
        erros = corpo.get("errors")
        if isinstance(erros, dict):
            campos = "; ".join(
                f"{campo}: {' '.join(str(m) for m in mensagens)}"
                if isinstance(mensagens, list)
                else f"{campo}: {mensagens}"
                for campo, mensagens in erros.items()
            )
            if campos:
                mensagem = f"{mensagem} ({campos})" if mensagem else campos

        return (mensagem or resposta.text)[:300]

    async def _requisitar(
        self,
        metodo: str,
        caminho: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        try:
            return await self._http.request(metodo, caminho, json=json, params=params)
        except httpx.HTTPError as erro:
            # Sem status: falha de rede é sempre temporária.
            raise ErroJuridico(f"falha de rede: {erro}") from erro

    # --- operacoes ----------------------------------------------------------

    async def contexto(self) -> dict:
        """Confere o token e diz onde os leads vão cair. Útil como diagnóstico."""
        resposta = await self._requisitar("GET", "/api/integracao/contexto")
        if resposta.status_code != 200:
            raise ErroJuridico(
                f"contexto recusado: {self._detalhe(resposta)}", resposta.status_code
            )
        return resposta.json()

    async def criar_negociacao(self, payload: dict) -> str:
        """Cria (ou recupera) a negociação do lead e devolve o id dela.

        200 e 201 são ambos sucesso, e a diferença importa pouco aqui: 201 é
        negociação nova, 200 é o servidor reconhecendo pela `referencia_externa`
        que este envio já tinha chegado. Nos dois casos o id é o certo.
        """
        resposta = await self._requisitar(
            "POST", "/api/integracao/negociacoes", json=payload
        )
        if resposta.status_code not in (200, 201):
            raise ErroJuridico(
                f"criacao de negociacao recusada: {self._detalhe(resposta)}",
                resposta.status_code,
            )
        corpo = resposta.json()
        identificador = corpo.get("id") if isinstance(corpo, dict) else None
        if not identificador:
            raise ErroJuridico("resposta da negociacao veio sem id")
        return str(identificador)

    async def registrar_observacao(self, negociacao_id: str, conteudo: str) -> None:
        resposta = await self._requisitar(
            "POST",
            f"/api/integracao/negociacoes/{negociacao_id}/observacoes",
            json={"conteudo": conteudo},
        )
        if resposta.status_code not in (200, 201):
            raise ErroJuridico(
                f"observacao recusada: {self._detalhe(resposta)}", resposta.status_code
            )

    async def criar_tarefa(self, negociacao_id: str, payload: dict) -> str:
        """Cria a reunião como tarefa da negociação, em aberto.

        Não mandamos responsável: o juridico usa o usuário do token, e é assim
        que "sem dono" é representado — a tarefa fica parada no nome do serviço
        até alguém com disponibilidade assumir.
        """
        resposta = await self._requisitar(
            "POST",
            f"/api/integracao/negociacoes/{negociacao_id}/tarefas",
            json=payload,
        )
        if resposta.status_code not in (200, 201):
            raise ErroJuridico(
                f"tarefa recusada: {self._detalhe(resposta)}", resposta.status_code
            )
        corpo = resposta.json()
        identificador = corpo.get("id") if isinstance(corpo, dict) else None
        if not identificador:
            raise ErroJuridico("resposta da tarefa veio sem id")
        return str(identificador)

    async def listar_usuarios(self) -> list[dict]:
        """Usuários ativos da empresa no juridico."""
        resposta = await self._requisitar("GET", "/api/integracao/usuarios")
        if resposta.status_code != 200:
            raise ErroJuridico(
                f"nao foi possivel listar usuarios: {self._detalhe(resposta)}",
                resposta.status_code,
            )
        corpo = resposta.json()
        return corpo if isinstance(corpo, list) else corpo.get("items", [])
