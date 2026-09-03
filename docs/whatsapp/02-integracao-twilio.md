# Integração com a Twilio

> Como colocar a Marina no WhatsApp usando a Twilio como intermediária, conforme
> a documentação oficial. Escrito em duas partes: **o que você faz** (conta,
> console, verificações — não depende de código) e **o que se constrói**.
>
> **Pré-requisito:** `01-motor-orientado-a-mensagem.md` precisa estar feito.
> Sem motor orientado a mensagem e sem o telefone na conversa, não há para onde o
> webhook entregar a mensagem que chega.
>
> Fontes oficiais no rodapé. Valores e limites conferidos em setembro de 2026 —
> a Twilio e a Meta mudam sem aviso; confirme antes de fechar contrato.
>
> **Estado: construído em 03/09/2026.** A Parte 2 descreve o que existe no
> código. O que mudou em relação ao plano original está marcado com
> *(na implementação)*.

---

## Por que Twilio primeiro

Do comparativo: a Twilio **não cobra mensalidade**, cobra US$ 0,005 por mensagem
em cada direção sobre a tarifa da Meta, e entrega uma biblioteca pronta em Python.
Custa mais por mensagem e menos por hora de trabalho. Com o volume da Marina esse
acréscimo é irrelevante no começo, e você descobre em dias se o produto para em pé.

A 360dialog só ganha da Twilio depois de vários clientes ativos, porque a
mensalidade dela é por número — cobrada mesmo com o número parado.

---

# Parte 1 — O que você faz

## Fase 1 — Sandbox (minutos, sem aprovação da Meta)

O Sandbox é um número compartilhado da Twilio para desenvolvimento. Serve para
validar o fluxo inteiro antes de existir número próprio.

1. **Criar a conta na Twilio.** No Console, anotar o **Account SID** e o **Auth
   Token**. O Auth Token é o segredo que valida a assinatura dos webhooks —
   trate-o como senha.
2. **Ativar o Sandbox.** Console → *Messaging* → *Try it out* → *Send a WhatsApp
   message*. Anotar o número do sandbox e o código de entrada
   (`join <duas-palavras>`).
3. **Fazer o opt-in do celular de teste.** Mandar `join <duas-palavras>` do seu
   WhatsApp para o número do sandbox. Isso não é burocracia da Twilio: o WhatsApp
   exige que a aplicação implemente opt-in explícito do usuário.
4. **Expor a máquina local em HTTPS público.** `ngrok http 8000` ou `cloudflared`.
   A Twilio **não conecta em HTTPS com certificado autoassinado** — precisa ser um
   endereço público com certificado válido.
5. **Apontar os webhooks no Sandbox:**
   - *When a message comes in* → `POST https://<seu-host>/webhooks/twilio/whatsapp`
   - *Status callback URL* → `POST https://<seu-host>/webhooks/twilio/status`
6. **Testar.** Mandar "oi" do celular e ver a Marina responder com a saudação e a
   pergunta do nome.

> O endereço do ngrok muda a cada reinício. Como a assinatura é calculada sobre a
> URL completa, trocar o endereço e esquecer de atualizar a configuração faz
> **toda** chamada ser rejeitada como inválida. É o erro mais comum desta fase.

## Fase 2 — Número próprio do escritório (semanas)

Não depende de código e é o gargalo comercial se começar tarde. Pode rodar em
paralelo com a Fase 1.

1. **Meta Business Manager do escritório.** O cliente precisa ter um, e é ele que
   passa pela **verificação da empresa** (Business Verification) na Meta. Leva
   dias e envolve documentos da empresa.
2. **Criar o WhatsApp Sender.** Console Twilio → *Messaging* → *Senders* →
   *WhatsApp senders*. Vincular à conta do WhatsApp Business (WABA) do cliente,
   escolher o número e submeter o **Display Name** (o nome que aparece no visor)
   à aprovação.
   - O número **não pode estar ativo em outro aplicativo do WhatsApp**. Se estiver
     em uso no WhatsApp comum ou no Business, a conta precisa ser apagada antes.
3. **Conhecer os limites de número por Business Manager:** conta não verificada,
   **2 números**; conta verificada, **até 20** (mais que isso, mediante pedido ao
   suporte). Contas verificadas suportam até 20 WABAs; Official Business Accounts,
   até 1.000.
4. **Templates (Content Template Builder).** Só são necessários para mensagem que
   a Marina **inicia** fora da janela de 24 h. Hoje ela só responde — e por isso
   **não precisa de nenhum template**. O primeiro será o lembrete de reunião, se e
   quando existir. Cada template é criado no Console, submetido à aprovação do
   WhatsApp e usado depois pelo `ContentSid`.
5. **Virar a chave.** Trocar o remetente na configuração (`TWILIO_WHATSAPP_FROM`)
   e apontar os webhooks do número real para os mesmos endereços. O sandbox pode
   continuar existindo para desenvolvimento.

### Checklist do que sai daí para o `.env`

