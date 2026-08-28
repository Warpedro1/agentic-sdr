# Marina — regras de negócio

> Documento de referência da automação de SDR. Escrito para ser lido por quem não
> mexe no código.
>
> Cada regra está marcada com seu estado:
> **[FEITO]** já funciona · **[DECIDIDO]** acordado, ainda não construído ·
> **[EM ABERTO]** ninguém decidiu ainda.
>
> Última atualização: 27/08/2026.

---

## 1. O que a Marina é

Uma assistente virtual que **captura e qualifica leads** interessados em revisão
ou renegociação de contrato de empréstimo, e entrega **uma reunião agendada com
um consultor**.

Ela não vende, não negocia e não fecha nada. O trabalho dela termina quando
existe um horário marcado na agenda de um consultor humano.

### O que ela deliberadamente NÃO é

**Não usa inteligência artificial.** Nenhuma resposta é gerada — todas são
escolhidas de um catálogo fixo de textos aprovados. Isso não é limitação
técnica: é o que garante que a Marina **nunca** improvise sobre juros, valores ou
honorários. Um sistema que gera texto pode dizer qualquer coisa; um sistema que
escolhe texto só pode dizer o que foi aprovado.

**Não lê o contrato.** Ela recebe o arquivo e arquiva. A análise é sempre humana,
feita por um consultor. Ela inclusive diz isso ao lead: *"A análise é feita por um
consultor, nunca automaticamente."*

**Não interpreta datas escritas por extenso.** "Pode ser amanhã de tarde" não é
entendido de propósito. Horário só é escolhido pelo número do menu. É exatamente
o ponto onde uma automação erra feio, e errar ali significa marcar reunião no dia
errado — o consultor perde a hora e o lead perde a confiança.

**Por que isso importa:** esta versão é a **linha de base mensurável**. Quando um
dia existir uma versão com IA, ela será comparada contra estes números. Por isso
toda conversa é gravada por inteiro.

---

## 2. O que a Marina pergunta, e nessa ordem

1. **Nome**
2. **Telefone** (com DDD)
3. **E-mail** — só se o telefone não sair
4. **Como chegou até a empresa** (anúncio, WhatsApp, e-mail, LinkedIn,
   indicação, Google, outro) — não bloqueia
5. **Que tipo de contrato quer revisar** (consignado, pessoal, cartão de crédito)
6. **Tem o contrato em mãos?** — se sim, oferece receber o arquivo
7. **Escolha do horário** e **confirmação**

**[FEITO]** Todo o fluxo, do "olá" ao agendamento gravado.

### O que basta para cadastrar

> **[FEITO — regra alterada em 27/08/2026]** Antes o e-mail era obrigatório e a
> fonte também travava o cadastro. Não travam mais.

**Nome mais UM meio de contato — telefone ou e-mail — bastam.**

- **Com telefone válido, a Marina nem pergunta o e-mail.** Um contato é o
  suficiente, e cada pergunta a mais é uma chance a mais de a pessoa desistir no
  meio.
- **Se o telefone não sair depois das tentativas, ela pede o e-mail** em vez de
  entregar a conversa para um humano. Antes esse era o motivo mais comum de
  escalonamento — e desperdiçava um lead que ainda dava para cadastrar.
- **Sem telefone e sem e-mail, aí sim escala.** Não há para onde retornar, e
  prometer retorno seria mentira.
- **A fonte de referência não bloqueia.** Se a Marina não entender a resposta
  depois das tentativas, ela diz *"deixa isso pra lá"* e segue. É dado de
  marketing: não muda nada do que o consultor precisa preparar.
- **Tipo de contrato e "tem o contrato em mãos" continuam obrigatórios.** Esses
  qualificam o caso — sem eles o consultor entra na reunião às cegas.

**Consequência para quem volta:** o reconhecimento é pelo telefone quando existe,
e pelo e-mail quando não existe. Quem se cadastrou só por e-mail e depois escreve
de outro canal pode não ser reconhecido — é o preço de aceitar meio contato, e é
menor do que perder o lead inteiro.

### Regras de interpretação

- **Nome** — aceita texto livre; recusa se for só números.
- **Telefone** — extrai os dígitos de qualquer formato, valida se o DDD existe de
  verdade no Brasil, e recusa número com todos os dígitos iguais.
- **Origem e tipo de contrato** — aceita **o número do menu ou a palavra**.
  Responder "cartão" funciona igual a responder "3". O que a pessoa escreveu fica
  guardado junto com a resposta interpretada, para ninguém perder o contexto.
- **Sim/não** — "não tenho" é lido como **não**, e não como sim por conter a
  palavra "tenho".

