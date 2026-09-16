# Manual operacional: Z-API para um SDR de WhatsApp

Como operar sem perder conversa, e como perder o mínimo quando o número cair.

Este documento parte de uma premissa: **o banimento vai acontecer em algum momento**. Nenhuma prática aqui reduz essa chance a zero — a própria documentação da Z-API abre dizendo que não existe boa prática 100% eficaz. O que dá para controlar é o custo.

---

## Parte 1 — O que você está contratando

### A instância é a unidade

Um número de WhatsApp vinculado, com URL própria, credenciais próprias e ciclo de vida próprio: **ativa**, **desconectada** ou **suspensa por cobrança**. Tudo que você automatiza conversa com a instância, nunca com "a Z-API" em abstrato.

Consequência prática: instância suspensa por atraso de pagamento derruba a operação daquele número mesmo com a plataforma inteira do fornecedor no ar. Cartão vencido é incidente de produção — trate como tal.

### O número é seu

A Z-API não fornece linha. A conexão é pareamento de dispositivo, o mesmo mecanismo do WhatsApp Web: o número que ler o QR Code vira o número do bot.

Existe o recurso **PhoneLess**, que permite operar sem celular conectado usando números físicos ou virtuais, configurado por token em vez de QR Code. É cobrado à parte e custa bem mais que o plano padrão — confirme o valor atual antes de contar com ele no orçamento.

### A autenticação tem duas camadas

Este é o erro mais comum de estreia:

- **Credenciais da instância**, que vão na URL — identificam o número
- **Client-Token**, que vai no header — autentica a sua conta

Chamada com uma camada e sem a outra gera o erro mais famoso do ecossistema.

> **Segurança:** as credenciais da instância viajam na URL, o que significa que elas vazam em log de proxy, de CDN e de qualquer intermediário. Nunca dê print da tela da instância, nunca registre a URL completa em log de aplicação, e rotacione o token se isso acontecer — existe botão de "Gerar novo token" no painel.

### O risco é contratualmente seu

A Z-API afirma não ter controle sobre decisões de bloqueio do WhatsApp, e a isenção de responsabilidade em casos de banimento está nos Termos aceitos na contratação. Se você não repassar esse risco ao seu cliente com o mesmo cuidado, ele para em você.

---

## Parte 2 — Antes de conectar: preparar o número

### A regra que não se quebra

O bot **nunca** roda no seu número pessoal nem no número principal do cliente. Chip dedicado, comprado para isso. Custa R$ 10 a R$ 20 e evita que um banimento leve junto a sua conta bancária, seus contatos e a operação comercial de quem te contratou.

### Maturação

A documentação da Z-API é explícita: número que nasce conectado numa API é o perfil que mais cai. A recomendação deles:

1. **Use o número pelo celular ou WhatsApp Web antes de plugar na API.** Mandar e receber mensagens reais, entrar em algum grupo, salvar contatos.
2. **Comece gradual e aumente o volume aos poucos.** Nada de ligar no volume final no primeiro dia.
3. **Continue usando o número ocasionalmente no celular** mesmo depois de automatizado. Número que só faz automação tem risco maior.
4. **Número recém-desbanido:** 3 a 5 dias de maturação antes de reconectar.

### Perfil

Preencha foto, nome e descrição antes de conectar. Conta sem nenhuma identidade visual é sinal de descarte.

E se este número está substituindo um que caiu: **não replique o perfil anterior**. O WhatsApp associa o número novo ao antigo quando foto, nome, descrição e padrão de comportamento são idênticos.

### Checklist de ativação

- [ ] Chip dedicado, comprado com documento, nunca o pessoal
- [ ] Número usado 3 a 7 dias no celular antes de conectar
- [ ] Foto, nome e descrição preenchidos
- [ ] Alguns contatos salvos e conversas reais no histórico
- [ ] Webhooks configurados **antes** de ler o QR Code
- [ ] Token da instância rotacionado se já apareceu em print ou log

---

## Parte 3 — Configuração que evita perder conversa

### Configure todos os webhooks, não só o de receber

A Z-API só aceita webhooks **HTTPS**. Os quatro que importam:

