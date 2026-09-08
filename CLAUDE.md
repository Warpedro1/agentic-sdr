# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Marina — automação de SDR determinística da Leve. Código, domínio e documentação
em português (`maquina`, `efeito`, `consultor`, `agendamento`); mantenha a
convenção ao criar código novo.

## Comandos

Python 3.12+, dependências em `pyproject.toml` (não há requirements.txt). O venv
do projeto é `.venv/` — no Windows os executáveis ficam em `.venv/Scripts/`.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"    # Linux/macOS: .venv/bin/python

docker compose up -d          # Postgres 16 na 5432 (marina/marina/marina)
alembic upgrade head          # head atual: 0005_conversa_whatsapp
python scripts/seed.py        # 3 consultores + agenda seg-sex 9h-18h

python -m app.cli             # conversa com a Marina no terminal
python -m app.whatsapp        # servidor do webhook da Twilio (:8000)
```

Não há `.env` versionado — copie o `.env.example`. `alembic/env.py` lê a URL do
banco de `app.config`, nunca do `alembic.ini`.

### Testes

```bash
pytest                                              # 343 testes
pytest tests/test_machine.py                        # um arquivo
pytest tests/test_guards.py -k "escopo"             # um caso
ATUALIZAR_SNAPSHOT=1 pytest tests/test_e2e_transcricao.py   # regrava o snapshot da copy
```

`tests/test_machine.py`, `test_parsers.py`, `test_guards.py`, `test_machine_pura.py`,
`test_juridico_mapeamento.py` e `test_whatsapp_adaptador.py` **não tocam o banco**
e rodam em menos de um segundo. Os demais usam a fixture `sessao_db` (`tests/conftest.py`), que cria o
banco `marina_test` à parte e **pula com mensagem clara** se o Postgres não
estiver no ar — um skip em massa quase sempre significa `docker compose up -d`,
não um bug.

Ao mexer em `app/messages.yaml`, `test_e2e_transcricao` falha de propósito e
mostra o diff da copy. Confira o diff antes de regravar o snapshot.

**Não há linter, formatter nem type checker configurado.** `pytest` é o único
gate. Os `# noqa: BLE001` no código são resquício de um ruff que não está no
projeto.

### Scripts operacionais

```bash
python scripts/dump_lead.py --telefone "+5511987654321"   # cadastro + transcrição inteira
python scripts/sync_consultores.py                        # importa usuarios do juridico
python scripts/configurar_agenda.py --listar              # define quem atende quando
python scripts/flush_outbox.py [--falhados|--reprocessar] # força a entrega ao juridico
python scripts/simular_webhook_twilio.py --texto "oi"     # webhook assinado, sem ngrok
```

## Arquitetura

### As três camadas, e a fronteira entre elas

```
adaptador de canal  →  machine.handle()  →  engine
(converte mundo)       (DECIDE, puro)       (EXECUTA I/O)
```

`machine.handle(estado, lead, ctx, entrada) -> Transicao` é uma **função pura**:
não toca banco, rede, disco nem relógio. Devolve `Transicao(novo_estado,
lead_updates, ctx_updates, mensagens, efeitos)`; quem executa é o `engine`.

`tests/test_machine_pura.py` é a cerca que sustenta isso: analisa a AST de
`app/machine.py` e falha se aparecer import de `sqlalchemy`, `os`, `pathlib`,
`zoneinfo`, `httpx`, `random` ou dos módulos de I/O do próprio app; se alguém
chamar `datetime.now`, `uuid4`, `open`, `input` ou `print`; e ainda importa
`app.machine` num subprocesso para pegar o stack de banco puxado **por tabela**,
via cadeia de imports. Ao mexer em `machine.py`, rode esse teste.

Consequência prática: tudo que exigiria I/O chega pronto no `ctx` —
`ctx["agora"]` (relógio), `ctx["politicas"]` (limites, que viriam do `.env`),
`ctx["slots_ofertados"]` (datas **já formatadas**, porque converter fuso leria a
base de fusos), `entrada.anexo` (já resolvido pelo adaptador),
`ctx["reconhecimento"]` (resultado de um efeito).