---

## 3. Guardrails — o que a Marina se recusa a fazer

### 3.1 Assunto proibido: dinheiro

**[FEITO]** Se o lead perguntar quanto custa, sobre honorários, taxas, juros,
valores, quanto vai receber, ou se o contrato é abusivo, a Marina **desvia sem
responder**:

> *"Essa é uma ótima pergunta para o consultor — ele analisa seu contrato e te
> explica direitinho. Prefiro não dar um número sem olhar o seu caso. Vamos
> agendar?"*

E volta o lead para o ponto onde ele estava.

Há um teste automático que lê a conversa inteira procurando qualquer sinal de
valor (R$, %, "reais"). Se alguém editar os textos e colocar um número, o teste
falha antes de ir para produção.

### 3.2 Comandos que valem em qualquer momento

**[FEITO]**

| A pessoa digita | O que acontece |
|---|---|
| `sair`, `parar`, `cancelar` | Encerra e registra que ela não quer mais ser contatada |
| `atendente`, `humano`, `pessoa` | Passa para um consultor humano |
| `voltar` | Volta para a pergunta anterior |

### 3.3 Limite de três tentativas

**[FEITO]** Se a pessoa responder algo que a Marina não entende **três vezes no
mesmo ponto**, ela para de insistir e passa para um humano.

Sem esse limite, o lead ficaria num laço e desistiria em silêncio — que é a pior
saída possível, porque ninguém fica sabendo que ele desistiu.

**Duas exceções ao limite**, ambas por decisão de 27/08/2026:

- **Telefone** — três erros não escalam mais; a Marina pede o e-mail.
- **Fonte de referência** — três erros não escalam; ela segue sem a fonte.

**Detalhe que vale registrar:** quando o escalonamento acontece sem nenhum
contato conhecido, a Marina **não promete** que alguém vai ligar — porque não há
para onde ligar nem para onde escrever. Ela pede que a pessoa volte a chamar.

---

## 4. Quem pediu para sair

**[FEITO]** Quando alguém digita `sair`, o contato entra numa lista de supressão.

**Regra:** a lista só recebe registro se houver **telefone ou e-mail** para
suprimir. Quem desiste antes de informar o telefone simplesmente encerra a
conversa — não faz sentido bloquear um contato desconhecido, e isso encheria a
lista de linhas vazias.

**[DECIDIDO]** O opt-out vale para a empresa inteira, **todas as filiais**. Quem
pediu para sair pediu para sair da empresa, não de uma unidade — ser abordado de
novo por outra filial da mesma empresa seria exatamente o que a pessoa recusou.

### Quando a pessoa volta

**[FEITO]** A Marina reconhece quem já falou com ela, pelo telefone:

```
telefone informado e reconhecido
  ├─ pediu para sair antes?
  │     SIM → "Você tinha pedido para encerrar. Quer retomar?"
  │            └─ se confirmar → registra a revogação e segue
  └─ NÃO → a última conversa foi há quanto tempo?
            < 24h  → "Vamos continuar de onde paramos?"
            > 24h  → conversa nova, mas reaproveita o que já sabe
```

**Regra de conflito:** se o nome digitado agora for diferente do cadastrado,
**vence o cadastrado**. O nome novo fica registrado à parte para um humano olhar.
Sobrescrever um cadastro por causa de um erro de digitação no meio da conversa é
pior do que ignorar a divergência.

---

## 5. Agendamento

### 5.1 Como os horários são calculados

**[FEITO]**

1. Cada consultor tem dias e faixa de horário de atendimento.
2. A faixa é fatiada em reuniões de duração fixa, com intervalo entre elas
   (padrão: 60 minutos de reunião + 15 de intervalo → horários de 75 em 75 min).
3. Horários já ocupados saem da lista.
4. Horários muito próximos de agora saem da lista (padrão: **antecedência mínima
   de 2 horas**).
5. Os horários de todos os consultores são misturados num menu só. Um horário
   aparece **uma vez**, e aparece se **alguém** estiver livre nele. Os **5
   primeiros** são oferecidos.

O lead escolhe um **horário**, e só isso.

### 5.2 Quem atende: ninguém, até alguém pegar

> **[FEITO — regra alterada em 27/08/2026]** Antes a Marina escolhia um consultor
> e **dizia o nome dele ao lead** ("agendada com Ana Ribeiro"). Não faz mais.

**A reunião nasce sem dono.** Ela vai para o juridico como uma **tarefa de CRM em
aberto**, e quem tiver disponibilidade assume.