| Variável | De onde vem |
|---|---|
| `TWILIO_ACCOUNT_SID` | Console, painel inicial |
| `TWILIO_AUTH_TOKEN` | Console, painel inicial |
| `TWILIO_WHATSAPP_FROM` | número do sandbox na fase 1, número do escritório na fase 2 |
| `TWILIO_URL_PUBLICA` | o endereço público exato configurado no Console |

---

# Parte 2 — O que se constrói

Uma peça só, isolada. O adaptador é a única parte que sabe que existe Twilio; se
um dia trocar para a Meta direta, a troca é local.

## Dependências novas (`pyproject.toml`)

- `fastapi` + `uvicorn` — o projeto não tem servidor HTTP hoje. Async nativo, que
  é o que combina com o engine e com o psycopg async.
- `twilio` — **só** pelo `RequestValidator`. A documentação de segurança da Twilio
  é explícita: *"Don't implement your own signature validation."*
- O envio usa `httpx`, que **já é dependência** do projeto e é async. Não vale
  puxar o cliente REST síncrono do SDK só para isso.

*(na implementação)* `python-multipart` **não** entrou. `request.form()` do
Starlette a exige mesmo para `x-www-form-urlencoded`, que é o único formato que a
Twilio manda aqui — o webhook lê o corpo com `parse_qsl` da biblioteca padrão.

## `app/channels/whatsapp_twilio.py` — o adaptador

Adaptador burro, `canal = "whatsapp"`, no contrato de `app/channels/base.py`.

**Entrada.** O webhook chega como `application/x-www-form-urlencoded`. Tradução:

| Campo da Twilio | Vira |
|---|---|
| `From` (`whatsapp:+5511...`) | a identidade da conversa (requisito A do doc 1) |
| `Body` | `InboundMessage.texto` |
| `NumMedia` > 0 | `InboundMessage.anexo` |
| `MediaUrl0`, `MediaContentType0` | `Anexo.origem` e `Anexo.mime` |
| `MessageSid` | chave de idempotência |
| `ProfileName`, `WaId` | registrados no log; **não** substituem a pergunta do nome |

A mídia recebida fica hospedada na Twilio e é baixada da `MediaUrl0` com
autenticação básica da conta (`AccountSid` : `AuthToken`), seguindo o redirect.

*(na implementação)* `Anexo.origem` guarda o **caminho do arquivo temporário
baixado**, e não a `MediaUrl0`. `LocalDocumentStorage` faz
`Path(anexo.origem).is_file()` e valida pela extensão (`app/storage/local.py:55`):
guardar a URL quebraria o arquivamento. Baixar no adaptador também é coerente com
a divisão do projeto — quem resolve o mundo é o adaptador, e a máquina só pergunta
se há anexo. O arquivo é apagado no fim do turno, depois de o armazenamento ter
copiado o que precisava.

> **Buraco conhecido: a copy da mídia recusada.** Mídia grande demais ou de tipo
> não aceito vira log e **não** vira `anexo_nao_resolvido`. O motivo é que a única
> mensagem que a máquina tem para esse caso, `erro_arquivo_nao_encontrado`, diz
> *"Confere se o caminho está completo — no Explorer dá para clicar com o botão
> direito"*: escrita para o terminal, e sem sentido no WhatsApp. O lead cai em
> "não entendi" e a Marina repete o pedido do contrato. Resolver exige chave nova
> em `messages.yaml` **e** um ramo novo em `machine.py` — fora do escopo desta
> entrega, que não toca o miolo.

**Saída.** `POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json`
com `From=whatsapp:+...`, `To=whatsapp:+...`, `Body` e `StatusCallback`.

> **Enviar uma mensagem de cada vez, aguardando o retorno de cada POST.** A Marina
> responde duas ou mais mensagens por turno (`boas_vindas` + `pergunta_nome`, por
> exemplo) e envios disparados em paralelo podem chegar fora de ordem no celular
> do lead.

## `app/web/webhook.py` — as rotas

**`POST /webhooks/twilio/whatsapp`** — a mensagem que chega.

1. **Validar `X-Twilio-Signature`.** HMAC-SHA1 com o Auth Token como chave, sobre
   a URL completa mais todos os parâmetros do formulário em ordem alfabética.
   Atrás do ngrok ou de qualquer proxy, a URL precisa ser montada a partir de
   `X-Forwarded-Proto` / `Host` **exatamente como a Twilio a chamou** — reescrever
   ou reencodar a URL invalida a assinatura. Assinatura inválida → 403, e nada é
   processado.
   *(A validação precisa tolerar parâmetros novos: a Twilio acrescenta campos sem
   aviso prévio. Usar o `RequestValidator` resolve isso de graça.)*
2. **Descartar `MessageSid` já visto**, para a mesma mensagem não virar dois
   passos da conversa.
3. **Processar fora do request** e responder vazio (204). Não devolvemos TwiML —
   quem envia é a REST API, que é o caminho que permite mandar várias mensagens em
   ordem e registrar cada uma na transcrição.
   *(A confirmar na doc antes de codar: o tempo que a Twilio espera pela resposta
   antes de registrar erro 11200, e se há retry ou apenas Fallback URL para o
   webhook de mensagem recebida.)*