### Estados internos = fronteiras de I/O explícitas

`RESOLVENDO_IDENTIDADE`, `CARREGANDO_HORARIOS` e `RESERVANDO` (em
`ESTADOS_INTERNOS`, `app/enums.py`) são estados em que a máquina **não espera
digitação**: ela emite um `Efeito` e para. O engine executa, grava o resultado no
`ctx` e chama `handle` de novo com `InboundMessage()` vazia — o laço de reentrada
em `Engine._processar`, limitado a `MAX_REENTRADAS = 8` para que um bug vire erro
visível em vez de laço infinito.

Efeito novo se declara como dataclass frozen em `machine.py` e se executa em
`Engine._executar_efeitos` — nunca inline na máquina.

### Duas entradas no engine: `rodar()` e `atender()`

`rodar()` é o laço de um canal que **puxa** a mensagem — o terminal. Abre a
conversa, dispara a saudação e fica esperando `adaptador.receber()`.

`atender(identidade, entrada)` é a entrada de um canal em que a **mensagem chega
sozinha** (WhatsApp). Não há laço: cada chamada é um passo da conversa daquela
identidade, e o processo não fica preso a ninguém. Por isso a `Execucao` é
reidratada do banco a cada mensagem (`_reidratar`) em vez de viver na memória —
inclusive o contador da transcrição, que não pode recomeçar em zero.

Não force um no outro. A semântica difere na primeira mensagem: no terminal a
saudação vem antes da primeira digitação, e a linha digitada é o nome; no WhatsApp
a mensagem que abre a conversa dispara a saudação e **não** é consumida como
resposta. Os dois compartilham tudo abaixo de `_processar`.

`conversas.identidade_externa` é o que liga uma mensagem à sua conversa antes de
existir lead, e o índice parcial `uq_conversa_aberta_por_identidade` garante uma
conversa aberta por número. A concorrência é serializada por
`pg_advisory_lock` de sessão (`Engine._trava`) — de sessão, e não de transação,
porque um turno abre várias transações curtas.

### O adaptador é burro, e a entrada é tipada

`ChannelAdapter` (`app/channels/base.py`) só converte mundo → `InboundMessage` e
`MensagemSaida` → envio. A entrada é tipada (`texto` **ou** `anexo`) por um
motivo concreto: com string crua, a máquina teria que olhar o disco para saber se
aquele texto é um caminho de arquivo. `TerminalAdapter` é o único componente
autorizado a tocar o disco; `ScriptedAdapter` é o usado nos testes.

`TwilioWhatsAppAdapter` (`app/channels/whatsapp_twilio.py`) faz o mesmo pelo
WhatsApp: `receber()` devolve None (ninguém puxa; quem dirige é o webhook), e ele
é criado **por turno** amarrado ao destino, porque `enviar()` não carrega
destinatário. A mídia é **baixada para um arquivo temporário** e é o caminho dele
que vai em `Anexo.origem` — guardar a `MediaUrl` quebraria `LocalDocumentStorage`,
que lê o arquivo e valida pela extensão.

### Nenhuma resposta é gerada

**Sem IA, sem LLM, em hipótese nenhuma.** Todo texto sai de `app/messages.yaml`
por `CatalogoMensagens.render()`, que levanta `ChaveDesconhecida` ou
`VariavelFaltando` na hora. É YAML para o time da Leve revisar a copy sem tocar
em código. Resposta nova = chave nova no YAML, nunca string no `.py`.

Guardrails (`app/guards.py`), na ordem em que `handle` os aplica: anexo pula os
guardrails de texto → comandos globais (`sair`/`atendente`/`voltar`, comparados
por **palavra inteira**, senão "pessoal" no menu escalaria o lead) → escopo
proibido (`taxa`, `juros`, `honorári`…, comparados por **trecho**, para pegar
"honorários"/"abusivo") → parser do estado, que na 3ª falha escala para humano.
Não existe parsing de data em linguagem natural: horário é escolhido pelo número
do menu, e ponto.