**Por que mudou:** prometer um nome só faz sentido se aquela pessoa for mesmo
quem aparece. Como a tarefa fica aberta para o time pegar, o nome prometido e o
nome que aparece podem ser diferentes — e aí a promessa vira problema na frente
do lead. Melhor não prometer.

**O que a Marina fala agora:** confirma **data e hora**, e diz que a conversa é
"com um dos nossos consultores".

#### O consultor que continua existindo nos bastidores

A Marina ainda calcula a agenda **por pessoa** — precisa saber quem trabalha
quando para saber se o horário cabe. E ao reservar, ela consome a vaga de um
consultor específico.

**Isso é contagem de capacidade, não atribuição.** Aquele consultor não é
prometido a ninguém, não vai no que é enviado ao juridico, e o lead nunca vê o
nome. Se outra pessoa pegar a tarefa, a agenda do consultor reservado fica
bloqueada à toa — imprecisão aceita de propósito, porque a alternativa era
perder a proteção contra dupla marcação que o banco já dá.

> **Atrito conhecido:** o juridico distribui *leads* por rodízio circular; a
> Marina calcula capacidade por carga. Convivem porque decidem coisas diferentes
> — o juridico decide de quem é o lead, a Marina decide se cabe mais uma reunião
> naquele horário.

### 5.3 Duas pessoas escolhendo o mesmo horário

**[FEITO]** Acontece na primeira semana de uso. A Marina tem **duas** defesas:
uma trava enquanto reserva, e uma regra no banco que recusa a segunda marcação.

**[FEITO]** E o horário só é perdido quando **todos** os consultores daquele
horário acabarem. Se o primeiro for tomado entre a oferta e a confirmação, a
Marina tenta o próximo em silêncio, em vez de fazer o lead escolher de novo.

Quando não sobra ninguém, **isso não é erro** — é o caminho normal do segundo
lead, que recebe *"Ih, esse horário acabou de ser preenchido. Escolhe outro?"* e
a lista recalculada.

**[DECIDIDO]** A trava vale por **pessoa**. Se o mesmo consultor atender mais de
uma filial, ele nunca é marcado em duas reuniões no mesmo horário — a agenda dele
é uma só, independente de por qual unidade a reunião entrou.

---

## 6. Uma empresa, e a filial escondida do lead

> **Correção registrada em 27/08/2026.** Esta seção dizia antes que a Marina
> atenderia várias empresas. Era leitura errada do juridico, e está desfeita.
> O texto anterior está descrito abaixo só para quem encontrar referências a ele.

**[DECIDIDO]** Esta instalação do juridico atende **uma empresa**. No vocabulário
das telas dele:

- **"Empresa"** identifica as **filiais de uma mesma empresa** — não clientes
  diferentes.
- **"Funcionário"** representa o **papel** daquela pessoa dentro da empresa.

### A regra de produto

**A Marina esconde a filial de quem está do outro lado.** Ela organiza a conversa
**só por horário e agendamento** — nunca pergunta, nunca menciona unidade, filial
ou regional.

> Para quem está procurando ajuda, "qual unidade me atende" é assunto interno da
> empresa. Perguntar acrescentaria uma pergunta que não qualifica ninguém, e
> ainda por cima é uma pergunta que a pessoa pode responder errado. Quem sabe
> qual consultor atende é a empresa; o lead só precisa de um horário.

### O que isso cancela

A reforma multi-empresa que estava planejada **não vai acontecer**: nada de
`empresa_id` nas tabelas, nada de identificação por *(empresa + telefone)*, nada
de uma sessão do juridico por empresa, nada de nome de empresa variável na
saudação. O modelo de uma empresa só, como está hoje, continua valendo.

### Como a Marina se identifica no juridico

**[FEITO]** Um usuário de serviço, criado em 27/08/2026:

| | |
|---|---|
| Login | `marina` |
| Papel | **DIRETOR** |
| Filial | nenhuma (acesso a todas) |
| Módulos | CRM entre eles |

**Sobre o papel — corrige o que estava escrito aqui antes.** Este documento dizia
que a Marina precisava ser SUPERVISOR. Não precisa, e não dá: a tela de
funcionários do juridico **cria sempre como DIRETOR**, qualquer que seja o
"cargo" digitado — o cargo é só um rótulo.

E DIRETOR serve melhor do que SUPERVISOR:

- pode criar lead e agendamento;
- **não entra no rodízio** de distribuição, que só sorteia COMERCIAL, GERENTE e
  SUPERVISOR. Ou seja, a Marina cadastra os leads sem ficar com nenhum deles.

O papel a evitar continua sendo **COMERCIAL**: quando quem cria o lead é
COMERCIAL, o juridico atribui o lead a essa pessoa em vez de distribuir, e a
Marina ficaria com todos.