| Webhook | Dispara quando | Por que você precisa |
|---|---|---|
| **Ao receber** | alguém interage com seu número | é a entrada do bot |
| **Ao enviar (delivery)** | sua mensagem chegou ao WhatsApp | confirma saída, **não** confirma recebimento pelo contato |
| **Status da mensagem** | recebida, lida, respondida, excluída | alimenta follow-up e CRM |
| **Ao desconectar** | indisponibilidade celular–WhatsApp ou celular–Z-API | **é o seu alarme de incêndio** |

Dá para apontar todos para a mesma URL e rotear internamente pelo tipo do evento. Também existem filtros por tipo de mensagem e por tipo de webhook, úteis para ignorar grupos, por exemplo.

### Trate "Ao desconectar" como alerta, não como log

Instância desconectada sem detecção significa que as mensagens chegam e não são processadas — a operação para sem ninguém perceber. O payload traz `momment` (quando caiu) e `error` (descrição).

O que fazer com ele:

- Disparar notificação imediata para um canal que alguém realmente olha
- Registrar o `momment` como **início da janela cega** (veja Parte 7)
- Iniciar o runbook da Parte 8 automaticamente

Complemento importante: monitore também **silêncio**. Se um número que normalmente recebe mensagem toda hora fica 90 minutos sem nenhum evento em horário comercial, isso é sinal mesmo sem webhook de desconexão.

### Persista antes de processar

A primeira coisa que seu endpoint faz é gravar o evento cru. Processar depois. Se o agente LLM falhar, a mensagem do lead não pode ter sumido junto.

### Trate eventos repetidos

A documentação avisa: uma mesma mensagem pode passar por vários status e até repetir o mesmo status, como "respondida". Seu processamento precisa ser idempotente — chave única por `messageId` + status, e nunca disparar ação de negócio duas vezes pelo mesmo evento.

### Fila de saída com retry e dead-letter

Toda mensagem que o bot quer enviar passa por fila. Quando a instância está fora, nada some em silêncio: as mensagens ficam retidas, e quando o número volta (ou o reserva sobe) você sabe exatamente o que não foi entregue e para quem.

Sem isso, o buraco no funil só aparece semanas depois, quando alguém pergunta por que aquele lead nunca respondeu.

---

## Parte 4 — Arquitetura de contingência

Cinco peças. A primeira é a que mais reduz dano e a que menos gente aplica.

### 1. Nunca divulgue o número direto

Anúncios, botão do site e QR Code impresso apontam para um **redirecionador que você controla**, não para o número em si.

Com redirecionador, trocar um número banido é mudar um registro no banco — minutos. Sem ele, você refaz campanha, reimprime material e ainda perde todo mundo que salvou o contato na agenda.

### 2. Número reserva aquecido em standby

Um segundo chip, maturado, com volume baixo e conversas reais rodando o tempo todo. Não adianta comprar o reserva no dia do incidente: chip novo conectado às pressas cai rápido.

Existe mercado de "chips aquecidos" no Brasil com reposição imediata. Ignore os que anunciam "sem risco de banimento" — é garantia que ninguém pode dar.

### 3. Histórico e contatos no seu banco

Como tudo passa pelo seu webhook, grave conversa, contato e contexto do lead do seu lado. Assim o banimento vira **perda de canal**, não perda de base.

Isso é crítico no não oficial, onde o histórico tende a ficar preso no ambiente da ferramenta ou no aparelho. Quem descobre isso depois do ban não recupera.

### 4. Canal alternativo pronto antes de precisar

E-mail, SMS e um formulário no site, já configurados. Quando o WhatsApp cai, o cliente avisa a base em vez de sumir.

### 5. Camada de transporte trocável

Seu agente não deve saber que existe Z-API. Um adapter com `enviar()`, `receber()` e `status()` isola o fornecedor. Isso serve para trocar de número, trocar de fornecedor, ou migrar para a API oficial da Meta sem reescrever o bot.

---

## Parte 5 — Reduzir a chance de banimento

### Os fatores que pesam

Segundo a documentação da Z-API, dois fatores são determinantes:

1. **Quantos destinatários diferentes o número tenta alcançar**
2. **O conteúdo da mensagem**

Histórico do número, IP/ASN e método de conexão pesam, mas são secundários.

### O que isso significa para um SDR reativo

Boa notícia real: se o lead sempre inicia, o fator número um fica quase zerado. Você está no melhor cenário possível dentro do não oficial.

