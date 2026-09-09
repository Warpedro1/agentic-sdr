# Marina no WhatsApp — o motor orientado a mensagem

> Requisitos da mudança de comportamento, **sem fornecedor**. Vale igual para
> Meta Cloud API, Twilio, 360dialog ou Zenvia — é o trabalho que precisa existir
> antes de qualquer um deles.
>
> Fonte: artefato *"WhatsApp para a Marina"* (setembro de 2026). Este documento
> contém **apenas** os requisitos listados lá. Nada foi acrescentado.
>
> Cada requisito está marcado com seu estado:
> **[FEITO]** já funciona · **[DECIDIDO]** acordado, ainda não construído ·
> **[EM ABERTO]** ninguém decidiu ainda.
>
> **Estado: construído em 03/09/2026** (menos o que segue marcado em aberto).
>
> Escolha de fornecedor e integração: ver `02-integracao-twilio.md`.

---

## 1. O problema

A Marina de hoje conversa com **uma pessoa de cada vez**. O motor abre a conversa,
pergunta, e fica parado esperando a resposta chegar — como um atendente ao
telefone que não desliga. É o que faz sentido no terminal.

O WhatsApp funciona ao contrário: **a mensagem chega sozinha**, a qualquer
momento, de qualquer pessoa, e o programa precisa descobrir de qual conversa ela
faz parte. Não dá para ficar esperando alguém específico.

| Como funciona hoje | Como precisa ficar |
|---|---|
| abre a conversa | chega uma mensagem do número X |
| manda a saudação | acha a conversa aberta de X |
| espera parado a resposta | avança um passo |
| avança um passo | grava e responde |
| volta a esperar | encerra o atendimento |
| ✕ **só uma conversa por vez** | **milhares em paralelo** |

---

## 2. A boa notícia: o miolo não muda

**[FEITO]** O miolo da Marina — as perguntas, as regras, o que ela faz com cada
resposta — **não muda uma linha**. Ele já foi escrito sem saber de onde vem a
mensagem, e o estado de cada conversa já é gravado no banco. Quem precisa virar
do avesso é só o laço que fica esperando.

O que já está pronto e sustenta isso:

- `app/machine.py` é uma função pura. `tests/test_machine_pura.py` é a cerca que
  garante — ele analisa a AST e falha se aparecer I/O ali dentro.
- O encaixe do canal já estava previsto: `app/channels/base.py:29` documenta que
  `Anexo.origem` é "caminho local no terminal, media_id no WhatsApp", e a entrada
  é tipada como `texto` **ou** `anexo` exatamente porque no WhatsApp não chega
  caminho de arquivo nenhum.
- `Canal.WHATSAPP` já existe em `app/enums.py`, e `juridico_canal_contato` em
  `app/config.py` já vale `"WHATSAPP"`.

> **Consequência para quem for implementar:** se a mudança exigir tocar
> `machine.py`, `messages.yaml`, `guards.py` ou `parsers.py`, algo saiu do
> caminho. Em particular, **a pergunta do telefone continua sendo feita** mesmo
> que o WhatsApp já entregue o número. O artefato diz que o miolo não muda, e a
> coluna do requisito A serve para *rotear a mensagem*, não para substituir a
> coleta.

---

## 3. Requisito A — a conversa precisa saber com quem está falando

**[FEITO]** — `conversas.identidade_externa`, migration `0005_conversa_whatsapp`.

A tabela de conversas guarda o vínculo com o lead, mas **não guarda o número de
telefone** (`app/models.py:113`). No começo da conversa o lead ainda não existe —
ele só nasce quando o telefone é validado —, então `conversas.lead_id` é nulo e
**não há por onde procurar** a conversa de quem acabou de escrever.

Precisa de uma coluna com a identidade de quem está falando. No WhatsApp ela chega
junto com a primeira mensagem, o que na prática facilita.

O que isso implica:

- Coluna nova em `conversas` com o identificador externo do interlocutor
  (telefone, no WhatsApp), preenchida **na abertura** da conversa.
- Índice que permita achar a conversa aberta daquele identificador em uma consulta.
- Migration nova (`alembic/versions/0005_*`). A head atual é `0004_contato_opcional`.
- No terminal não há identificador externo — a coluna aceita vazio, e o
  comportamento de hoje continua igual.

---

## 4. Requisito B — atender por mensagem recebida

**[FEITO]** — `Engine.atender()`.

Sair da conversa única para uma que atende por mensagem recebida. Em termos de
comportamento, os cinco passos da coluna da direita da tabela da seção 1:

1. **Chega uma mensagem do número X.** O motor deixa de puxar a próxima mensagem
   e passa a ser chamado com ela.
2. **Acha a conversa aberta de X.** Se existir uma conversa não encerrada daquele
   identificador, é dela que a mensagem faz parte. Se não existir, nasce uma.
3. **Avança um passo.** Exatamente o que já acontece hoje — inclusive o laço de
   reentrada dos estados internos (`RESOLVENDO_IDENTIDADE`, `CARREGANDO_HORARIOS`,
   `RESERVANDO`), que não muda.
