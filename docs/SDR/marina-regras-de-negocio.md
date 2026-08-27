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
3. **E-mail** — obrigatório: é por ele que vai o convite da reunião
4. **Como chegou até a empresa** (anúncio, WhatsApp, e-mail, LinkedIn,
   indicação, Google, outro)
5. **Que tipo de contrato quer revisar** (consignado, pessoal, cartão de crédito)
6. **Tem o contrato em mãos?** — se sim, oferece receber o arquivo
7. **Escolha do horário** e **confirmação**

**[FEITO]** Todo o fluxo, do "olá" ao agendamento gravado.

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

**Detalhe que vale registrar:** quando o escalonamento acontece por erro no
telefone, a Marina **não promete** que alguém vai ligar — porque justamente não
há telefone válido para ligar. Ela pede que a pessoa volte a chamar.

---

## 4. Quem pediu para sair

**[FEITO]** Quando alguém digita `sair`, o contato entra numa lista de supressão.

**Regra:** a lista só recebe registro se houver **telefone ou e-mail** para
suprimir. Quem desiste antes de informar o telefone simplesmente encerra a
conversa — não faz sentido bloquear um contato desconhecido, e isso encheria a
lista de linhas vazias.

**[DECIDIDO]** O opt-out vale **apenas para a empresa** onde a pessoa pediu para
sair. Se ela desistiu da empresa A, a empresa B continua podendo abordá-la — a
empresa B nunca falou com ela e tem base própria para isso. Cada empresa é dona
da relação dela.

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
5. Os horários de todos os consultores são misturados, ordenados, e os **5
   primeiros** são oferecidos.

O lead escolhe um **horário**, não um consultor. Quem vai atender é decidido pela
Marina.

### 5.2 Quem atende

**[FEITO]** Entre os consultores livres naquele horário, ganha **quem tem menos
reuniões futuras**. Empate desempata por quem está há mais tempo sem receber.

**Por que não por rodízio circular:** com rodízio, um consultor de férias
continua "recebendo a vez" e a fila trava. Por carga, a distribuição se
reequilibra sozinha.

> **Atrito conhecido:** o juridico distribui *leads* por rodízio circular; a
> Marina distribui *reuniões* por carga. Convivem porque decidem coisas
> diferentes — o juridico decide de quem é o lead, a Marina decide quem atende a
> reunião. Vira problema no dia em que alguém quiser um número único de "leads
> por consultor".

### 5.3 Duas pessoas escolhendo o mesmo horário

**[FEITO]** Acontece na primeira semana de uso. A Marina tem **duas** defesas:
uma trava enquanto reserva, e uma regra no banco que recusa a segunda marcação.

Quando a segunda defesa dispara, **isso não é erro** — é o caminho normal do
segundo lead, que recebe *"Ih, esse horário acabou de ser preenchido. Escolhe
outro?"* e a lista recalculada.

**[DECIDIDO]** Se o mesmo consultor atender duas empresas, a trava vale por
**pessoa**, não por vínculo — ele nunca é marcado em duas reuniões no mesmo
horário, mesmo que sejam de empresas diferentes.

---

## 6. Várias empresas

**[DECIDIDO]** A Marina atende **várias empresas**, e todas elas são empresas
cadastradas dentro do juridico. Um CRM por empresa, mesmo sistema.

### Regras

- **Cada empresa é um mundo separado.** Leads, conversas, consultores, horários,
  agendamentos e listas de supressão pertencem a uma empresa.
- **A mesma pessoa pode ser lead de duas empresas** e são dois cadastros
  distintos. A identificação é por *(empresa + telefone)*, nunca por telefone
  sozinho.

  > Esta é a regra mais importante desta seção. Se a identificação fosse só pelo
  > telefone, a mesma pessoa viraria **um cadastro compartilhado** entre dois
  > clientes — e a empresa B enxergaria o que a empresa A apurou. Não é bug de
  > funcionamento, é vazamento entre clientes.

- **Quem decide de qual empresa é a conversa é o canal.** No WhatsApp, o número
  que recebeu a mensagem. O juridico já sabe resolver empresa a partir do número.
- **A Marina se apresenta com o nome da empresa certa.** O nome não pode ser
  texto fixo na saudação.

### Como a Marina se identifica no juridico

**[DECIDIDO]** Um único usuário "Marina" no juridico, vinculado a várias
empresas — o mesmo mecanismo que o juridico usa para funcionários que atendem
mais de uma empresa.

- Papel **SUPERVISOR** em cada empresa.

  > Não pode ser COMERCIAL: verificado no código do juridico, quando quem cria o
  > lead é COMERCIAL, o lead é atribuído a essa pessoa em vez de ir para o
  > rodízio. A Marina ficaria com todos os leads e nenhum consultor receberia
  > nada.

- O usuário precisa ter **matriz e filial coerentes** em cada empresa. Sem isso os
  leads nascem fora de escopo e as listagens do juridico voltam **vazias, sem dar
  erro** — falha silenciosa, difícil de perceber.
- **Uma sessão por empresa.** O juridico verifica se a sessão está no contexto da
  empresa que o acesso diz, então um acesso da empresa A **para de funcionar** se
  aquela sessão trocar para a empresa B. O isolamento é garantido pelo próprio
  juridico, não pela nossa disciplina.

---

## 7. O que a Marina manda para o juridico

**[FEITO]** Três momentos:

| Quando | O que vai |
|---|---|
| Telefone validado | O lead é criado no CRM |
| Qualificação completa | Tipo de contrato, se tem o contrato, origem real |
| Reunião confirmada | O agendamento, com o consultor certo |

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

## 9. Painel de configuração por empresa

**[EM ABERTO]** Ideia registrada em 27/08/2026: dentro do juridico, abrir uma
empresa e configurar a Marina ali, sem intervenção técnica.

**O que já existe e pode ser aproveitado:** o juridico já tem, por empresa, uma
tela de configuração de WhatsApp que guarda o identificador do número, o número,
a mensagem de boas-vindas, origens e FAQ — e que **cifra** as credenciais da Meta
(`accessToken`, `appSecret`, `webhookVerifyToken`). Ela até mostra a URL do
webhook pronta para colar no painel da Meta.

**O que faltaria acrescentar:**

- uma chave "atender com a Marina" (em vez do bot de FAQ atual);
- nome de exibição da empresa, usado na saudação;
- quais consultores atendem e em quais horários;
- antecedência mínima e quantos horários oferecer;
- e-mail para onde vão as pendências que precisam de gente.

**Ponto que precisa ser decidido antes:** um número de WhatsApp só pode estar
ligado a um aplicativo por vez. Cada empresa precisa do próprio número, ou o
número atual precisa migrar. Isso é decisão de negócio, não técnica.

---

## 10. Decisões ainda em aberto

| Assunto | Situação |
|---|---|
| Quem recebe as mensagens do WhatsApp — o juridico repassa ou a Marina recebe direto | **[EM ABERTO]** |
| Número de WhatsApp por empresa | **[EM ABERTO]** — depende de conta Meta |
| Unificar os dois rodízios (juridico por ponteiro, Marina por carga) | **[EM ABERTO]** — convivem hoje |
| Coletar CPF para casar com cadastros antigos do juridico | **[EM ABERTO]** |
| Follow-up de quem abandonou a conversa no meio | **[EM ABERTO]** — hoje não existe |

---

## 11. Fora de escopo, por decisão

Nenhuma IA ou LLM · interpretação de datas em texto livre · leitura do conteúdo do
contrato · envio real de e-mail · bloqueios e férias de consultor · painel web
próprio da Marina · atendimento por e-mail.