Mas não confunda com imunidade. A própria documentação registra que a antiga distinção entre "contas passivas não são banidas" e "contas muito ativas são" **não é mais tão clara**, e que em muitos casos apenas conectar um número novo à plataforma já pode gerar banimento.

### Práticas de conteúdo

- Escreva mensagens que **incentivem resposta**. Padrão unidirecional parece spam mesmo quando não é.
- Evite disparo idêntico para muita gente. Varie a redação.
- Cuidado com listas e botões: há relatos consistentes de que remover mensagens de lista e botão fez o banimento parar.
- Respeite horário comercial. Bot respondendo às 3h da manhã é assinatura de automação.
- Introduza variação no tempo de resposta. Resposta instantânea e sempre idêntica é o padrão mais fácil de detectar.

### O que nunca fazer

- Importar lista e disparar para contatos que não escreveram primeiro
- Reconectar número recém-banido sem maturação
- Subir número novo já no volume total
- Replicar exatamente o perfil de um número que caiu

---

## Parte 6 — Quando o número cai: os três estágios

Nem toda queda é banimento, e nem todo banimento é igual.

| Estágio | Como identificar | O que fazer |
|---|---|---|
| **Restrição temporária** | ainda dá para usar no celular; volta sozinha após o período | espere, não force nada, não reconecte na API |
| **Banimento com recurso** | tela de bloqueio **com** botão *Solicitar análise* | abra o recurso imediatamente |
| **Banimento definitivo** | tela de bloqueio **sem** o botão | não cabe apelação; vire a chave para o reserva |

A presença do botão *Solicitar análise* é o diagnóstico. O FAQ do WhatsApp é explícito: se a tela não mostra a opção de solicitar análise, o banimento é definitivo e não cabe apelação.

### Sobre o recurso

- Só existem dois canais reconhecidos: o botão na tela e o e-mail `support@whatsapp.com`
- Prazo típico: até 24 horas pelo botão; relatos de até 72 horas por e-mail
- **Não existe telefone, chat ou atendente no Instagram.** Quem cobra para "recuperar" número é golpe
- Registrar um número banido na API oficial normalmente não resolve

### O histórico

- **Temporário:** o histórico continua no aparelho e volta com a conta
- **Permanente:** o acesso acaba; sobra apenas o backup que já existia

Por isso a Parte 4, item 3, existe.

---

## Parte 7 — Conversas em andamento

Esta é a parte mais difícil, porque o dano aqui não é técnico — é de confiança.

Do lado do lead não aparece aviso nenhum. Ele vê a mensagem parada sem confirmação de entrega e conclui que a empresa não respondeu. E quando alguém aparece de outro número dizendo "oi, sou eu da empresa X", o brasileiro já foi treinado a achar que é golpe de clonagem. Você não está só reconectando: está tendo que provar identidade.

### Triagem por estágio

Tratar todas as conversas igual é o erro que queima o número reserva.

| Estágio | Ação | Prioridade |
|---|---|---|
| **Reunião já marcada** | confirme por e-mail e telefone | primeira hora |
| **Perto de fechar** | contato humano, um a um | primeiras horas |
| **Em qualificação** | convite por e-mail para reiniciar; deixe o lead iniciar | dias seguintes |
| **Só cumprimentou** | espere o número voltar | não vale queimar o reserva |

### Retome o estado, não a conversa

O thread se perde. O contexto só se perde se você não tiver guardado.

Se cada lead tem no seu banco o estágio, o que já respondeu e o que estava pendente, a retomada é *"você estava decidindo entre terça e quinta"* em vez de *"oi, tudo bem?"*. A irritação real do lead não é trocar de número — é ter que repetir tudo.

### Tire o valor do WhatsApp o quanto antes

No fluxo de agendamento, o momento em que a reunião entra no sistema deve disparar convite de calendário por e-mail. Aí o banimento não cancela reunião nenhuma: ela já existe fora do canal.

Pela mesma lógica, **peça o e-mail cedo na qualificação**, não no fim. É o que transforma o WhatsApp de ponto único de falha em apenas um transporte.

### Faça o lead mandar a primeira mensagem

Nunca dispare do número novo. Mande por e-mail ou pelo site um link que abre a conversa com o número novo, e deixe ele iniciar.

