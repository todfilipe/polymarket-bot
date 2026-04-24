# CLAUDE.md — Polymarket Copytrade Bot

Documento de referência permanente. Todas as regras aqui são **inegociáveis** salvo instrução explícita do utilizador. A lógica estratégica completa está em `Polymarket_Bot_Logica_Estrategica.docx`.

---

## 1. Filosofia Central

- **Qualidade > Quantidade**: 7 wallets de elite, não 10.
- **Risco primeiro**: preservar capital > maximizar retorno pontual.
- **Disciplina automática**: o bot executa regras sem emoção. Zero overrides ad-hoc no código.
- **Fail-safe**: em caso de falha técnica, NÃO tradar. Capital ocioso > capital perdido.

## 2. Parâmetros Base (Hardcoded em `config`)

| Parâmetro | Valor |
|---|---|
| Capital inicial | $1.000 USDC |
| Objetivo semanal | 1–5% |
| Stop loss semanal | −15% da banca |
| Risco máx./trade | 8% da banca (hard cap) |
| Tamanho mínimo/trade | $20 |
| Nº wallets seguidas | 7 (Top 3 = 60% peso, Bottom 4 = 40%) |
| Cash reserve | 30% sempre (inegociável) |
| Posições abertas simultâneas | Máx. 10 |

## 3. Scoring de Wallets (0–100)

| Métrica | Peso |
|---|---|
| Profit Factor (lucro/perda total) | 30% |
| Consistência (% semanas +) | 20% |
| Win Rate (mín. 55%) | 20% |
| Drawdown Máx. (penalidade se > 35%) | 15% |
| Diversificação (≥ 2 categorias) | 10% |
| Recência (últimas 4 semanas) | 5% |

**Filtros obrigatórios de elegibilidade** (qualquer falha = excluir):
- ≥ 50 trades no histórico
- Ativa nas últimas 2 semanas
- Drawdown histórico nunca > 35%
- Volume mínimo $500
- Profit Factor > 1.5
- Trades em 2+ categorias

**Deteção de sorte vs skill**: se >60% do lucro vem de 1–2 trades → excluir.

## 4. Filtros de Mercado (todos obrigatórios)

- Volume > $50k | Tempo: 24h a 60 dias | Probabilidade: 10%–82%
- Order book: fill sem mover > 2%
- **Zona ideal**: prob 20–65%, tempo 7–30 dias, volume > $100k
- **EV obrigatório**: `EV = (p × lucro) − ((1−p) × stake)` > 10% do stake

## 5. Execução

- **SEMPRE limit orders. NUNCA market orders.**
- Janela: 60 segundos — se não executa, cancelar e registar "perdido por slippage".
- Slippage máx.: 5% se volume > $500k, senão 2%.
- **Consenso de wallets**:
  - 1 wallet → 50% do tamanho
  - 2+ wallets mesma direção → 100%
  - Wallets opostas (YES vs NO) → **não entrar**
  - Todas as wallets → até 1.5×, respeitando cap 8%

## 6. Sizing

```
base = 5% banca
× tier (Top3=1.4, Bot4=0.7)
× consenso (2+ wallets = 1.3, 1 wallet = 0.5)
× recuperação (após 2 perdas = 0.6)
HARD CAP: 8% da banca | MÍNIMO: $20
```

## 7. Saídas (hierarquia, por ordem de prioridade)

1. **Hard Stop Loss (−40% do investido)** — SEMPRE. Inegociável.
2. **Exit da wallet** — só seguir se wallet tem > 60% de exits precisos no histórico.
3. **Take Profit parcial** — a 75% de probabilidade, fechar 50%.
4. **Saída temporal** — se resolve em < 6h e em lucro, sair.

**Regra de ouro**: NUNCA depender da wallet para proteção. Stop loss próprio sempre ativo.

## 8. Gestão de Risco — Limites Simultâneos

- 8% / trade | 25% / categoria | 15% / evento | 50% top 3 combinadas | 30% cash reserve.
- **Averaging down: NUNCA seguir.** Momentum add: 50% tamanho, máx. 1 adição por posição.

## 9. Circuit Breakers

| Trigger | Ação |
|---|---|
| −15% semanal | Parar novos trades, retomar domingo |
| 2 stop losses consecutivos | −50% sizing + review manual |
| 3 semanas negativas | **Halt completo** |
| 3+ erros API/hora | Suspender trading |
| Exceção não tratada | Parar + alerta |

## 10. Resiliência Técnica

- Retry API: 5s → 30s → 5min → suspender.
- **Hash único por trade**: `wallet_address + market_id + side + minuto_timestamp`. Verificar antes de executar.
- Após restart: recarregar posições da SQLite, verificar stop losses perdidos, **não** executar trades retroativas.
- WebSocket como canal principal de deteção — reconectar em domingos após rebalanceamento.

## 11. Rebalanceamento (Domingos)

- 22:00 — Relatório semanal via Telegram
- 22:30 — Fechar posições em perda de wallets removidas
- 23:00 — Correr scoring sobre últimas 4 semanas
- 23:30 — Guardar novas top 7 em SQLite
- 00:00 seg — Reconfigurar WebSocket

## 12. Notificações Telegram

**Tempo real**: trade executada, trade ignorada (com motivo), stop loss, erro crítico.
**Semanal (dom 22:00)**: P&L, trades ganhas/perdidas/ignoradas, top 3 da semana, novas 7 para a semana seguinte.

## 13. Princípios de Implementação

- **Persistência**: SQLite (posições, trades, wallets, scores, hashes de dedup).
- **Dry-run é o modo default.** Live trading só com `LIVE_MODE=true` no `.env`. Todo módulo de execução verifica a flag antes de enviar ordens. Logs distinguem paper vs live.
- **Credenciais**: Polygon private key + CLOB creds apenas em `.env`. As credenciais CLOB (key/secret/passphrase) são geradas ao startup a partir da private key via `py-clob-client`. Proxy wallet criado na primeira execução.
- **Universo de wallets**: dinâmico via Polymarket Data API — top-N por profit nas últimas 4 semanas. Polygon RPC como fallback.
- **Idempotência**: toda ordem passa por dedup hash + lock antes de enviar.
- **Logs estruturados** (JSON) por módulo; motivos de "trade ignorada" contabilizados.
- **Telegram bidirecional**: comandos `/status`, `/positions`, `/pause`, `/resume`, `/halt`, `/balance` + alertas.
- **Deployment**: Windows local (dev) → VPS Linux (prod). APScheduler para cron.
- **Sem market orders. Sem retroatividade. Sem overrides emocionais.**
