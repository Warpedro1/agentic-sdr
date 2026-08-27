---
name: Nova Funcionalidade
about: Proponha uma funcionalidade nova para o sistema

---

**Problema a resolver:**
Que dor existe hoje e quem sente. Descreva a situação atual, não a solução.

**Comportamento esperado:**
O que o usuário deve conseguir fazer depois que isso existir.

**Módulo e telas:**
Módulo (Faturamento, Financeiro, Contabilidade, Compras/Estoque, RH/Cooperados,
Jurídico, Acesso) e as telas ou rotas envolvidas.

**Regras de negócio:**
As regras que valem, incluindo os casos de exceção e quem pode fazer o quê.

**Critérios de aceite:**
- [ ]
- [ ]

**Impactos a verificar:**
Marque o que a funcionalidade toca. Cada item marcado precisa estar resolvido antes do merge.
- [ ] Permissão nova (`r_permissions` / `app/Permissions.php`)
- [ ] Alteração de banco — SQL manual em `database/sql/`, o projeto não usa migrations
- [ ] Lançamento contábil — exige sincronizar os três `docs/contabilidade/*.md`
- [ ] Integração externa (appcoop, ZXOCR, TOTVS/Protheus, NFSe, TISS, CNAB)
- [ ] Documentação em `docs/` — documento novo entra no índice `docs/README.md`

**Como testar:**
Passo a passo para validar a funcionalidade pronta.

**Contexto adicional:**
Capturas de tela, protótipos, planilhas ou conversas que ajudem a entender o pedido.