Você ganha duas vezes: o lead iniciando zera o fator número um de banimento, e quem clica num link vindo de um e-mail que já confia não suspeita de golpe.

### Prove identidade sem pedir nada

A primeira mensagem no canal novo deve:

- Citar algo que só vocês dois sabem — o que ele perguntou, o horário que estava avaliando
- **Não pedir código, dado ou pagamento**
- Vir depois de um aviso público nos canais oficiais da empresa, para que o lead possa verificar sozinho

### A janela cega

As mensagens que os leads mandaram para o número banido **nunca chegam até você**. Não há recuperação.

O `momment` do webhook de desconexão marca o início dessa janela e dimensiona o estrago. Vale a retomada assumir isso na primeira linha: *"pode ser que sua última mensagem não tenha chegado até nós"*.

### Escalone o retorno

Reabrir 200 conversas numa hora no número novo reproduz exatamente o padrão que derruba números. Distribua por dias, na ordem da triagem: a reunião marcada entra na primeira hora, quem só cumprimentou entra quando o número já tiver histórico.

---

## Parte 8 — Runbook de incidente

Escreva isto antes de precisar. Durante a crise ninguém improvisa bem.

**T+0 — detecção automática**
- Webhook "Ao desconectar" dispara alerta
- Sistema registra `momment` e congela envios daquele número
- Fila de saída começa a reter

**T+5min — classificação**
- Alguém abre o WhatsApp no celular do chip e vê a tela
- Determina o estágio: temporária, com recurso, ou definitiva
- Se for desconexão simples (não banimento), reconecta e encerra

**T+15min — recurso e chaveamento**
- Se tem botão *Solicitar análise*: abre o recurso agora
- Se é definitivo: vira a chave do redirecionador para o número reserva
- Atualiza webhooks da instância reserva

**T+30min — triagem**
- Roda a consulta de conversas ativas das últimas 72h agrupadas por estágio
- Separa as reuniões marcadas e as conversas perto de fechar

**T+1h — comunicação**
- Aviso nos canais públicos da empresa sobre a troca de número
- Contato humano com as reuniões marcadas, por e-mail e telefone
- Cliente informado por você, antes de ele perceber sozinho

**T+2h a T+24h — retomada escalonada**
- Conversas perto de fechar, uma a uma
- Convite por e-mail para os em qualificação reiniciarem no número novo
- Acompanhar o resultado do recurso

---

## Parte 9 — Contrato e cliente

### Decida quem paga

A Z-API se isenta contratualmente. Você tem duas saídas legítimas:

- **Transferir o risco** ao cliente e cobrar menos
- **Absorver o risco** e precificar — incluindo o número reserva no plano

O que não funciona é não decidir. Cliente que descobre o risco no dia do incidente vai embora independente de quem estava certo.

### Transparência antes, não depois

Explique na proposta que o canal é WhatsApp não oficial, que existe risco de bloqueio, e mostre o plano de contingência. Isso converte melhor do que parece: cliente que vê plano de incidente entende que você já pensou nisso.

### Migração como upgrade

Ofereça a API oficial da Meta como plano superior. Cliente que cresce ou que não tolera risco migra sem reescrita — e a limitação vira degrau de receita em vez de motivo de churn.

---

## Parte 10 — O que precisa existir antes do incidente

Se você escrever qualquer uma dessas coisas durante a crise, já perdeu as horas que mais valiam.

- [ ] Consulta de conversas ativas nas últimas 72h, agrupada por estágio
- [ ] Alerta no webhook de desconexão, apontando para canal que alguém olha
- [ ] Detector de silêncio por número em horário comercial
- [ ] Redirecionador com número trocável por configuração
- [ ] Número reserva maturado e conectado
- [ ] Templates de e-mail: aviso de instabilidade, convite para reiniciar, confirmação de reunião
- [ ] Runbook da Parte 8 impresso ou fixado onde a equipe vê
- [ ] Export do histórico rodando periodicamente

---

## Resumo em uma frase

Nenhuma prática deste documento reduz muito a chance do banimento acontecer — elas reduzem o que ele custa. O cliente não vai embora porque o número caiu. Vai embora porque ficou três dias sem canal, perdeu o histórico, e descobriu tudo isso sozinho.