**Sem filial fixa, com acesso a todas.** É isso que põe a matriz inteira no
escopo dela e permite ignorar filial ao escolher consultor e horário.

**[EM ABERTO]** Risco a vigiar se filiais de verdade passarem a existir: um
agendamento gravado sem filial pode **não aparecer** no calendário de um
consultor que só enxerga a própria unidade. Hoje não há filial cadastrada, então
o risco é teórico.

### Papel próprio para a Marina

**[FEITO — 27/08/2026]** A Marina deixou de usar um papel de pessoa. O papel dela
agora é **`ROBO_SDR`**, e ele abre **só o módulo CRM**.

**Dois motivos, e o segundo é o que promoveu isto de "desejável" a
"pré-requisito":**

**1. Menos poder.** DIRETOR — o papel que ela usava — é o papel de um diretor de
verdade: abre **todos** os módulos, inclusive financeiro e jurídico. A Marina usa
**um**. Ela carregava uma chave que abre a empresa inteira para um trabalho que
precisa de uma chave só, e essa chave mora num arquivo de configuração.

**2. É o que torna "em aberto" reconhecível.** A tarefa da reunião fica parada no
nome da Marina até alguém pegar. Enquanto ela era DIRETOR, essa tarefa era
**indistinguível** da tarefa de um diretor de verdade — não havia como listar "o
que está em aberto" sem um jeito de dizer *"o responsável é o robô"*.

**O que o papel abre:** cadastrar lead, registrar o primeiro contato e criar a
tarefa da reunião. **O que ele não abre:** financeiro, jurídico, documentos de
cliente, criação de usuários, configuração da empresa.

**Duas condições que ele preserva de propósito:**

1. **Não entra no rodízio de distribuição.** Se entrasse, o juridico passaria a
   atribuir todos os leads ao robô e nenhum consultor receberia nada.
2. **Enxerga a matriz inteira, sem filial fixa.** É o que permite ignorar filial
   ao calcular horário.

> **Armadilha registrada:** no juridico, um papel que as listas de escopo não
> conheçam é **bloqueado em tudo, em silêncio** — a verificação de escopo devolve
> "não pode" para qualquer papel desconhecido. Papel novo tem que ser
> acrescentado lá explicitamente.

---

## 7. O que a Marina manda para o juridico

**[FEITO]** Três momentos:

| Quando | O que vai |
|---|---|
| Contato validado (telefone ou e-mail) | O lead é criado no CRM |
| Qualificação completa | Tipo de contrato, se tem o contrato, origem real |
| Reunião confirmada | Uma **tarefa de CRM em aberto**, do tipo REUNIÃO |

### A reunião vira tarefa, não agendamento

> **[FEITO — regra alterada em 27/08/2026]**

A reunião entra no juridico como **tarefa de CRM**, e aparece no calendário de lá
com a etiqueta **CRM**, no dia e hora certos.

**Como "em aberto" é representado:** o juridico exige responsável em toda tarefa —
não existe tarefa sem dono. Então a tarefa nasce **no nome do próprio usuário da
Marina**, e quem for atender troca o responsável para si. A partir daí ela é dele
e aparece no "apenas os meus" dele.

**Quem é o lead vai escrito no texto da tarefa** — nome, contato e o número do
cadastro no CRM. A tarefa do juridico não tem campo de lead, e sem isso quem
pegasse veria "Sem vínculo" no calendário e não teria com quem falar.

**Nenhum consultor é enviado.** Mandar um voltaria a atribuir a reunião a alguém,
que é justamente o que a regra nova desfez.

### Como alguém pega a tarefa

**[FEITO — 27/08/2026]**

- **Listar o que está esperando:** as tarefas cujo responsável ainda é o robô.
- **Assumir:** a tarefa passa a ser de quem assumiu e some da lista de abertas.

Com três regras:

| Regra | Por quê |
|---|---|
| **Qualquer pessoa com acesso ao CRM** assume; só o **administrador da empresa** não | Regra confirmada em 28/08/2026. A reunião é de quem for atender, e o administrador administra — não atende |
| Só é assumível o que **ainda está com o robô** | Senão "assumir" viraria um jeito de tomar a tarefa de um colega |
| Quem chega depois recebe *"esta tarefa já é de Fulano"* | Duas pessoas clicando junto é normal; a segunda precisa saber o que houve |

**Onde aparece o botão** — nos dois lugares onde a reunião é vista:

- **Comercial → Tarefas**, no cartão da tarefa;
- **Calendário**, ao abrir o compromisso.