4. **Grava e responde.** Transcrição de entrada e de saída, como já é feito.
5. **Encerra o atendimento.** Encerra o *processamento daquela mensagem*, não a
   conversa. O processo não fica preso a ninguém.

Onde isso encosta no código de hoje:

- **`Engine.atender(identidade, entrada)` é a entrada nova.** Trava, acha ou abre
  a conversa daquela identidade, registra a entrada, avança um passo, volta.
- `Engine._localizar_ou_abrir()` procura a conversa aberta antes de criar. Conversa
  parada há mais de `janela_retomada_horas` é encerrada e uma nova nasce: retomar
  no meio de uma pergunta um mês depois, sem contexto, é pior que recomeçar.
- `Engine._reidratar()` remonta a `Execucao` do banco a cada mensagem: estado,
  `contexto` (JSONB), `lead_id`, o dict do lead e o contador da transcrição — que
  não pode recomeçar em zero, porque `mensagens.sequencia` é único por conversa
  (`app/models.py:162`).
- `_processar`, `_aplicar` e `_executar_efeitos` **não mudaram**: já operavam sobre
  um passo de cada vez.

**`rodar()` continua existindo, e igual.** O plano falava em transformá-lo num laço
sobre `atender()`, e isso estava errado: no terminal a saudação vem **antes** da
primeira digitação, e a primeira linha digitada é o nome. No WhatsApp a primeira
mensagem dispara a saudação e não é consumida. São semânticas diferentes, e forçar
uma na outra estragaria o terminal — além de `rodar()` ser chamado em ~30 testes,
incluindo o snapshot da copy. Os dois caminhos compartilham tudo abaixo de
`_processar`.

**Dois detalhes que só aparecem quando o ctx passa pelo banco:**

- `consentimento_em` é carimbado na saudação e só usado duas mensagens depois,
  quando o lead nasce. O JSONB devolve texto; `_reidratar` volta a convertê-lo em
  data.
- `ctx["reconhecimento"]` é transitório de propósito, mas os dois estados de
  retomada o leem **depois** de esperar a resposta do lead. `_reconhecimento()`
  refaz a chave do banco — que é a fonte — em vez de persistir resultado de I/O.
  Sem isso, quem responde "sim, quero retomar" cairia no caminho genérico de
  coleta em vez de voltar exatamente onde parou.

---

## 5. Requisito C — mensagens do mesmo número em paralelo

**[FEITO]** — `Engine._trava()`.

"Milhares em paralelo" só é verdade se duas mensagens do **mesmo** lead chegando
ao mesmo tempo não avançarem o estado juntas — senão uma sobrescreve o que a outra
gravou, e a conversa pula ou repete uma pergunta.

O atendimento precisa ser serializado por conversa. O projeto já tem o remédio e o
precedente: `app/agenda/reserva.py` usa `SELECT ... FOR UPDATE` com revalidação dentro da
trava para impedir dupla marcação.

---

## 6. Segurança

Três pontos que mudam de verdade a exposição do sistema.

### 6.1 Quem chama o seu programa — **[FEITO]**

No WhatsApp, quem bate na sua porta é o fornecedor. **Sem conferir a assinatura de
cada chamada**, qualquer um que descubra o endereço consegue inventar mensagens e
criar leads e reuniões falsas no jurídico.

Requisito: chamada sem assinatura válida é rejeitada. A checagem só pode ser
desligada em ambiente de teste, e nunca por padrão.

### 6.2 Conversa é dado pessoal — **[EM ABERTO]**

Telefone, nome e o que a pessoa contou passam a ficar gravados. O jurídico já trata
isso com cuidado, e o mesmo padrão precisa valer no bot. Três perguntas sem
resposta hoje:

- **quanto tempo** guarda a transcrição e o cadastro;
- **quem lê** — quem tem acesso à transcrição e por qual tela;
- **como apagar** quando o titular pedir, incluindo o que já foi para o jurídico.

O artefato exige a política; os prazos ainda não foram definidos por ninguém.

### 6.3 Quem fica com o número — **[DECIDIDO]** (contrato, não código)

No cadastro embutido, o número e a conta são **do escritório cliente**, não seus.
É o certo — e é também o que permite ao cliente sair sem perder o próprio WhatsApp.
Vale deixar isso claro no contrato, porque é argumento de venda.

Nada a construir. Fica registrado aqui para não se perder.

---

## 7. O que este documento não cobre

Estava no artefato, mas como **decisão**, não como requisito de comportamento:

- a escolha entre Meta Cloud API, Twilio, 360dialog e Zenvia/Take Blip;
- o custo por mensagem e a conta de quanto sai por lead atendido;
- o cadastro embutido da Meta e o portão de **10 clientes novos a cada 7 dias**
  (só passa a 200 depois de verificação da empresa, revisão do aplicativo e
  verificação de acesso) — processo que leva semanas e **não depende de código**,
  e por isso vale começar cedo;
- o motivo de Evolution API, Z-API e semelhantes estarem fora: elas funcionam
  fingindo ser o WhatsApp Web, o que viola os termos de uso, e o bloqueio do
  número é permanente e sem recurso.

Escolha de fornecedor e o que fazer com ela: `02-integracao-twilio.md`.