**`POST /webhooks/twilio/status`** — o que aconteceu com o que enviamos.
Registra `MessageStatus` (`queued`, `sent`, `delivered`, `read`, `failed`,
`undelivered`) e o `ErrorCode` quando houver. É o "painel de erro" do artefato, em
forma de log estruturado — sem isso, mensagem que não chegou some sem ninguém ver.

## Entrypoint

`python -m app.whatsapp`, subindo o uvicorn por **`app.aio.executar`** — nunca
`asyncio.run` nem `uvicorn.run`, que criaria um loop próprio. No Windows o loop
padrão é o Proactor e o psycopg async não funciona nele: a conexão falha com
`InterfaceError` antes de qualquer query.

Ele recusa subir sem `TWILIO_ACCOUNT_SID` e `TWILIO_AUTH_TOKEN`: sem token, todo
webhook seria recusado por assinatura inválida — o sintoma mais confuso possível.

## Como testar sem ngrok e sem conta

`scripts/simular_webhook_twilio.py` assina um formulário com o mesmo
`RequestValidator` que o webhook usa para conferir, e faz o POST no servidor
local. Em um terminal:

```bash
python -m app.whatsapp
```

Em outro:

```bash
python scripts/simular_webhook_twilio.py --texto "oi" --sid SM1        # 204
python scripts/simular_webhook_twilio.py --texto "oi" --sid SM1        # 204, ignorada
python scripts/simular_webhook_twilio.py --texto "Pedro Guerra"        # avança
python scripts/simular_webhook_twilio.py --texto "oi" --sem-assinatura # 403
```

Com credenciais falsas o envio da resposta vira `envio_falhou` no `marina.log` —
o que também prova que uma falha de envio não derruba o turno. A conversa em si
sai em `python scripts/dump_lead.py --telefone +5511987654321`.

## Configuração nova (`app/config.py` e `.env.example`)

`twilio_account_sid`, `twilio_auth_token`, `twilio_whatsapp_from`,
`twilio_url_publica`, `twilio_validar_assinatura` (padrão `true`; só o teste
desliga), `whatsapp_host` e `whatsapp_porta`.

---

## Regras que a Twilio impõe ao negócio

**Janela de 24 horas.** A janela de atendimento abre quando o usuário manda uma
mensagem e vale por 24 h a partir da **última mensagem recebida**. Dentro dela,
texto livre. Fora dela, só template aprovado.

A conversa inteira da Marina cabe na janela, porque **quem começa é o lead** — é
isso que faz o custo por lead atendido tender a zero. O único uso previsto de
template é um lembrete de reunião no dia seguinte, que hoje não existe.

**Mídia: 16 MB por mensagem na Twilio** (JPG, JPEG, PNG, áudio, PDF).
*(na implementação)* `doc_max_bytes` passou de 20 MB para **16 MiB**: aceitar mais
só adiaria a recusa para um lugar onde a Marina não tem como avisar o lead. O
tamanho é conferido **durante** o download, para um arquivo enorme não encher o
disco antes de alguém reparar.

**Opt-in explícito.** O WhatsApp exige que a aplicação implemente opt-in do
usuário. Na prática da Marina isso já acontece — o lead escreve primeiro — e o
consentimento já é carimbado na saudação (`consentimento_em`), com opt-out
implementado (`RegistrarOptOut` / `RevogarOptOut`).

---

## O que não muda

`app/machine.py`, `app/messages.yaml`, `app/guards.py`, `app/parsers.py`, o outbox
e toda a integração com o `juridico`. `juridico_canal_contato` já vale
`"WHATSAPP"` e `Canal.WHATSAPP` já existe.

---

## Quando trocar de fornecedor

O gatilho: quando a conta da Twilio passar do que custaria uma mensalidade por
cliente na 360dialog. Aí sim vale trocar — e como o adaptador é uma peça só, a
troca é local. Enquanto isso, começar a verificação na Meta em paralelo é o que
destrava passar de 10 para 200 clientes novos por semana no cadastro embutido.

---

## Fontes

- [Twilio · WhatsApp API](https://www.twilio.com/docs/whatsapp/api) — endpoint,
  parâmetros, janela de 24 h, limites de número por Business Manager.
- [Twilio · Webhook request (mensageria)](https://www.twilio.com/docs/messaging/guides/webhook-request)
  — lista completa dos parâmetros de entrada e de status callback.
- [Twilio · Webhooks security](https://www.twilio.com/docs/usage/webhooks/webhooks-security)
  — `X-Twilio-Signature`, HMAC-SHA1, `RequestValidator`, HTTPS obrigatório.
- [Twilio · Preços do WhatsApp](https://www.twilio.com/en-us/whatsapp/pricing)
- [Meta · Preços da plataforma](https://developers.facebook.com/docs/whatsapp/pricing/)