### Persistência

`app/models.py` — tudo em UTC (`DateTime(timezone=True)`), conversão para
`America/Sao_Paulo` só na renderização (`app/datas.py`, que formata em português
**sem locale**, porque pt_BR falha em boa parte das instalações do Windows).

- `conversas.lead_id` é **nullable de propósito**: a deduplicação é por contato,
  então o lead só nasce quando telefone (ou e-mail) é validado. Até lá, nome,
  e-mail e consentimento vivem em `conversas.contexto` (JSONB).
- `mensagens.sequencia` ordena a transcrição, **não** o timestamp: o `now()` do
  Postgres devolve a hora da *transação*, e o desempate cairia no UUID aleatório.
- `Engine._ctx_persistivel` filtra `CHAVES_TRANSITORIAS` antes de gravar o ctx:
  chaves que carregam objetos ricos ou resultado de I/O recém-feito não vão para
  o JSONB.
- Toda mensagem, de entrada **e** de saída, vai para `mensagens` com o
  `template_key` que a produziu. Não é opcional: é o dataset contra o qual uma
  versão futura com IA será comparada.

### Agendamento: capacidade, não atribuição

A reunião **nasce sem dono** — vai ao CRM como tarefa em aberto, e a copy nunca
promete o nome de um consultor. `app/routing.py` ordena por **carga** (menos
agendamentos futuros; empate por quem está parado há mais tempo) apenas para
decidir se o horário cabe; `app/booking.py` consome a vaga de um consultor
específico.

Duas defesas contra dupla marcação, não uma: `SELECT ... FOR UPDATE` no consultor
com revalidação dentro da trava, **mais** o índice parcial
`UNIQUE (consultor_id, inicio_utc) WHERE status = 'agendado'`. Quando a segunda
dispara, **isso não é erro** — é o caminho normal do segundo lead, que recebe
`slot_perdido`. Tudo atrás da interface `CalendarProvider` (`app/calendar.py`),
para o Google Calendar entrar sem tocar na máquina de estados.

### WhatsApp pela Twilio

Desligado por padrão no sentido de que nada sobe sem `python -m app.whatsapp`.
`app/web/webhook.py` tem as rotas; a ordem lá não é negociável: **assinatura**
(`X-Twilio-Signature`, senão 403 e nada acontece) → **idempotência**
(`mensagens_recebidas`, UNIQUE por `MessageSid`) → **atender fora da requisição**
e devolver 204 vazio. Não devolvemos TwiML: quem envia é a REST API, que é o que
permite mandar várias mensagens em ordem e registrar cada uma na transcrição.

A URL usada para conferir a assinatura vem de `TWILIO_URL_PUBLICA`, não dos
cabeçalhos — o endereço do ngrok muda a cada reinício, e esquecer de atualizar a
variável faz **toda** chamada virar 403.

`scripts/simular_webhook_twilio.py` exercita isso sem ngrok e sem conta.
Documentação: `docs/whatsapp/`.

### Integração com o `juridico`

Desligada por padrão (`JURIDICO_ATIVO=false`), e com ela desligada a Marina roda
sozinha. O backend é o **`base-crm-juridico`** (Laravel 12 + Inertia), disponível
como diretório de trabalho adicional em `C:\Dev\JURIDICO\base-crm-juridico` —
confira as rotas em `routes/api.php` lá antes de mudar o cliente.

Aquele backend **não tinha API nenhuma**: era só Inertia com sessão e CSRF. A
superfície `/api/integracao/*` foi criada para esta integração, e é estreita de
propósito — cinco rotas, autenticadas por token do Sanctum. O token pertence a um
usuário de serviço lá dentro, e é dele que saem a empresa e as permissões.

Não existe "lead" naquele CRM. O que existe é **contato + negociação** (a
negociação *é* o lead) e **tarefa** (que exige `negociacao_id` — não há tarefa
avulsa). A tradução é:

| `TipoEnvio` | vira |
|---|---|
| `LEAD` | `POST /api/integracao/negociacoes` — reaproveita o contato pelo telefone ou cria um novo; a negociação nasce em `contato_feito` |
| `QUALIFICACAO` | `POST .../negociacoes/{id}/observacoes` — vira `HistoricoObservacao` |
| `AGENDAMENTO` | `POST .../negociacoes/{id}/tarefas` — tarefa do tipo `reuniao` |

`leads.crm_lead_id` guarda o **id da negociação**.

Três camadas, e o engine só conhece a primeira:

1. `app/crm.py` — o Protocol `CrmSink` (`registrar_lead` / `registrar_qualificacao`
   / `registrar_agendamento`), com `CrmSinkNoop` e `CrmSinkOutbox`. **Nunca
   levanta erro para o engine**: um problema de integração não pode derrubar um
   atendimento em andamento.
2. `app/outbox.py` — fila local genérica, que não sabe o que é o juridico.
   `pendentes()` usa `FOR UPDATE SKIP LOCKED`, e isso **é correção, não
   performance**: a drenagem é disparada a cada marco da conversa, e sem a
   reserva um lead virava três cadastros. Erro permanente queima as tentativas de
   uma vez, para o item sair da fila e virar algo que uma pessoa olha.
3. `app/integrations/juridico/` — **tudo que é específico daquele backend**:
   `cliente.py` (HTTP, token fixo — não há login nem renovação a administrar),
   `mapeamento.py` (tradução pura, testável sem rede), `entregador.py` (drena a
   fila) e `fabrica.py` (monta as peças pela config).

   Em `cliente.py`, o cabeçalho **`Accept: application/json` não é cosmético**:
   sem ele o Laravel trata a chamada como navegador, e um token inválido vira 302
   para a tela de login enquanto um erro de validação vira redirecionamento. Os
   dois seriam lidos como resposta definitiva e matariam o item na fila.

Duas invariantes da entrega: a **ordem por lead** é respeitada (um agendamento
não pode chegar antes de o lead existir lá; se o lead falha, os itens *dele*
esperam e os de outros leads seguem) e **a conversa nunca espera** — o engine
dispara a drenagem em background e faz uma `_drenagem_final` depois do estado
terminal.

## Pontos de atenção

- **`app/aio.py::executar` no lugar de `asyncio.run`, sempre.** No Windows o loop
  padrão é o Proactor e o psycopg async não funciona nele — a conexão falha com
  `InterfaceError` antes de qualquer query. Todo entrypoint assíncrono (cli,
  scripts, alembic) usa `executar`; os testes usam o hook
  `pytest_asyncio_loop_factories` em `conftest.py` pelo mesmo motivo.
- **O README está defasado em pontos concretos.** Ele diz 301 testes (são 343) e
  descreve endpoints de um backend que não existe mais. A reunião fica **em
  aberto** no nome do usuário de serviço e nenhuma mensagem cita um consultor.
  Para regra de negócio, a fonte atual é **`docs/SDR/marina-regras-de-negocio.md`**,
  não o README.
- **Usuário de serviço no juridico.** Gere com
  `php artisan integracao:usuario "Nome da Empresa"` lá; o comando cria um perfil
  com só duas permissões (`comercial.negociacoes` e `comercial.tarefas`) e imprime
  o token **uma única vez**. Evite dar `administrador_empresa` a ele: essa chave
  faz `temPermissao` devolver `true` para tudo.
- **A negociação e a reunião nascem no nome do usuário de serviço**, porque o
  juridico exige responsável em toda tarefa — é assim que "em aberto" se
  representa. Alguém do time precisa assumir depois; não há rodízio.
- **Log em arquivo por padrão** (`LOG_ARQUIVO=marina.log`). Com a variável vazia o
  JSON do structlog se intercala com a conversa no terminal e deixa o atendimento
  ilegível.
- `sessao_db` limpa por `TRUNCATE`, não por transação: `booking.reservar` faz
  commit de propósito, porque a trava só vale se a transação existir de verdade.
- Enums são `StrEnum` em `app/enums.py`; as colunas correspondentes são `String`.
