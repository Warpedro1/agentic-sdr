# Marina — Especificação de Requisitos de Software

> **Norma:** ISO/IEC/IEEE 29148:2018, estrutura de SRS (cláusula 9.6).
> **Versão:** 1.1 · 15/09/2026 · **Situação:** linha de base + canal Z-API.
>
> **Fonte da verdade:** o código em `main` e a suíte `pytest` (453 testes). Os
> requisitos foram levantados do comportamento implementado e rastreados até o teste
> que o comprova. Onde este documento e `marina-regras-de-negocio.md` divergem, vale
> este — as divergências estão no [Apêndice C](#apêndice-c--divergências-com-documentos-anteriores).

---

## Como ler

| Convenção | Significado |
|---|---|
| **deve** / **não deve** | Obrigatório / proibido. Não há requisito opcional na seção 3. |
| `RF-<área>-NN` | Requisito funcional |
| `RNF-<atributo>-NN` | Requisito não funcional (qualidade) |
| `RI-NN` · `RD-NN` · `RR-NN` | Interface externa · dados · restrição de projeto |
| `PROP-NN` | Requisito proposto, ainda não implementado ([Apêndice B](#apêndice-b--requisitos-propostos-e-pendências)) |
| **[A DEFINIR]** | Requisito reconhecido cujo valor ainda não foi decidido. Tem responsável no [Apêndice B.2](#b2-valores-a-definir) |

**Situação.** Todo requisito da seção 3 está **implementado**, salvo os marcados
[A DEFINIR] e as ressalvas escritas no próprio requisito.

**Verificação** (29148, 6.4.9): **T** = teste automatizado, citado como
`arquivo::caso` (arquivos em `tests/`, sem o prefixo `test_` do arquivo);
**I** = inspeção do código ou da configuração; **D** = demonstração com os scripts
operacionais; **A** = análise.

**Identificadores são estáveis.** Requisito que deixa de valer é marcado como
retirado, e seu ID não é reaproveitado.

---

## 1. Introdução

### 1.1 Propósito

Especificar o que a Marina deve fazer e com que qualidade, de forma verificável, para
servir de base a: aceite de mudanças, revisão pelo negócio, e comparação com uma
versão futura baseada em IA — a Marina atual é a linha de base dessa comparação.

### 1.2 Escopo

A **Marina** (repositório `agentic-sdr`) é uma automação **determinística** de SDR
(*Sales Development Representative*). Ela conversa com pessoas interessadas em revisão
ou renegociação de contrato de empréstimo, **captura e qualifica o lead** e entrega
**uma reunião agendada** com um consultor humano, registrando tudo no CRM da empresa.

**Dentro do escopo:** conversa por terminal e por WhatsApp (Twilio, Meta Cloud API e Z-API);
coleta e validação de dados; guardrails; reconhecimento de retorno e descadastro;
recebimento e arquivamento de contrato; cálculo de horários e reserva; entrega ao CRM
`base-crm-juridico`; atendimento de várias empresas por uma instalação.

**Fora do escopo, por decisão:** qualquer IA ou LLM gerando texto; interpretação de
datas em linguagem natural; leitura do conteúdo do contrato; envio real de e-mail;
bloqueios e férias de consultor; painel web próprio; atendimento por e-mail.

### 1.3 Visão geral do produto

#### 1.3.1 Perspectiva do produto

```
                ┌─────────────────────────── Marina ───────────────────────────┐
 Lead ──► canal │ adaptador de canal ──► máquina de decisão ──► engine          │
 (WhatsApp,     │ (traduz o mundo)       (pura: decide)         (executa I/O)   │
  terminal)  ◄──│                                                  │            │
                │                         catálogo de textos       ├─► PostgreSQL│
                │                         (messages.yaml)          ├─► disco    │
                │                                                  └─► fila ──► CRM
                └──────────────────────────────────────────────────────────────┘   (base-crm-juridico)
```

A Marina é um sistema independente. Ela depende de: um provedor de WhatsApp (Twilio,
Meta ou Z-API), um PostgreSQL, um disco local para contratos e, opcionalmente, uma
instalação do `base-crm-juridico` por empresa atendida.

#### 1.3.2 Funções do produto

1. Conduzir a conversa de qualificação, do "olá" à reunião confirmada.
2. Interpretar respostas por regras fixas e recusar o que for duvidoso.
3. Recusar assuntos financeiros e respeitar comandos de saída e de atendimento humano.
4. Reconhecer quem volta, retomar conversas e respeitar descadastros.
5. Receber e arquivar o contrato sem lê-lo.
6. Oferecer horários reais e reservar sem dupla marcação.
7. Registrar lead, qualificação e reunião no CRM sem nunca fazer o lead esperar.
8. Atender várias empresas com isolamento de dados entre elas.
9. Gravar a conversa inteira como material de auditoria e de comparação.

#### 1.3.3 Características dos usuários

| Usuário | Características | Como interage |
|---|---|---|
| **Lead** | Pessoa física com empréstimo; usa WhatsApp; pode não ter o contrato em mãos; sem conhecimento técnico | Conversa com a Marina |
| **Consultor / comercial** | Funcionário da empresa com acesso ao CRM | Assume a reunião em aberto no CRM; nunca fala com a Marina |
| **Administrador da empresa** | Gestor no CRM | Libera tarefas presas; não atende |
| **Revisor de textos** | Time da Leve, sem conhecimento de código | Edita `app/messages.yaml` |
| **Operador técnico** | Mantém a instalação | `.env`, scripts em `scripts/`, painéis da Twilio e da Meta |
| **Avaliador** | Compara versões | Lê a transcrição gravada |

#### 1.3.4 Limitações

- **Política do WhatsApp:** a Marina só responde (não inicia conversa), então opera
  dentro da janela de 24 h aberta pelo lead. Pela Meta, **número estrangeiro não
  entrega mensagem a usuário brasileiro** (erro 130497), nem como resposta —
  observado em 11/09/2026.
- **Z-API não é a API oficial:** conecta o número como aparelho do WhatsApp Web, por
  QR code. O número precisa estar ativo num aplicativo WhatsApp, a instância cai se
  o celular perder a sessão, e o WhatsApp pode restringir número que automatiza envio.
- **LGPD** (Lei 13.709/2018): tratamento de dados pessoais de leads.
- **CRM:** o `base-crm-juridico` exige telefone para criar negociação e aceita um
  conjunto fixo de 11 fontes.
- **Processo único:** cadastrar empresa ou trocar credencial exige reiniciar o processo.

### 1.4 Definições

| Termo | Definição |
|---|---|
| **Lead** | Pessoa em atendimento. Só passa a existir no banco quando um contato é validado. |
| **Contato** | Telefone ou e-mail validado. Um basta para cadastrar. |
| **Conversa** | Sequência de mensagens com um estado atual. No máximo uma aberta por identidade por empresa. |
| **Identidade** | Endereço de quem escreve no canal (no WhatsApp, o número). |
| **Estado terminal** | `AGENDADO`, `OPT_OUT` ou `ESCALADO_HUMANO`. Encerra a conversa. |
| **Estado interno** | `RESOLVENDO_IDENTIDADE`, `CARREGANDO_HORARIOS` ou `RESERVANDO`. A Marina não espera digitação neles. |
| **Escalonamento** | Encerrar a conversa entregando o lead a um humano. |
| **Descadastro (opt-out)** | Pedido de não ser mais contatado; gera registro de supressão. |
| **Janela de retomada** | Tempo em que uma conversa interrompida pode ser continuada. Padrão: 24 h. |
| **Horário ofertado** | Início de reunião em que ao menos um consultor está livre. |
| **Reserva** | Consumo da vaga de um consultor específico. É contagem de capacidade, não atribuição. |
| **Empresa** | Uma instalação do CRM e seus funcionários. Identificada pelo número de WhatsApp que recebe a mensagem. |
| **Fila de entrega** | Tabela local (`crm_outbox`) com o que precisa chegar ao CRM. |
| **Negociação / Tarefa** | No CRM, o lead vira negociação; a reunião vira tarefa do tipo reunião. |
| **Catálogo** | `app/messages.yaml`: única origem de texto enviado ao lead. |
| **E.164** | Formato internacional de telefone: `+5511987654321`. |
| **Nono dígito** | Dígito 9 inicial do celular brasileiro, que o WhatsApp omite em contas antigas. |
| **Id externo** | Identificador da mensagem no provedor: `MessageSid` (Twilio) ou `wamid` (Meta). |

---

## 2. Referências

| Referência | Uso |
|---|---|
| ISO/IEC/IEEE 29148:2018 — *Requirements engineering* | Estrutura e qualidade dos requisitos |
| Lei 13.709/2018 (LGPD) | Requisitos de privacidade |
| `docs/SDR/marina-regras-de-negocio.md` | Regras de negócio (parcialmente superado — Apêndice C) |
| `docs/empresas/01-uma-marina-varias-empresas.md` | Decisões do atendimento a várias empresas |
| `docs/whatsapp/01-motor-orientado-a-mensagem.md`, `02-integracao-twilio.md` | Canal WhatsApp |
| `C:\Dev\JURIDICO\base-crm-juridico\routes\api.php` | Contrato da API do CRM |
| Meta — WhatsApp Cloud API; Twilio — Messaging API | Interfaces dos provedores |

---

## 3. Requisitos específicos

### 3.1 Interfaces externas

| ID | Requisito | Verificação |
|---|---|---|
| RI-01 | A Marina deve oferecer atendimento por terminal (`python -m app.cli`), aceitando `--empresa <slug>` para escolher a empresa atendida. | D · T: `integracao_resiliencia::test_cli_empresa_integrada_usa_a_fila` |
| RI-02 | A Marina deve receber mensagens da Twilio em `POST /webhooks/twilio/whatsapp` e avisos de entrega em `POST /webhooks/twilio/status`, e enviar respostas pela API REST de mensagens da Twilio. | T: `webhook_twilio::test_primeira_mensagem_abre_a_conversa_e_responde` |
| RI-03 | A Marina deve atender o aperto de mão da Meta em `GET /webhooks/meta/whatsapp`, receber mensagens em `POST /webhooks/meta/whatsapp` e enviar respostas em `POST /{versão}/{phone_number_id}/messages`, com a versão fixada por configuração (`META_API_VERSAO`). | T: `webhook_meta::test_aperto_de_mao_devolve_o_desafio` · `whatsapp_meta::test_envio_monta_a_chamada_da_graph_api` |
| RI-04 | A Marina deve expor `GET /saude`, respondendo 200 sem autenticação. | T: `webhook_twilio::test_saude_responde_sem_assinatura` |
| RI-05 | A Marina deve se integrar ao CRM apenas pelas rotas `/api/integracao/` (`contexto`, `negociacoes`, `negociacoes/{id}/observacoes`, `negociacoes/{id}/tarefas`, `usuarios`), com token de portador e cabeçalho `Accept: application/json` em toda chamada. | T: `juridico_cliente::test_caminhos_das_chamadas` · `juridico_cliente::test_toda_requisicao_leva_token_e_pede_json` |
| RI-06 | Toda chamada ao CRM deve ter tempo limite configurável (`JURIDICO_TIMEOUT_S`, padrão 15 s). | I: `app/config.py` |
| RI-07 | O texto enviado ao lead deve vir do catálogo `app/messages.yaml`, editável sem alterar código. | T: `guards::test_todas_as_chaves_usadas_existem` |
| RI-08 | A operação deve dispor de scripts para: cadastrar e listar empresas, importar consultores do CRM, configurar agenda, forçar e reprocessar a fila, consultar um lead e simular webhook assinado. | D: `scripts/` |
| RI-09 | A Marina deve receber da Z-API em `POST /webhooks/zapi/{segredo}` (mensagens e avisos de status, entrega e desconexão) e enviar respostas em `POST /instances/{instância}/token/{token}/send-text`, com o cabeçalho `Client-Token` quando configurado. | T: `webhook_zapi::test_primeira_mensagem_abre_a_conversa_e_responde_pela_instancia` · `whatsapp_zapi::test_envio_monta_a_chamada_da_z_api` |
| RI-10 | O operador deve conseguir conferir a conexão da instância e apontar todos os webhooks dela para a Marina com um único comando. | D: `scripts/configurar_webhook_zapi.py` · T: `whatsapp_zapi::test_apontar_webhooks_grava_o_endereco_em_todos` |

### 3.2 Requisitos funcionais

#### 3.2.1 Conversa e coleta (RF-CONV)

| ID | Requisito | Justificativa | Verificação |
|---|---|---|---|
| RF-CONV-01 | Ao iniciar uma conversa, a Marina deve enviar a saudação — que informa a finalidade do uso dos dados e o comando SAIR — seguida da pergunta do nome. | Transparência exigida pela LGPD na primeira interação. | T: `machine::test_inicio_sauda_pergunta_o_nome_e_grava_consentimento` · `machine::test_boas_vindas_avisa_sobre_o_uso_dos_dados` |
| RF-CONV-02 | A Marina deve registrar o instante do consentimento no momento da saudação, e esse registro deve sobreviver ao encerramento do processo entre mensagens. | O carimbo precisa ser defensável; o cadastro nasce várias mensagens depois. | T: `engine_atender::test_consentimento_sobrevive_a_reidratacao` |
| RF-CONV-03 | A Marina deve coletar, nesta ordem: nome, telefone, e-mail, origem, tipo de contrato, posse do contrato, arquivo do contrato (se possuir), escolha de horário e confirmação. | Ordem aprovada pelo negócio. | T: `engine::test_caminho_feliz_agenda_e_grava_tudo` · `e2e_transcricao::test_transcricao_do_caminho_feliz` |
| RF-CONV-04 | Havendo telefone válido, a Marina não deve perguntar o e-mail. | Cada pergunta a mais aumenta a desistência; um contato basta. | T: `machine::test_com_telefone_a_marina_nao_pergunta_o_email` |
| RF-CONV-05 | Quando o canal informar o número de quem escreve, a Marina não deve perguntar o telefone e deve usar o número informado pelo canal. Sem essa informação (terminal), deve perguntar. | O número já chega na mensagem; perguntar pediria um dado que a conversa carrega. | T: `machine::test_com_o_numero_do_canal_ela_nao_pergunta_o_telefone` · `machine::test_sem_numero_do_canal_ela_continua_perguntando` |
| RF-CONV-06 | O lead deve ser cadastrado somente quando houver nome e ao menos um contato validado. | A deduplicação depende do contato. | T: `engine::test_lead_so_nasce_depois_do_telefone` · `engine::test_lead_nasce_so_com_email_quando_o_telefone_nao_sai` |
| RF-CONV-07 | Após o número máximo de tentativas sem um telefone válido, a Marina deve pedir o e-mail com uma pergunta própria, em vez de escalar. | Um lead cadastrável não deve ser perdido por causa do telefone. | T: `machine::test_telefone_que_nao_sai_tenta_o_email_em_vez_de_escalar` |
| RF-CONV-08 | Após o número máximo de tentativas sem entender a origem, a Marina deve avisar e seguir sem ela. | Origem é dado de marketing e não qualifica o caso. | T: `machine::test_fonte_incompreensivel_segue_sem_ela` · `engine::test_fonte_incompreensivel_nao_trava_o_atendimento` |
| RF-CONV-09 | Tipo de contrato e posse do contrato devem ser obrigatórios: esgotadas as tentativas, a Marina deve escalar. | Sem eles o consultor entra na reunião às cegas. | I: `CAMPOS_DISPENSAVEIS` em `app/machine.py` · T: `machine::test_limite_de_tentativas_respeita_a_politica` |
| RF-CONV-10 | Diante de resposta incompreensível, a Marina deve repetir a pergunta do ponto atual — com mensagem de erro específica para telefone e tipo de contrato — e, ao atingir `MAX_TENTATIVAS` (padrão 3) no mesmo ponto, escalar. | Sem limite, o lead fica em laço e desiste em silêncio. | T: `machine::test_nome_invalido_repete_a_pergunta` · `machine::test_telefone_invalido_usa_o_erro_especifico` · `machine::test_todo_estado_de_espera_sabe_repetir_a_propria_pergunta` |
| RF-CONV-11 | Ao escalar sem nenhum contato conhecido, a Marina não deve prometer retorno e deve pedir que a pessoa volte a chamar. | Prometer retorno sem ter para onde retornar seria mentira. | T: `machine::test_sem_telefone_e_sem_email_ai_sim_escala` · `machine::test_escalonamento_com_telefone_promete_o_retorno` |
| RF-CONV-12 | Um estado terminal não deve transicionar; mensagem recebida depois dele deve abrir uma conversa nova. | — | T: `machine::test_terminais_nao_transicionam` · `engine_atender::test_conversa_encerrada_nao_recebe_mensagem_nova` |
| RF-CONV-13 | Nenhuma mensagem deve ser enviada com variável de texto vazia ou ausente. | Evita frases como "alguém te chama no ." | T: `machine::test_nenhuma_mensagem_sai_com_variavel_vazia_pendurada` · `guards::test_render_reclama_de_variavel_faltando` |

#### 3.2.2 Interpretação de respostas (RF-VAL)

| ID | Requisito | Justificativa | Verificação |
|---|---|---|---|
| RF-VAL-01 | O nome deve ser aceito como texto livre com ao menos 2 caracteres, não composto só de dígitos e pontuação, truncado em 160 caracteres. | — | T: `parsers::test_nome_aceito` · `parsers::test_nome_recusado` · `parsers::test_nome_truncado_no_limite` |
| RF-VAL-02 | O telefone digitado deve ser aceito em qualquer formatação, exigir DDD brasileiro existente, recusar dígitos todos iguais, recusar formato duvidoso e ser gravado em E.164. | Perguntar de novo é melhor que gravar errado. | T: `parsers::test_telefone_normalizado` · `parsers::test_telefone_recusado` |
| RF-VAL-03 | O telefone informado pelo canal deve ser convertido para E.164 repondo o nono dígito do celular brasileiro (`+558191234567` → `+5581991234567`); número não reconhecível não deve ser usado como telefone. | O WhatsApp omite o 9 em contas antigas. | T: `parsers::test_telefone_do_canal_repoe_o_nono_digito` · `parsers::test_telefone_do_canal_recusa_o_que_nao_reconhece` |
| RF-VAL-04 | O e-mail deve ser extraído do texto e gravado em minúsculas. | — | T: `parsers::test_email_aceito` · `parsers::test_email_recusado` |
| RF-VAL-05 | Origem e tipo de contrato devem aceitar o número do menu ou a palavra, e gravar o valor interpretado junto do texto original. | Ninguém perde o que a pessoa escreveu. | T: `parsers::test_fonte_guarda_texto_original` · `parsers::test_tipo_contrato_guarda_texto_original` · `parsers::test_cartao_vence_pessoal_quando_a_frase_cita_os_dois` |
| RF-VAL-06 | Em perguntas de sim/não, a negação deve prevalecer: "não tenho" deve ser lido como não. | — | T: `parsers::test_nao_tenho_nao_e_lido_como_sim` |
| RF-VAL-07 | O horário deve ser escolhido apenas pelo número do menu; data ou hora em linguagem natural e opção inexistente devem ser tratadas como resposta incompreensível. | É onde uma automação erra a data e o consultor perde a reunião. | T: `machine::test_horario_em_linguagem_natural_nao_e_interpretado` · `machine::test_horario_fora_do_menu_repete` |

#### 3.2.3 Guardrails e comandos (RF-GRD)

| ID | Requisito | Justificativa | Verificação |
|---|---|---|---|
| RF-GRD-01 | Em qualquer estado, as palavras `sair`, `parar`, `cancelar` ou `encerrar` devem encerrar a conversa como descadastro. | — | T: `machine::test_sair_funciona_em_qualquer_estado` · `guards::test_comando_sair` |
| RF-GRD-02 | Em qualquer estado, as palavras `atendente`, `humano`, `pessoa` ou `consultor` devem escalar a conversa. | — | T: `machine::test_atendente_funciona_em_qualquer_estado` |
| RF-GRD-03 | Em qualquer estado, `voltar` ou `volta` deve retornar à pergunta anterior; sem pergunta anterior, deve repetir a atual. | — | T: `machine::test_voltar_retorna_ao_estado_anterior` · `machine::test_voltar_sem_historico_fica_onde_esta` · `engine::test_voltar_funciona_ponta_a_ponta` |
| RF-GRD-04 | Comandos devem ser reconhecidos por palavra inteira, não por trecho. | "pessoal" (resposta do menu) não pode escalar por conter "pessoa". | T: `guards::test_pessoal_nao_dispara_escalonamento` |
| RF-GRD-05 | Mensagem que contenha, por trecho e sem distinção de acento, `quanto custa`, `honorári`, `abusiv`, `quanto vou receber`, `taxa`, `juros`, `valor`, `cobra` ou `preço` deve receber o texto fixo de desvio seguido da pergunta atual, sem contar como tentativa. | A Marina nunca opina sobre dinheiro sem um consultor olhar o contrato. | T: `machine::test_fora_de_escopo_desvia_e_devolve_ao_fluxo` · `machine::test_fora_de_escopo_nao_conta_como_erro_de_parsing` · `guards::test_acento_nao_escapa_do_guardrail` |
| RF-GRD-06 | Nenhuma mensagem enviada ao lead deve conter valor monetário ou percentual. | Garantia contra edição descuidada do catálogo. | T: `e2e_transcricao::test_a_marina_nunca_fala_de_dinheiro` · `machine::test_fora_de_escopo_nao_da_numero` |
| RF-GRD-07 | As regras devem ser aplicadas nesta ordem: anexo dispensa os guardrails de texto; depois comandos; depois assunto proibido; por fim a interpretação do estado. | — | T: `machine::test_anexo_pula_o_guardrail_de_escopo` |

#### 3.2.4 Identidade, retorno e descadastro (RF-ID)

| ID | Requisito | Justificativa | Verificação |
|---|---|---|---|
| RF-ID-01 | A Marina deve reconhecer lead já cadastrado na mesma empresa pelo telefone e, na falta dele, pelo e-mail. | — | T: `engine::test_lead_que_volta_e_reconhecido` |
| RF-ID-02 | Se a última conversa do lead não terminou e foi há menos de `JANELA_RETOMADA_HORAS` (padrão 24 h), a Marina deve oferecer continuar ou começar do zero; continuando, deve voltar ao ponto exato em que parou. | — | T: `machine::test_reconhecimento_conversa_recente_oferece_retomar` · `engine_atender::test_retomada_volta_ao_estado_exato_da_conversa_anterior` |
| RF-ID-03 | "Começar do zero" deve refazer origem, tipo e posse do contrato, mantendo nome e contato. | Quem recomeça quer rever o caso, não redigitar o telefone. | T: `machine::test_retomada_conversa_recusada_refaz_a_qualificacao` |
| RF-ID-04 | Retomar uma conversa parada na escolha ou confirmação de horário deve recalcular os horários. | A lista ofertada antes não existe mais. | I: `ESTADOS_QUE_DEPENDEM_DE_SLOTS` em `app/machine.py` |
| RF-ID-05 | Lead reconhecido fora da janela deve receber a saudação de retorno e ser perguntado apenas sobre o que ainda falta. | — | T: `machine::test_reconhecimento_conversa_antiga_pula_o_que_ja_sabe` · `machine::test_reconhecimento_antigo_para_no_primeiro_campo_faltante` |
| RF-ID-06 | O nome informado por um lead reconhecido não deve sobrescrever o nome cadastrado. | Um erro de digitação não pode corromper o cadastro. | T: `engine::test_nome_divergente_nao_sobrescreve_o_cadastro` |
| RF-ID-07 | O descadastro deve gerar registro de supressão somente se houver telefone ou e-mail conhecido. | Bloquear um desconhecido não significa nada. | T: `machine::test_sair_antes_do_telefone_nao_registra_opt_out` · `engine::test_sair_registra_opt_out` |
| RF-ID-08 | Contato suprimido que volta a escrever deve ser perguntado se quer retomar antes de qualquer outra pergunta; aceitando, a supressão deve ser revogada; recusando, a conversa deve encerrar. | — | T: `machine::test_reconhecimento_opt_out_ativo_pede_confirmacao_antes_de_continuar` · `engine::test_retomar_opt_out_revoga_o_registro` · `machine::test_retomada_opt_out_recusada_mantem_encerrado` |
| RF-ID-09 | Numa conversa por mensagem avulsa (WhatsApp), conversa aberta parada há mais que a janela de retomada deve ser encerrada, e a nova mensagem deve abrir outra conversa. | — | T: `engine_atender::test_conversa_parada_ha_tempo_demais_recomeca` · `engine_atender::test_dentro_da_janela_a_conversa_continua` |

#### 3.2.5 Contrato do lead (RF-DOC)

| ID | Requisito | Justificativa | Verificação |
|---|---|---|---|
| RF-DOC-01 | Se o lead tiver o contrato, a Marina deve pedir o envio e aceitar `pular`, `pula`, `depois` ou `skip` para seguir sem ele. | — | T: `machine::test_tem_contrato_sim_pede_o_arquivo` · `machine::test_pular_o_envio` |
| RF-DOC-02 | O arquivo recebido deve ser arquivado sem leitura do conteúdo, em diretório do lead, com nome gerado, registrando tipo e tamanho. | A análise do contrato é sempre humana. | T: `engine::test_documento_e_arquivado_e_registrado` |
| RF-DOC-03 | Só devem ser aceitos arquivos não vazios, de até `DOC_MAX_BYTES` (padrão 16 MiB) e com extensão em `DOC_EXTENSOES` (padrão pdf, jpg, jpeg, png, doc, docx); mídia de tipo não aceito não deve ser baixada. | 16 MiB é o teto da Twilio por mensagem. | T: `whatsapp_adaptador::test_arquivo_acima_do_limite_e_abortado_no_meio` · `whatsapp_meta::test_midia_de_tipo_nao_aceito_nem_chega_a_ser_baixada` · `whatsapp_zapi::test_midia_de_tipo_nao_aceito_nem_chega_a_ser_baixada` |
| RF-DOC-04 | A confirmação de recebimento deve dizer que a análise é feita por um consultor, nunca automaticamente. | — | T: `machine::test_contrato_recebido_diz_que_a_analise_e_humana` |
| RF-DOC-05 | Caminho de arquivo inexistente (terminal) deve receber mensagem específica, não "não entendi". | Evita gastar as tentativas às cegas. | T: `engine::test_caminho_errado_diz_o_que_houve_em_vez_de_nao_entendi` |
| RF-DOC-06 | Falha ao baixar uma mídia não deve impedir o processamento da mensagem. | — | T: `whatsapp_adaptador::test_midia_que_nao_baixa_nao_derruba_a_mensagem` |

#### 3.2.6 Agendamento (RF-AGD)

| ID | Requisito | Justificativa | Verificação |
|---|---|---|---|
| RF-AGD-01 | Os horários devem ser gerados das regras de disponibilidade dos consultores **ativos da empresa**, por dia da semana e faixa em horário local, fatiados em duração + intervalo, incluindo só horários que caibam inteiros na faixa. | Regras de agenda são escritas no fuso do consultor. | T: `disponibilidade::test_janela_de_9_as_18_rende_sete_horarios` · `disponibilidade::test_horario_que_nao_cabe_inteiro_fica_de_fora` · `disponibilidade::test_consultor_inativo_nao_gera_horario` |
| RF-AGD-02 | Devem ser excluídos horários ocupados, horários com menos de `ANTECEDENCIA_MINIMA_HORAS` (padrão 2 h) a partir de agora e horários além de `HORIZONTE_DIAS` (padrão 7). | — | T: `disponibilidade::test_horario_ja_ocupado_some_da_lista` · `engine::test_horario_ofertado_respeita_antecedencia` |
| RF-AGD-03 | As agendas devem ser mescladas: cada horário aparece uma vez, se ao menos um consultor estiver livre, e os primeiros `SLOTS_OFERTADOS` (padrão 5) devem ser oferecidos. | O lead escolhe horário, não pessoa. | T: `engine::test_slots_mesclam_as_agendas_sem_atribuir_ninguem` |
| RF-AGD-04 | Sem horário disponível no horizonte, a Marina deve informar e escalar. | — | T: `machine::test_sem_horarios_escala_para_humano` · `engine::test_sem_consultor_nenhum_escala_para_humano` |
| RF-AGD-05 | O horário escolhido deve ser confirmado antes da reserva; recusando, a lista deve ser recalculada e oferecida de novo. | — | T: `machine::test_confirmacao_reserva` · `machine::test_confirmacao_recusada_reoferta` |
| RF-AGD-06 | Nenhuma mensagem ao lead deve citar o nome de um consultor; a confirmação deve citar data, hora e "um dos nossos consultores". | A reunião nasce sem dono; prometer um nome é prometer quem pode não aparecer. | T: `machine::test_confirmacao_fala_do_horario_e_nao_de_uma_pessoa` |
| RF-AGD-07 | A reserva deve consumir a vaga do consultor livre de menor carga (menos agendamentos futuros; empate: parado há mais tempo) e, se ele for tomado entre a oferta e a confirmação, tentar o próximo livre sem nova pergunta ao lead. | — | T: `carga::test_ganha_quem_tem_menos_agendamentos` · `carga::test_empate_desempata_por_quem_esta_parado_ha_mais_tempo` · I: laço sobre `consultores` na reserva, em `app/engine.py` |
| RF-AGD-08 | Esgotados os consultores do horário, o lead deve receber o aviso de horário preenchido e a lista recalculada, sem tratamento de erro. | É o caminho normal do segundo lead. | T: `machine::test_reserva_perdida_reoferta_a_lista_recalculada` · `reserva::test_dois_leads_ao_mesmo_tempo_so_um_agenda` |
| RF-AGD-09 | A mensagem final deve citar o envio da confirmação por e-mail somente se houver e-mail. | — Ver PROP-04. | T: `machine::test_agendado_sem_email_nao_promete_confirmacao_por_email` |

#### 3.2.7 Integração com o CRM (RF-CRM)

| ID | Requisito | Justificativa | Verificação |
|---|---|---|---|
| RF-CRM-01 | Com a integração ativa para a empresa, a Marina deve registrar no CRM: o lead ao validar o contato (negociação), a qualificação (observação na negociação) e a reunião confirmada (tarefa do tipo reunião). | — | T: `juridico_entregador::test_entrega_lead_qualificacao_e_agendamento` |
| RF-CRM-02 | A negociação deve levar o identificador do lead como referência externa, e uma negociação já existente para essa referência deve contar como entregue. | Tempo esgotado após gravação remota é indistinguível de falha; reenviar não pode duplicar. | T: `juridico_mapeamento::test_payload_da_negociacao_leva_a_referencia_externa` · `juridico_entregador::test_negociacao_ja_conhecida_conta_como_entrega` |
| RF-CRM-03 | A tarefa da reunião não deve indicar responsável e deve identificar o lead (nome e contato) no assunto, limitado a 180 caracteres, e na observação. | A reunião fica em aberto para o comercial assumir. | T: `integracao_resiliencia::test_reuniao_vai_como_tarefa_em_aberto` · `juridico_mapeamento::test_tarefa_trunca_assunto_no_limite_da_coluna` |
| RF-CRM-04 | A origem deve ser traduzida para uma das fontes aceitas pelo CRM; o que não couber em campo (tipo de contrato, posse, origem real, canal) deve ir escrito na observação. | Nada se perde em silêncio. | T: `juridico_mapeamento::test_toda_fonte_vira_valor_que_o_juridico_aceita` · `juridico_mapeamento::test_qualificacao_registra_a_origem_real_quando_a_traducao_perde` |
| RF-CRM-05 | O telefone deve ser enviado ao CRM sem o código do país. | É o formato em que o CRM busca contato existente. | T: `juridico_mapeamento::test_telefone_perde_o_codigo_do_pais` |
| RF-CRM-06 | A entrega deve respeitar a ordem por lead: a falha de um item deve reter os itens seguintes do mesmo lead, sem reter os de outros leads. | Agendamento não pode chegar antes do lead existir. | T: `juridico_entregador::test_falha_no_lead_segura_os_itens_seguintes_dele` · `juridico_entregador::test_um_lead_travado_nao_trava_a_fila_de_outro` |
| RF-CRM-07 | Falha temporária (rede, erro 5xx, 401, 403, resposta sem id) deve gerar nova tentativa com espera de 30 s dobrando até 30 min, limitada a `OUTBOX_MAX_TENTATIVAS` (padrão 5); falha permanente (404, 422) deve retirar o item da fila na hora. | Erro permanente não melhora com repetição e precisa virar algo que uma pessoa olha. | T: `juridico_cliente::test_tabela_de_recuperabilidade` · `juridico_entregador::test_erro_permanente_nao_fica_tentando_para_sempre` |
| RF-CRM-08 | Com a chave geral `JURIDICO_ATIVO=false` ou empresa sem credencial, a Marina deve atender normalmente sem enviar nada ao CRM. | — | T: `integracao_resiliencia::test_a_chave_geral_desliga_todas_as_empresas` · `integracao_resiliencia::test_cli_empresa_sem_credencial_cai_no_noop` |
| RF-CRM-09 | O operador deve conseguir forçar a entrega, listar itens que esgotaram tentativas e reprocessá-los. | — | D: `scripts/flush_outbox.py` |

#### 3.2.8 Canal WhatsApp (RF-WA)

| ID | Requisito | Justificativa | Verificação |
|---|---|---|---|
| RF-WA-01 | A primeira mensagem de uma identidade deve abrir a conversa e disparar a saudação, sem ser consumida como resposta. | — | T: `engine_atender::test_primeira_mensagem_abre_a_conversa_e_so_saúda` |
| RF-WA-02 | Mensagem com id externo já processado não deve gerar novo passo da conversa. | Provedores reentregam. | T: `webhook_twilio::test_message_sid_repetido_nao_vira_dois_passos` · `webhook_meta::test_mensagem_repetida_nao_vira_dois_passos` · `webhook_zapi::test_mensagem_repetida_nao_vira_dois_passos` |
| RF-WA-03 | Deve existir no máximo uma conversa aberta por identidade por empresa, e mensagens simultâneas da mesma identidade devem ser processadas em série. | — | T: `engine_atender::test_duas_mensagens_juntas_nao_duplicam_a_conversa` |
| RF-WA-04 | A resposta deve sair pelo número da empresa que recebeu a mensagem e pelo mesmo provedor por onde ela chegou. | Senão o lead recebe mensagem de número para o qual não escreveu. | T: `webhook_twilio::test_a_resposta_sai_pelo_numero_da_empresa` · `webhook_meta::test_primeira_mensagem_abre_a_conversa_e_responde_pelo_numero_da_empresa` · `webhook_zapi::test_primeira_mensagem_abre_a_conversa_e_responde_pela_instancia` |
| RF-WA-05 | Pela Meta, a resposta a celular brasileiro deve ser endereçada ao número com o nono dígito; números de outros países devem ser respondidos como chegaram. | A lista de destinatários do número de teste compara o texto literal. | T: `webhook_meta::test_numero_de_fora_do_brasil_recebe_resposta_como_chegou` |
| RF-WA-06 | Avisos de entrega devem ser registrados; falhas devem registrar código e motivo do provedor; aviso de entrega não deve abrir conversa. | É o único lugar onde o motivo de uma mensagem não entregue existe. | T: `webhook_twilio::test_status_de_falha_e_registrado` · `webhook_meta::test_aviso_de_entrega_nao_abre_conversa` · `whatsapp_meta::test_o_motivo_da_meta_chega_ao_log` |
| RF-WA-07 | Mensagens sem conteúdo tratável (reação, figurinha, localização, texto vazio) devem ser ignoradas sem contar tentativa. | Reagir com emoji não pode escalar o lead. | T: `whatsapp_meta::test_reacao_nao_queima_tentativa_do_lead` · `whatsapp_adaptador::test_mensagem_vazia_nao_vira_texto_em_branco` · `whatsapp_zapi::test_reacao_figurinha_e_localizacao_nao_viram_mensagem` |
| RF-WA-08 | Falha no envio de uma resposta não deve interromper o processamento do turno. | — Ver PROP-05. | T: `whatsapp_meta::test_falha_de_envio_nao_derruba_o_turno` · `whatsapp_adaptador::test_falha_de_envio_nao_derruba_o_turno` · `whatsapp_zapi::test_resposta_200_com_erro_e_falha` |
| RF-WA-09 | Pela Meta, o aperto de mão deve devolver o desafio quando o token de verificação conferir e responder 403 caso contrário. | — | T: `webhook_meta::test_aperto_de_mao_devolve_o_desafio` · `webhook_meta::test_aperto_de_mao_com_token_errado_e_403` |
| RF-WA-10 | Pela Meta, todos os blocos de uma mesma chamada devem ser processados. | A Meta agrupa entregas; tratar só o primeiro perderia mensagens. | T: `whatsapp_meta::test_todos_os_blocos_sao_percorridos_e_lixo_e_ignorado` |
| RF-WA-11 | Pela Z-API, mensagens enviadas pelo próprio número, de grupo, de canal, de lista de transmissão, resposta a status ou ainda não decifradas devem ser ignoradas antes de qualquer acesso ao banco. | O número conectado continua vivo no aplicativo; sem o filtro, o eco de cada resposta da Marina viraria mensagem nova. | T: `whatsapp_zapi::test_o_que_nao_e_lead_falando_com_a_empresa_e_ignorado` · `webhook_zapi::test_eco_do_proprio_numero_e_ignorado_sem_tocar_no_banco` |
| RF-WA-12 | Pela Z-API, contato identificado só por `@lid` deve ser atendido sem ter o identificador tomado por telefone, e a resposta deve ir para o `phone` exatamente como chegou. | Os dígitos do `@lid` poderiam formar um telefone por acaso. | T: `whatsapp_zapi::test_contato_com_numero_oculto_nao_vira_telefone` · `parsers::test_telefone_do_canal_recusa_o_que_nao_reconhece` |
| RF-WA-13 | Pela Z-API, avisos de status devem ser registrados; falha de entrega e desconexão da instância devem ser registradas como erro; nenhum aviso deve abrir conversa. | Desconexão é o jeito de a Marina parar de responder sem ninguém ver. | T: `webhook_zapi::test_aviso_de_status_nao_abre_conversa_nem_toca_no_banco` · I: `_registrar_aviso_zapi` em `app/web/webhook.py` |

#### 3.2.9 Várias empresas (RF-EMP)

| ID | Requisito | Justificativa | Verificação |
|---|---|---|---|
| RF-EMP-01 | A mensagem deve ser atribuída à empresa pelo número que a recebeu (Twilio), pelo id do número (Meta) ou pelo id da instância (Z-API). | — | T: `webhook_twilio::test_a_mensagem_e_atribuida_a_empresa_do_numero_que_a_recebeu` · `empresas::test_o_numero_que_recebeu_a_mensagem_identifica_a_empresa` · `empresas::test_a_instancia_da_zapi_identifica_a_empresa_e_traz_o_token` |
| RF-EMP-02 | Mensagem para número não cadastrado ou de empresa desativada deve ser respondida com 204, registrada em log e não deixar nenhum outro rastro. | Quem chama é o provedor; erro só geraria reentrega. | T: `webhook_meta::test_numero_nao_cadastrado_nao_deixa_rastro` · `webhook_twilio::test_empresa_desativada_deixa_de_ser_atendida` · `webhook_zapi::test_instancia_nao_cadastrada_nao_deixa_rastro` |
| RF-EMP-03 | Cadastro, conversa e descadastro devem ser independentes por empresa: o mesmo telefone em duas empresas gera dois cadastros, e sair de uma não suprime na outra. | Cada instalação é um escritório diferente; o dado de um não é do outro. | T: `multiempresa::test_o_mesmo_telefone_em_duas_empresas_gera_dois_cadastros` · `multiempresa::test_sair_de_uma_empresa_nao_bloqueia_a_outra` |
| RF-EMP-04 | Só devem ser ofertados e reservados horários de consultores da empresa da conversa. | — | T: `multiempresa::test_so_sao_ofertados_horarios_de_profissional_da_empresa` · `multiempresa::test_reservar_recusa_profissional_de_outra_empresa` |
| RF-EMP-05 | Cada item da fila deve ser entregue apenas ao CRM da empresa de origem; item de lead de outra empresa deve falhar em definitivo. | Era o vazamento mais grave: dado de um escritório no CRM do outro. | T: `multiempresa::test_o_entregador_de_uma_empresa_nao_consome_item_da_outra` · `multiempresa::test_item_com_lead_de_outra_empresa_falha_em_definitivo` |
| RF-EMP-06 | Com mais de uma empresa cadastrada, os scripts operacionais devem exigir `--empresa` e recusar adivinhar; com uma só, devem assumi-la. | Escolher errado escreveria no CRM do escritório errado. | T: `empresas::test_com_duas_empresas_a_ferramenta_recusa_adivinhar` · `empresas::test_com_uma_empresa_so_ela_e_assumida` |

#### 3.2.10 Registro e auditoria (RF-REG)

| ID | Requisito | Justificativa | Verificação |
|---|---|---|---|
| RF-REG-01 | Toda mensagem de entrada e de saída deve ser gravada, na ordem da conversa, com a chave do catálogo que produziu cada resposta. | É o material de auditoria e o dataset de comparação com uma versão com IA. | T: `engine::test_transcricao_registra_entrada_e_saida` |
| RF-REG-02 | O operador deve conseguir consultar cadastro e transcrição completa de um lead pelo telefone. | — | D: `scripts/dump_lead.py` |
| RF-REG-03 | Eventos operacionais devem ser registrados em log estruturado (JSON), gravado em arquivo por padrão (`LOG_ARQUIVO`). | No terminal, o log intercalado tornaria a conversa ilegível. | I: `app/logging.py`, `app/config.py` |

### 3.3 Usabilidade (RNF-USA)

| ID | Requisito | Verificação |
|---|---|---|
| RNF-USA-01 | Perguntas de escolha devem apresentar opções numeradas e aceitar tanto o número quanto a palavra. | T: `parsers::test_fonte_aceita` · `parsers::test_tipo_contrato_aceito` |
| RNF-USA-02 | Todo texto deve estar em português do Brasil, com datas por extenso e horários no fuso `TIMEZONE_EXIBICAO` (padrão America/Sao_Paulo), independentemente do idioma do sistema operacional. | I: `app/datas.py` |
| RNF-USA-03 | Alteração em texto do catálogo deve ser detectada pela suíte, exibindo a diferença para revisão antes de ser aceita. | T: `e2e_transcricao::test_transcricao_do_caminho_feliz` |
| RNF-USA-04 | **[A DEFINIR]** Duração máxima da conversa completa. A saudação promete "menos de 2 minutos", sem meta medida. | A — após definição |

### 3.4 Desempenho (RNF-DES)

Nenhuma meta de desempenho foi definida. Os requisitos abaixo estão reconhecidos e
aguardam valor ([Apêndice B.2](#b2-valores-a-definir)).

| ID | Requisito | Situação atual (informativa) | Verificação |
|---|---|---|---|
| RNF-DES-01 | **[A DEFINIR]** Tempo entre a mensagem do lead chegar e a resposta ser enviada, em percentil. | Não medido. | A / T de carga |
| RNF-DES-02 | **[A DEFINIR]** Tempo máximo para o webhook responder ao provedor. | O webhook responde antes de atender (RF-WA-01); a Twilio desiste após 15 s. | T de carga |
| RNF-DES-03 | **[A DEFINIR]** Capacidade: conversas simultâneas e mensagens por minuto. | Não medido. Concorrência serializada por identidade (RF-WA-03). | T de carga |
| RNF-DES-04 | **[A DEFINIR]** Tempo máximo entre um marco da conversa e o registro no CRM, com o CRM disponível. | Entrega disparada a cada marco, em segundo plano. | T |

### 3.5 Requisitos lógicos de banco de dados (RD)

| ID | Requisito | Verificação |
|---|---|---|
| RD-01 | Datas e horas devem ser gravadas em UTC com fuso; a conversão para o fuso de exibição deve ocorrer só na apresentação. | I: `app/models.py` |
| RD-02 | A transcrição deve ser ordenada por número de sequência, não por data de gravação. | I: `mensagens.sequencia` |
| RD-03 | `empresa_id` deve ser obrigatório em `leads`, `conversas`, `consultores`, `regras_disponibilidade`, `opt_outs` e `crm_outbox`. | I: `app/models.py` · T: `multiempresa::test_a_fila_registra_para_qual_empresa_o_item_vai` |
| RD-04 | O banco deve garantir unicidade de: telefone por empresa; conversa aberta por identidade por empresa; e-mail de consultor por empresa; mensagem recebida por canal e id externo; agendamento ativo por consultor e início; instância da Z-API em uma empresa só. | T: `reserva::test_segunda_reserva_no_mesmo_horario_e_recusada` · `multiempresa::test_o_mesmo_telefone_em_duas_empresas_gera_dois_cadastros` |
| RD-05 | Regra de disponibilidade deve referenciar o consultor por chave composta (consultor, empresa). | I: `app/models.py` |
| RD-06 | O contexto persistido da conversa não deve conter dados transitórios (objetos ricos ou resultado de I/O recém-feito). | T: `engine::test_contexto_persistido_nao_leva_lixo_transitorio` |
| RD-07 | O identificador externo de mensagem deve comportar até 255 caracteres. | T: `webhook_meta` (fixture `WAMID_REAL`, 78 caracteres) |
| RD-08 | O esquema deve evoluir apenas por migrações Alembic versionadas; a versão atual é `0010_empresa_instancia_zapi`. | I: `alembic/versions/` |

### 3.6 Restrições de projeto (RR)

| ID | Requisito | Justificativa | Verificação |
|---|---|---|---|
| RR-01 | Nenhum texto enviado ao lead deve ser gerado por IA ou LLM; todo texto deve vir do catálogo, e chave inexistente ou variável faltante deve falhar na hora. | Um sistema que escolhe texto só diz o que foi aprovado. | T: `guards::test_render_reclama_de_variavel_faltando` · `guards::test_todas_as_chaves_usadas_existem` |
| RR-02 | A camada de decisão deve ser pura: sem acesso a banco, rede, disco, relógio ou aleatoriedade, importando do projeto só módulos explicitamente autorizados. | Permite testar todas as transições em milissegundos. | T: `machine_pura::test_machine_nao_importa_io` · `machine_pura::test_machine_so_importa_o_que_foi_autorizado_do_projeto` · `machine_pura::test_importar_a_maquina_nao_carrega_stack_de_io` |
| RR-03 | Toda operação com I/O deve ser declarada como efeito pela camada de decisão e executada pelo engine, com no máximo 8 reentradas por mensagem. | Um defeito vira erro visível, não laço infinito. | I: `MAX_REENTRADAS` em `app/engine.py` |
| RR-04 | Canais, agenda e armazenamento de documentos devem ficar atrás de interfaces (`ChannelAdapter`, `CalendarProvider`, `ArmazenamentoDocumento`), de modo que uma nova implementação não altere a camada de decisão. | — | I |
| RR-05 | A plataforma deve ser Python 3.12+ e PostgreSQL 16, com dependências declaradas em `pyproject.toml`. | — | I |
| RR-06 | Credenciais de cada empresa — token do CRM e token da instância da Z-API — devem residir apenas em variável de ambiente; o banco não deve guardar o valor de nenhuma delas. | Coluna com credencial apareceria em cópias e réplicas. | T: `empresas::test_a_credencial_e_lida_do_ambiente_pelo_nome_guardado` · `empresas::test_o_token_nao_aparece_na_representacao_da_empresa` · `empresas::test_empresa_sem_instancia_nao_carrega_token_da_zapi` |
| RR-07 | A verificação de integração deve recusar receber a configuração global no lugar de uma empresa. | Nomes de atributo coincidentes já reabriram a credencial global em silêncio. | T: `multiempresa::test_a_configuracao_nao_pode_passar_por_empresa` |

### 3.7 Atributos do sistema

#### 3.7.1 Confiabilidade (RNF-CONF)

| ID | Requisito | Verificação |
|---|---|---|
| RNF-CONF-01 | Indisponibilidade ou lentidão do CRM não deve interromper nem atrasar a conversa; o que ficou pendente deve ser entregue, em ordem, quando o CRM voltar. | T: `integracao_resiliencia::test_conversa_termina_normalmente_com_o_juridico_fora_do_ar` · `integracao_resiliencia::test_quando_o_juridico_volta_a_fila_entrega_na_ordem` |
| RNF-CONF-02 | Entregas concorrentes da fila não devem duplicar registros no CRM. | T: `juridico_entregador::test_drenagens_simultaneas_nao_duplicam_o_lead` |
| RNF-CONF-03 | Dois leads confirmando o mesmo horário simultaneamente devem resultar em no máximo uma reserva por consultor, inclusive com sobreposição parcial. | T: `reserva::test_dois_leads_ao_mesmo_tempo_so_um_agenda` · `reserva::test_sobreposicao_parcial_tambem_e_recusada` |
| RNF-CONF-04 | Estado da conversa e marcas de idempotência devem sobreviver ao reinício do processo. | T: `engine_atender::test_mensagens_seguintes_avancam_a_mesma_conversa` |
| RNF-CONF-05 | Erro de integração com o CRM nunca deve ser propagado ao atendimento. | I: `app/crm.py` |

#### 3.7.2 Disponibilidade (RNF-DISP)

| ID | Requisito | Verificação |
|---|---|---|
| RNF-DISP-01 | **[A DEFINIR]** Meta de disponibilidade do atendimento (percentual e janela de medição). | A — após definição |
| RNF-DISP-02 | **[A DEFINIR]** Endereço público estável para os webhooks. Hoje é um túnel descartável, que muda a cada reinício e exige reconfigurar os painéis. | D — após definição |
| RNF-DISP-03 | A credencial da Meta em uso deve ser a de usuário do sistema (sem expiração); token temporário não deve ser usado em operação. **Ressalva:** em 11/09/2026 o token configurado era temporário. | I: `.env` (`debug_token` da Graph API) |

#### 3.7.3 Segurança (RNF-SEG)

| ID | Requisito | Verificação |
|---|---|---|
| RNF-SEG-01 | Chamada da Twilio deve ser autenticada pela assinatura calculada sobre a URL pública configurada (`TWILIO_URL_PUBLICA`), nunca sobre cabeçalhos recebidos. | T: `webhook_twilio::test_url_assinada_vem_da_config_e_nao_dos_cabecalhos` |
| RNF-SEG-02 | Chamada da Meta deve ser autenticada por HMAC-SHA256 do corpo cru com a chave do app; sem chave configurada, toda chamada deve ser recusada. | T: `whatsapp_meta::test_corpo_trocado_derruba_a_assinatura` · `whatsapp_meta::test_sem_chave_do_app_recusa_ate_assinatura_calculada_sem_chave` |
| RNF-SEG-03 | Chamada sem assinatura válida deve ser respondida com 403 sem nenhum acesso ao banco. | T: `webhook_twilio::test_sem_assinatura_e_403_e_nao_toca_no_banco` · `webhook_meta::test_sem_assinatura_e_403_e_nao_toca_no_banco` |
| RNF-SEG-04 | A ordem de processamento do webhook deve ser: assinatura → empresa → idempotência → atendimento fora da requisição. | T: `webhook_meta::test_assinatura_de_outro_corpo_e_403` · I: `app/web/webhook.py` |
| RNF-SEG-05 | A resposta a número não cadastrado deve ser idêntica à de sucesso, sem revelar quais números existem. | T: `webhook_twilio::test_numero_nao_cadastrado_nao_deixa_rastro` |
| RNF-SEG-06 | A conferência de assinatura só deve poder ser desligada por configuração explícita, e o desligamento deve gerar aviso em log a cada chamada. | I: `app/web/assinatura.py` |
| RNF-SEG-07 | Credenciais não devem ser versionadas: `.env` e cópias `.env.bak*` devem estar fora do controle de versão. | I: `.gitignore` |
| RNF-SEG-08 | O usuário de serviço do CRM deve ter só as permissões `comercial.negociacoes` e `comercial.tarefas`, nunca `administrador_empresa`. | I: CRM, `php artisan integracao:usuario` |
| RNF-SEG-09 | Chamada da Z-API, que não é assinada, deve ser autenticada pelo segredo no endereço (`ZAPI_WEBHOOK_SEGREDO`), comparado em tempo constante; sem segredo configurado, toda chamada deve ser recusada; a recusa não deve acessar o banco. | T: `webhook_zapi::test_segredo_errado_e_403_e_nao_toca_no_banco` · `webhook_zapi::test_sem_segredo_configurado_tudo_e_recusado` |
| RNF-SEG-10 | Nenhum registro de log deve conter token de instância, token de conta ou segredo de webhook: URLs com esses valores devem ser mascaradas antes de gravadas, e falha de envio da Z-API deve ser registrada sem a URL. | T: `logging::test_filtro_mascara_url_passada_como_objeto` · `logging::test_log_de_acesso_do_webhook_da_zapi_e_mascarado` · `whatsapp_zapi::test_falha_de_rede_nao_vaza_o_token` |

#### 3.7.4 Privacidade (RNF-PRIV)

| ID | Requisito | Verificação |
|---|---|---|
| RNF-PRIV-01 | A finalidade do uso dos dados e o meio de encerrar devem ser informados antes da primeira pergunta. | T: `machine::test_boas_vindas_avisa_sobre_o_uso_dos_dados` |
| RNF-PRIV-02 | Contato suprimido não deve ser atendido sem confirmar que deseja retomar (RF-ID-08). | T: `engine::test_retomar_opt_out_revoga_o_registro` |
| RNF-PRIV-03 | **[A DEFINIR]** Prazo de retenção de conversas, cadastros e contratos, e rotina de exclusão ao fim do prazo. Hoje não há prazo nem exclusão. | A — após definição |
| RNF-PRIV-04 | **[A DEFINIR]** Atendimento a pedido do titular de acesso e de exclusão dos dados. Hoje há consulta (`dump_lead.py`), sem exclusão. | D — após definição |

#### 3.7.5 Manutenibilidade (RNF-MAN)

| ID | Requisito | Verificação |
|---|---|---|
| RNF-MAN-01 | A suíte `pytest` deve ser o critério de aceite de toda mudança e deve passar integralmente; os testes que não usam banco devem rodar em menos de 1 s. | I · T: `pytest` |
| RNF-MAN-02 | Testes que dependem do banco devem ser pulados com mensagem clara quando o PostgreSQL estiver indisponível, e não falhar. | I: `tests/conftest.py` |
| RNF-MAN-03 | Código, domínio e documentação devem seguir a convenção em português. | I |

#### 3.7.6 Portabilidade (RNF-PORT)

| ID | Requisito | Verificação |
|---|---|---|
| RNF-PORT-01 | A Marina deve funcionar em Windows, Linux e macOS; todo ponto de entrada assíncrono deve usar `app/aio.py::executar`. | I |
| RNF-PORT-02 | A formatação de datas não deve depender do locale do sistema operacional. | I: `app/datas.py` |

### 3.8 Informações de apoio

- **Parâmetros configuráveis** (`app/config.py`, lidos do `.env`): antecedência mínima,
  horizonte, horários ofertados, tentativas, janela de retomada, limites de documento,
  timeout e tentativas do CRM, fuso de exibição, credenciais de Twilio e Meta.
- **Máquina de estados:** `app/enums.py` (estados) e `app/machine.py` (transições).
- **Catálogo:** `app/messages.yaml`.

---

## 4. Verificação

| Método | Como é executado |
|---|---|
| **T — Teste** | `pytest` (453 casos). Cada requisito T cita ao menos um caso. A cerca de pureza (`machine_pura`) e o snapshot da transcrição (`e2e_transcricao`) são testes de restrição. |
| **I — Inspeção** | Revisão do código ou da configuração citada, em revisão de mudança. |
| **D — Demonstração** | Execução do script citado contra um ambiente de desenvolvimento. |
| **A — Análise** | Medição ou revisão documental. Aplica-se aos itens [A DEFINIR] depois de receberem valor. |

**Regra de manutenção:** mudança de comportamento deve atualizar, na mesma entrega, o
requisito, o teste que o rastreia e este documento. Requisito sem verificação não é
aceito.

---

## Apêndice A — Premissas e dependências

| # | Premissa / dependência | Efeito se falhar |
|---|---|---|
| A-1 | A empresa possui número de WhatsApp **brasileiro** registrado na Meta, ou um número conectado a uma instância da Z-API. | Pela Meta com número estrangeiro, as respostas a leads brasileiros não são entregues (130497); com a instância da Z-API desconectada, nada entra nem sai. |
| A-2 | O lead inicia a conversa; a Marina só responde dentro da janela de 24 h. | Mensagens fora da janela exigiriam modelo aprovado, não implementado. |
| A-3 | Cada empresa tem uma instalação do `base-crm-juridico` com a API de integração e um usuário de serviço. | A integração fica desligada para a empresa (RF-CRM-08). |
| A-4 | Consultores e regras de agenda estão cadastrados e ativos. | Sem horários, todo lead é escalado (RF-AGD-04). |
| A-5 | PostgreSQL disponível. | O atendimento para. |
| A-6 | Os dois lados (Marina e CRM) operam em America/Sao_Paulo. | A hora da tarefa no CRM fica deslocada. |

---

## Apêndice B — Requisitos propostos e pendências

### B.1 Requisitos propostos

| ID | Proposta | Motivo | Situação |
|---|---|---|---|
| PROP-01 | A Marina deve se apresentar em nome da empresa que atende. | A saudação cita fixo "Leve Soluções Financeiras e Empresariais" para todas as empresas. | Em aberto (fase 4) |
| PROP-02 | Lead cadastrado só com e-mail deve chegar ao CRM ou ser sinalizado a uma pessoa. | O CRM exige telefone: o lead ouve "está agendado" e a falha só aparece na fila. | Em aberto |
| PROP-03 | Item que esgota as tentativas na fila deve notificar uma pessoa. | Hoje só é descoberto por quem executar `flush_outbox.py --falhados`. | Em aberto |
| PROP-04 | O catálogo não deve prometer e-mail que não é enviado — ou o convite deve passar a ser enviado. | `pergunta_email` ("é por ele que envio o convite") e `agendado` ("enviei a confirmação") prometem um envio que está fora do escopo. | **Inconsistência** — decidir |
| PROP-05 | Resposta não entregue ao lead não deve fazer a conversa avançar como se ele a tivesse lido. | Em 11/09/2026, a saudação não entregue deixou a conversa na pergunta do nome, e o "Oi" seguinte seria gravado como nome. | Em aberto |
| PROP-06 | Os textos devem ser adequados ao canal. | `pedido_contrato` pede "o caminho do arquivo" e `erro_arquivo_nao_encontrado` cita o Explorer — instruções de terminal também enviadas no WhatsApp. | **Inconsistência** — decidir |
| PROP-07 | Reunião marcada diretamente no CRM deve bloquear o horário na Marina. | Hoje a Marina não lê a agenda do CRM e pode marcar por cima. | Em aberto |
| PROP-08 | Lead que abandona a conversa deve receber acompanhamento. | Não existe hoje; exigiria modelo aprovado fora da janela de 24 h. | Em aberto |
| PROP-09 | Coletar CPF para casar com cadastros existentes no CRM. | Cadastro existente só por CPF não é reconhecido. | Em aberto |
| PROP-10 | Configuração da Marina por tela dentro do CRM. | Hoje exige operador técnico. | Em aberto |

### B.2 Valores A DEFINIR

| Requisito | O que decidir | Quem decide |
|---|---|---|
| RNF-USA-04 | Duração máxima da conversa | Negócio |
| RNF-DES-01 a 04 | Tempos de resposta, capacidade e prazo de entrega ao CRM | Negócio + técnico |
| RNF-DISP-01 | Meta de disponibilidade | Negócio |
| RNF-DISP-02 | Hospedagem com endereço público estável | Técnico |
| RNF-PRIV-03 | Prazo de retenção e rotina de exclusão | Negócio + jurídico (LGPD) |
| RNF-PRIV-04 | Procedimento para pedidos do titular | Negócio + jurídico (LGPD) |
| Todos | Prioridade de cada requisito (atributo da 29148) | Negócio |

---

## Apêndice C — Divergências com documentos anteriores

| Documento | Afirmação | Situação atual |
|---|---|---|
| `marina-regras-de-negocio.md` §6 | "Esta instalação atende uma empresa"; a reforma para várias empresas "não vai acontecer". | **Superado.** Várias empresas implementadas em 09/09/2026 (RF-EMP). |
| `marina-regras-de-negocio.md` §6–7 | Usuário de serviço com papel DIRETOR/ROBO_SDR; tarefa nasce no nome do robô; 4 fontes (`INSTAGRAM_META_ADS`…). | **Superado** pelo `base-crm-juridico`: tarefa sem responsável desde 10/09/2026, 11 fontes, usuário de serviço com duas permissões (RF-CRM-03, RF-CRM-04, RNF-SEG-08). |
| `marina-regras-de-negocio.md` §2 | Telefone é sempre perguntado. | **Alterado:** no WhatsApp o número vem do canal (RF-CONV-05). |
| `marina-regras-de-negocio.md` §3.2 | Comandos `sair/parar/cancelar`, `atendente/humano/pessoa`, `voltar`. | **Ampliado:** também `encerrar`, `consultor` e `volta` (RF-GRD-01 a 03). |
| `README.md` | Número de testes e endpoints de backend antigos. | **Defasado** (ver `CLAUDE.md`). |

---

## Apêndice D — Histórico de revisões

| Versão | Data | Alteração |
|---|---|---|
| 1.0 | 15/09/2026 | Linha de base levantada do código e dos testes (413 casos, migração `0009_id_externo_da_meta`). |
| 1.1 | 15/09/2026 | Canal Z-API (RI-09, RI-10, RF-WA-11 a 13, RNF-SEG-09 e 10), mascaramento de segredos no log, migração `0010_empresa_instancia_zapi`, 453 casos. |