Nos dois, enquanto ninguém assumiu, o responsável aparece como **"em aberto"** em
vez do nome do robô — que não diz nada a quem lê.

> **Atenção ao módulo.** O calendário pertence ao módulo **ATENDIMENTO**, não ao
> CRM. Um funcionário que só tenha CRM enxerga a reunião em *Comercial →
> Tarefas*, mas **não vê o calendário** — a aba nem aparece para ele. Para ver a
> agenda, ele precisa dos dois módulos.

### A conversa nunca espera pelo juridico

**[FEITO]** Se o juridico estiver lento ou fora do ar, o lead **não fica travado
no meio do atendimento**. A Marina anota o que precisa enviar e entrega à parte,
tentando de novo depois. Quando o juridico volta, tudo é entregue na ordem certa.

Sem isso, uma queda do juridico viraria uma queda da Marina — e a integração
deixaria a Marina pior do que ela era sem integração nenhuma.

### O que a tradução perde

**[FEITO]** O juridico aceita menos informação do que a Marina coleta:

| Na Marina | No juridico |
|---|---|
| 7 origens de lead | apenas 4 (`INSTAGRAM_META_ADS`, `GOOGLE_ADS`, `TV`, `INDICACAO`) |
| tipo de contrato | não existe campo |
| tem o contrato em mãos | não existe campo |

WhatsApp, e-mail, LinkedIn e "outro" viram `INDICACAO`. **Nada se perde em
silêncio:** tudo o que não cabe num campo vai escrito no registro de primeiro
contato do lead, que é texto livre e vira histórico.

### Lead que já existe no juridico

**[FEITO]** O juridico recusa cadastro duplicado por telefone. A Marina lê essa
recusa como *"essa pessoa já está aqui"*, localiza o cadastro e segue com ele.

**Limite conhecido:** a Marina não coleta CPF. Se a pessoa já existir no juridico
cadastrada **só** por CPF, não há como casar automaticamente — o caso vai para
uma lista de pendências para alguém resolver.

---

## 8. O que fica registrado

**[FEITO]**

- **A conversa inteira**, mensagem por mensagem, de entrada e de saída, com
  identificação de qual texto do catálogo produziu cada resposta. Não é opcional:
  é o material de avaliação e de auditoria.
- **O consentimento**, carimbado **no momento em que foi dado** (na saudação), e
  não quando o cadastro foi criado no banco, várias mensagens depois. É esse
  carimbo que precisa ser defensável se alguém questionar.
- **O contrato enviado**, arquivado em disco com registro de tipo e tamanho. O
  conteúdo nunca é lido.

---

## 9. Painel de configuração dentro do juridico

**[EM ABERTO]** Ideia registrada em 27/08/2026: configurar a Marina por um botão
dentro do juridico, sem intervenção técnica.

**O que já existe e pode ser aproveitado:** o juridico já tem, por empresa, uma
tela de configuração de WhatsApp que guarda o identificador do número, o número,
a mensagem de boas-vindas, origens e FAQ — e que **cifra** as credenciais da Meta
(`accessToken`, `appSecret`, `webhookVerifyToken`). Ela até mostra a URL do
webhook pronta para colar no painel da Meta.

**O que faltaria acrescentar:**

- uma chave "atender com a Marina" (em vez do bot de FAQ atual);
- quais consultores atendem e em quais horários — hoje o passo mais manual;
- antecedência mínima e quantos horários oferecer;
- e-mail para onde vão as pendências que precisam de gente.

**Ponto que precisa ser decidido antes:** um número de WhatsApp só pode estar
ligado a um aplicativo por vez. Se o número que a empresa já usa for o mesmo, ele
precisa migrar. Isso é decisão de negócio, não técnica.

---

## 10. Decisões ainda em aberto

| Assunto | Situação |
|---|---|
| Reunião marcada à mão no juridico bloquear horário na Marina | **[EM ABERTO]** — hoje ela não lê a agenda de lá e pode marcar em cima |
| Quem recebe as mensagens do WhatsApp — o juridico repassa ou a Marina recebe direto | **[EM ABERTO]** |
| Número de WhatsApp da Marina — novo ou migrar o atual | **[EM ABERTO]** — depende de conta Meta |
| Coletar CPF para casar com cadastros antigos do juridico | **[EM ABERTO]** |
| Follow-up de quem abandonou a conversa no meio | **[EM ABERTO]** — hoje não existe |

---

## 11. Fora de escopo, por decisão

Nenhuma IA ou LLM · interpretação de datas em texto livre · leitura do conteúdo do
contrato · envio real de e-mail · bloqueios e férias de consultor · painel web
próprio da Marina · atendimento por e-mail.
