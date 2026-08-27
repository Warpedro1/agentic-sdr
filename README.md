# Marina — automação de SDR da Leve

Captura e qualifica leads interessados em revisão ou renegociação de contrato de
empréstimo, e entrega uma **reunião agendada com um consultor**.

**Determinística, sem nenhuma IA ou LLM.** Toda resposta é escolhida de um catálogo
fixo ([`app/messages.yaml`](app/messages.yaml)) — nunca gerada. É isso que garante que a
Marina jamais opine sobre juros, valores ou honorários.

Este MVP é também a **linha de base mensurável** contra a qual uma versão futura com
agente de IA vai ser comparada. Por isso a transcrição completa de cada conversa é
gravada, com o template que produziu cada resposta.

---

## Como rodar

Precisa de Docker e Python 3.12+ (desenvolvido e verificado no 3.14).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Linux/macOS: .venv/bin/python

docker compose up -d              # Postgres 16
alembic upgrade head              # cria as tabelas
python scripts/seed.py            # 3 consultores, agenda seg–sex 9h–18h

python -m app.cli                 # conversa com a Marina
```

Conferir o que foi gravado:

```bash
python scripts/dump_lead.py --telefone "+5511987654321"
```

Mostra cadastro, documento, agendamento com o consultor atribuído e a transcrição
inteira da conversa.

### Testes

```bash
pytest
```

301 testes. Os de parser, guardrail e máquina de estados **não precisam de banco** e
rodam em menos de um segundo; os demais pulam com mensagem clara se o Postgres não
estiver no ar.

Ao ajustar a copy do `messages.yaml`, o teste de snapshot falha de propósito e mostra o
diff. Se a mudança for desejada:

```bash
ATUALIZAR_SNAPSHOT=1 pytest tests/test_e2e_transcricao.py
```

---

## O fluxo

```
INICIO
  ▼ boas-vindas + aviso LGPD (grava consentimento)
AGUARDANDO_NOME
  ▼
AGUARDANDO_TELEFONE
  ▼ telefone válido → efeito ResolverIdentidade
RESOLVENDO_IDENTIDADE          (interno: engine consulta, preenche ctx, reinvoca)
  ├─ opt-out ativo ──────▶ AGUARDANDO_RETOMADA_OPT_OUT
  ├─ conversa < 24h ─────▶ AGUARDANDO_RETOMADA_CONVERSA
  └─ novo / > 24h ───────▶ segue, pulando o que já sabe
  ▼
AGUARDANDO_EMAIL  →  AGUARDANDO_FONTE  →  AGUARDANDO_TIPO_CONTRATO
  ▼
AGUARDANDO_TEM_CONTRATO
  ├─ sim ─▶ AGUARDANDO_ENVIO_CONTRATO ─┐
  └─ não ───────────────────────────────┤
                                        ▼
                          CARREGANDO_HORARIOS        (interno)
                                        ▼
                          AGUARDANDO_ESCOLHA_HORARIO
                                        ▼
                          AGUARDANDO_CONFIRMACAO
                                        ▼
                          RESERVANDO                 (interno)
                                        ▼
                                    AGENDADO

