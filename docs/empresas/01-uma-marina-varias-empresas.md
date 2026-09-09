# Uma Marina, várias empresas

> **Estado: fases 1 a 3 construídas em 09/09/2026.** A fase 4 (a Marina falar em
> nome da empresa) segue pendente.
>
> Este documento registra as decisões que **não se recuperam lendo o código**: por
> que o telefone deixou de ser único, por que a credencial não fica no banco, e
> por que a fila de entrega precisa saber o destino.

---

## 1. A premissa que caiu

Quando o SDR foi criado, esperava-se que **um** sistema jurídico atendesse várias
empresas, e que a Marina as distinguisse por dentro. Foi por isso que ela ficou
com o calendário e com o cadastro de profissionais.

A realidade é outra: **cada instalação do jurídico é uma empresa e seus
funcionários**. Quem passa a atender várias é a Marina, conversando com várias
instalações e tratando cada uma como uma empresa.

| Antes | Agora |
|---|---|
| Um endereço e uma credencial de jurídico, no arquivo de configuração | Um cadastro de empresas, cada uma com o seu |
| Um número de WhatsApp | Um número por empresa; o número que **recebeu** a mensagem diz de quem ela é |
| Um conjunto de profissionais | Profissionais e agenda por empresa |
| Telefone único no banco inteiro | Telefone único **por empresa** |

---

## 2. As decisões

### 2.1 A mensagem é atribuída pelo número que a recebeu

Cada empresa tem seu número de WhatsApp. O campo de destino já chegava em toda
mensagem e era descartado; passou a ser a chave de roteamento.

**A atribuição acontece depois da autenticação, e antes de qualquer gravação.** O
número de destino vem no corpo, que não é confiável até a assinatura ser
conferida — rotear antes deixaria qualquer um escolher em nome de qual empresa
criar lead. E vem antes da marca de idempotência porque mensagem para número
desconhecido não deve deixar rastro nenhum.

**Número não cadastrado responde sucesso vazio, com aviso no log — nunca erro.**
Quem chama é a operadora, não uma pessoa: um erro só a faria reentregar e encher
o painel de alertas que ninguém consegue acionar; confirmaria a um sondador quais
números existem; e o caso é *estado de configuração*, não falha.

### 2.2 Uma conta de WhatsApp, vários números

A autenticação da mensagem usa uma credencial **da conta**, não do número. Com uma
conta só, ela é conhecida antes de olhar o conteúdo, e autenticar-antes-de-rotear
sai de graça.

Com contas separadas por empresa aparece um problema real: ou se tenta autenticar
contra todas as credenciais — e aí a credencial da empresa A valida uma mensagem
que se declara da empresa B, permitindo que um cliente crie leads no CRM do outro
—, ou se dá a cada empresa um endereço de webhook próprio, o que resolve mas exige
duas conferências adicionais.

**Ficou decidido: uma conta.** O caminho para separar existe e está registrado
aqui; nada depende dessa escolha além de as credenciais da conta seguirem globais.

### 2.3 A credencial de cada empresa vive no ambiente, não no banco

O banco guarda o **nome** da variável de ambiente (`JURIDICO_TOKEN_<SLUG>`); o
valor mora só no arquivo de configuração local.

Uma coluna com o token apareceria em cópia de segurança, em réplica e em qualquer
consulta — e cópias de desenvolvimento circulam entre máquinas. Cifrar resolveria,
ao custo de uma dependência nova e de uma chave mestra a administrar, que não se
paga neste estágio.

**O custo aceito é que cadastrar empresa exige reiniciar o processo.** É menor do
que parece: cadastrar já exige gerar a credencial no jurídico daquela empresa e
apontar um número para o webhook. Nunca foi autoatendimento.

**Não há retorno para uma credencial global.** Um retorno significaria, numa
configuração errada, entregar o dado de uma empresa com a credencial de outra. O
que sobrou na configuração é só o desligamento de emergência de todas de uma vez.

### 2.4 O mesmo telefone em duas empresas são dois cadastros

Cada instalação do jurídico é um escritório diferente, e o dado de um não é do
outro. Por isso a unicidade de telefone passou a ser por empresa, e o mesmo vale
para a conversa aberta e para o descadastro: **pedir para sair de uma empresa não
suprime a pessoa nas outras.**

### 2.5 A fila de entrega precisa saber o destino

Era o vazamento mais grave. A busca por itens pendentes **reserva** as linhas que
lê; sem filtro por empresa, o entregador de um escritório consumiria os itens do
outro e os postaria no CRM errado.

A busca passou a exigir a empresa, sem valor padrão, e cada empresa tem o seu
entregador. Há ainda uma conferência antes de enviar: item cuja empresa não é a do
entregador falha **em definitivo**, não em nova tentativa — é defeito de
programação, e queimar as tentativas de uma vez o transforma em algo que uma
pessoa olha.

### 2.6 O isolamento é do banco, não só do código

O identificador de empresa é obrigatório nas seis tabelas alcançáveis sem um pai
já escopado. Agendamentos, mensagens e documentos ficam de fora porque sempre
chegam pelo profissional, pela conversa ou pelo lead.

Uma regra de agenda **não consegue** apontar para profissional de outra empresa: a
chave estrangeira é composta. Sem ela, a empresa gravada na regra e a do
profissional poderiam divergir em silêncio, e divergência em agenda vira horário
oferecido pela empresa errada.

---

## 3. O que se aprendeu no caminho

**Coincidência de nomes de atributo abriu uma porta dos fundos.** Ao passar a
credencial a ser da empresa, uma ferramenta continuou entregando o objeto de
configuração onde agora se espera uma empresa. Como os dois têm por acaso os
mesmos três nomes de atributo, a chamada **passou sem erro** e voltou a usar a
credencial global. Hoje a checagem recusa explicitamente o que não for uma
empresa, e há teste para isso.

**Registros sem dono aparecem em toda implantação real**, não só em
desenvolvimento: toda linha criada entre a implantação do cadastro e a da fiação
nasce sem empresa. A migração os atribui quando é inequívoco e **recusa quando há
ambiguidade**, dizendo quantos são e o que fazer — atribuir ao escritório errado é
pior do que parar.

---

## 4. O que este documento não cobre

- **A Marina ainda se apresenta com nome fixo.** Atendendo por uma empresa, ela se
  identifica como a outra. É a fase 4.
- **Lead sem telefone não vira negociação.** A Marina aceita cadastro com um
  contato só; o jurídico exige telefone. Hoje esse lead ouve "está agendado" e
  ninguém do outro lado fica sabendo — a falha só aparece na fila.
- **Falha na fila não avisa ninguém.** Um lead que morre na entrega só é
  descoberto por quem for olhar `flush_outbox.py --falhados`.