De qualquer estado:  OPT_OUT · ESCALADO_HUMANO
```

---

## Arquitetura

Três princípios, e as consequências que eles impõem.

### 1. A máquina de estados é uma função pura

`machine.handle(estado, lead, ctx, entrada) -> Transicao`. Não toca banco, rede, disco
nem relógio. Devolve o que deve acontecer; quem **executa** é o `engine`.

Isso não é purismo: é o que permite cobrir todas as transições em milissegundos, sem
subir Postgres. [`tests/test_machine_pura.py`](tests/test_machine_pura.py) é a cerca —
falha se alguém importar banco, relógio ou disco em `machine.py`, inclusive por tabela,
num subprocesso.

Para a pureza sobreviver, tudo o que seria I/O chega pronto:

| Precisaria de I/O | Como chega |
|---|---|
| hora atual | `ctx["agora"]`, posto pelo engine |
| limites de tentativa e retomada | `ctx["politicas"]` |
| data formatada em português | já formatada em `ctx["slots_ofertados"]` |
| arquivo enviado | `entrada.anexo`, já resolvido pelo adaptador |
| quem é este lead | `ctx["reconhecimento"]`, resultado de um efeito |

**As fronteiras de I/O são estados internos explícitos.** Em `RESOLVENDO_IDENTIDADE`,
`CARREGANDO_HORARIOS` e `RESERVANDO`, a máquina emite um efeito e para. O engine
executa, grava o resultado no `ctx` e chama `handle` de novo com entrada vazia.

Esse mecanismo de reentrada é o que faz o WhatsApp caber depois **sem tocar no fluxo**:
lá o telefone é conhecido na mensagem zero, então o engine resolve a identidade antes do
primeiro `handle`, e o reconhecimento acontece antes da saudação.

### 2. O canal é um adaptador burro

Converte mundo → `InboundMessage` e `MensagemSaida` → envio. Nada mais.

A entrada é **tipada** (`texto` ou `anexo`) por um motivo concreto: se o adaptador
entregasse uma string crua, a máquina teria que olhar o disco para saber se aquele texto
é um caminho de arquivo — I/O dentro da função pura, e lógica que só valeria no
terminal. O `TerminalAdapter` é o único componente autorizado a tocar o disco.

### 3. Nenhuma resposta é gerada

Todo texto sai do `messages.yaml`, que é YAML justamente para o pessoal da Leve revisar
a copy sem tocar em código. Chave inexistente ou variável faltando falham na hora.

---

## Guardrails

Ordem de avaliação de cada mensagem recebida:

1. **Anexo?** Pula os guardrails de texto — eles só existem para texto.
2. **Comandos globais**, válidos em qualquer estado: `sair`/`parar`/`cancelar` →
   opt-out; `atendente`/`humano`/`pessoa` → escalonamento; `voltar` → estado anterior.
   A comparação é por **palavra inteira**: por trecho, responder "pessoal" no menu de
   tipo de contrato casaria com "pessoa" e escalaria o lead por engano.
3. **Escopo proibido** — `quanto custa`, `honorári`, `abusiv`, `quanto vou receber`,
   `taxa`, `juros`, `valor`, `cobra`, `preço`. Desvia **sem responder** e devolve o lead
   ao fluxo. Aqui a comparação é por trecho, para pegar "honorários" e "abusivo"; o
   custo aceito é um falso positivo ocasional, que só resulta em reperguntar.
4. **Parser do estado.** Falhou → repete. Na 3ª falha → escalonamento humano.

**Não existe parsing de data em linguagem natural.** Horário é escolhido pelo número do
menu, e ponto — é exatamente onde uma automação erra feio, e errar ali significa marcar
reunião no dia errado.

**A análise do contrato é humana.** A Marina recebe e arquiva o arquivo; nunca o abre,
nunca lê o conteúdo.

---

## Decisões de modelagem que não são óbvias

**O lead só nasce depois do telefone.** O fluxo pede o nome antes, mas a deduplicação é
por telefone. Criar o lead em `AGUARDANDO_NOME` geraria uma linha sem telefone e, no
passo seguinte, a descoberta de que a pessoa já tinha cadastro. Até o telefone ser
validado, nome, e-mail e consentimento ficam em `conversas.contexto` — por isso
`conversas.lead_id` é **nullable**.

**O consentimento é carimbado quando foi dado**, na saudação, e não quando a linha do
lead é criada, várias mensagens depois. É esse carimbo que precisa ser defensável.

**Opt-out sem contato não vira registro.** Se a pessoa digita `SAIR` antes de informar o
telefone, não há o que suprimir. A conversa encerra, mas nenhuma linha vai para
`opt_outs` — bloquear um desconhecido não significa nada e enche a lista de supressão de
linhas vazias.

**A ordem da transcrição é uma coluna, não um timestamp.** O `now()` do Postgres devolve
o horário da *transação*, então mensagens gravadas no mesmo passo ficam com a hora
idêntica e o desempate cairia no UUID, que é aleatório — na prática, a saudação aparecia
depois da primeira pergunta. `mensagens.sequencia` resolve isso.

**No reconhecimento, o cadastro vence o que foi digitado agora.** Se o nome informado
diverge do cadastrado, o antigo permanece e o novo vai para o contexto como
`nome_informado_divergente`. Sobrescrever um cadastro por um erro de digitação no meio da
conversa é pior que ignorar a divergência.

---

## Agendamento

O `Agendamento` tem **duas defesas** contra dupla marcação, não uma:

1. `SELECT ... FOR UPDATE` no consultor serializa tentativas concorrentes, e a
   revalidação acontece dentro da trava.
2. Um índice **parcial** `UNIQUE (consultor_id, inicio_utc) WHERE status = 'agendado'` é
   a rede embaixo. Parcial de propósito: cancelar um horário o devolve para a oferta.

Quando a segunda defesa dispara, **isso não é erro** — é o caminho normal do segundo
lead, que recebe `slot_perdido` e a lista recalculada. Dois leads escolhendo o mesmo
horário ao mesmo tempo aparece na primeira semana de uso.

**Distribuição por carga, não por ponteiro.** Entre os consultores livres num horário,
ganha quem tem menos agendamentos futuros; empate desempata por quem está parado há mais
tempo. Um ponteiro circular continuaria entregando a vez de quem está de férias e a fila
travaria.

Tudo atrás da interface `CalendarProvider`, para o Google Calendar entrar depois sem
tocar na máquina de estados.

---

## Fora do escopo deste MVP

Nenhuma IA ou LLM · parsing de data em linguagem natural · leitura do conteúdo do
contrato · adaptadores de WhatsApp e e-mail · qualquer endpoint HTTP · Google Calendar ·
follow-up de conversa parada · envio real de e-mail · bloqueios e férias de consultor ·
painel web · multi-tenant · **integração com o backend `juridico`**.

### Sobre a integração com o `juridico`

Está **bloqueada**: aquele sistema não tem API máquina-a-máquina. O painel dele anuncia
`POST /api/public/oauth/token`, mas essa rota não existe no servidor.

Aqui fica apenas a costura: a interface `CrmSink` (`registrar_lead` /
`registrar_agendamento`) com uma implementação que só registra no log. Sem cliente HTTP e
sem contrato de API inventado.

**Dois pontos de atrito conhecidos para quando esse dia chegar** — anotados, não
resolvidos:

- **Dois round-robins conflitantes.** O `juridico` distribui leads por ponteiro circular
  (`atribuirLeadRoundRobin`, com estado em `LeadDistribuicaoEstado`); aqui a distribuição
  é por carga. Um dos dois terá de ceder.
- **Deduplicação incompatível.** O `Lead` do `juridico` tem chave única em `(tenant, cpf)`
  e `(tenant, email)`, **não em telefone** — que é exatamente a chave de deduplicação
  daqui. A Marina não coleta CPF.

Só quando a API existir isso vira decisão de implementação; até lá, é risco documentado.

---

## Integração com o `juridico`

Desligada por padrão. Com `JURIDICO_ATIVO=false` a Marina funciona sozinha,
exatamente como sem integração nenhuma.

### Ligar

No `juridico`, alguém precisa criar **um usuário para a Marina**:

- papel **SUPERVISOR** — importante: se fosse `COMERCIAL`, o juridico atribuiria
  **todos** os leads à própria Marina em vez de distribuir entre os consultores;
- ativo, com **matriz e filial coerentes**. Sem isso os leads nascem fora de
  escopo e as listagens do juridico voltam **vazias, sem dar erro** — o modo de
  falha mais traiçoeiro daquele sistema.

Depois, no `.env` da Marina:

```
JURIDICO_ATIVO=true
JURIDICO_BASE_URL=https://o-endereco-do-juridico
JURIDICO_LOGIN=...
JURIDICO_SENHA=...
```

E então:

```bash
python scripts/sync_consultores.py     # importa os consultores reais
python scripts/configurar_agenda.py    # define quem atende quando
```

O segundo passo é humano e não tem como ser adivinhado: **o juridico não tem
noção nenhuma de agenda** — lá um agendamento é uma data que uma pessoa digitou.
Consultor sem horário definido nunca aparece na lista oferecida ao lead.

### Nenhuma alteração é necessária no `juridico`

Todos os endpoints usados já existem: `POST /api/auth/login`,
`POST /api/auth/refresh`, `GET /api/admin/leads/responsaveis`,
`POST /api/admin/leads`, `GET /api/admin/leads?q=`,
`POST /api/admin/leads/:id/primeiro-contato` e
`POST /api/admin/leads/:id/agendamentos`.

### A conversa nunca espera pelo juridico

O envio acontece durante o atendimento. Se o outro sistema estiver lento ou fora
do ar, o lead não pode ficar travado no meio da conversa.

Por isso a Marina grava a intenção em `crm_outbox` — escrita local e rápida — e
entrega em segundo plano, com nova tentativa e espera crescente. A conversa
termina sempre. Quando o juridico volta, tudo é entregue.

```bash
python scripts/flush_outbox.py              # força a entrega agora
python scripts/flush_outbox.py --falhados   # o que desistiu e precisa de gente
```

A ordem por lead é respeitada: não adianta mandar o agendamento antes de o lead
existir do outro lado. Se o envio de um lead falha, os itens seguintes **dele**
esperam; os de outros leads seguem normalmente.

### O que a tradução perde, e o que se faz a respeito

| Na Marina | No juridico |
|---|---|
| 7 fontes de lead | só 4 origens (`INSTAGRAM_META_ADS`, `GOOGLE_ADS`, `TV`, `INDICACAO`) |
| tipo de contrato | não tem campo |
| tem o contrato em mãos | não tem campo |

WhatsApp, e-mail, LinkedIn e "outro" viram `INDICACAO`, e o `metadadosJson` do
lead é calculado no servidor do juridico, que ignora o que o cliente manda.

Tudo o que não cabe num campo vai no **texto do primeiro contato**, que é livre e
vira histórico do lead. Nada se perde em silêncio.

### Atritos conhecidos, anotados e não resolvidos

- **Dois rodízios.** O juridico distribui leads por ponteiro circular; a Marina
  distribui reuniões por carga. Convivem porque cada um decide uma coisa
  diferente: o juridico decide de quem é o lead, a Marina decide quem atende a
  reunião. Vira problema no dia em que alguém quiser um número único de "leads
  por consultor".
- **CPF.** A Marina não coleta. O juridico deduplica por CPF, e-mail e telefone;
  se a pessoa já existir lá cadastrada **só** por CPF, a Marina não consegue
  casar sozinha e o item vai para a lista de falhados, para uma pessoa resolver.

---

## Estrutura

```
app/
  machine.py       ← o coração: função pura, sem I/O
  engine.py        ← faz o I/O e executa os efeitos
  parsers.py       ← o que a Marina entende, e o que se recusa a adivinhar
  guards.py        ← comandos globais e escopo proibido
  messages.yaml    ← toda a copy
  availability.py  ← geração de horários
  routing.py       ← distribuição por carga
  booking.py       ← reserva com trava
  calendar.py      ← interface + implementação Postgres
  crm.py           ← contrato do CRM externo + fila local
  outbox.py        ← entrega resiliente, sem travar a conversa
  integrations/juridico/  ← TUDO que é específico do juridico mora aqui
  storage/local.py ← arquivamento de contratos
  channels/        ← base (tipos) + terminal
  cli.py           ← ponto de entrada
scripts/           ← seed e dump_lead
tests/             ← 301 testes
```
