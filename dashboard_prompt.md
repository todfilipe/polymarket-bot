# Prompt — Dashboard de Monitorização do Polymarket Bot

Cria um dashboard web local para monitorizar o Polymarket Copytrade Bot em tempo real.
O dashboard lê diretamente da base de dados SQLite do bot e da Polymarket API.
Deve ser simples de lançar (`python dashboard/app.py`) e abrir no browser.

---

## Stack obrigatória

- **Backend**: FastAPI + uvicorn (serve a API de dados e os ficheiros estáticos)
- **Frontend**: HTML/CSS/JS vanilla num único ficheiro `dashboard/static/index.html`  
  (sem React, sem build step — deve abrir diretamente no browser)
- **Base de dados**: SQLAlchemy async (já configurado em `src/polymarket_bot/db/`) — reutilizar os modelos existentes em `src/polymarket_bot/db/models.py`
- **Refresh automático**: polling a cada 30 segundos via `setInterval` no frontend

O ficheiro de entrada é `dashboard/app.py`. Deve:
1. Arrancar o servidor FastAPI na porta 8765
2. Servir `dashboard/static/index.html` na raiz `/`
3. Expor os endpoints de API em `/api/...`
4. Ler o caminho da SQLite a partir da variável de ambiente `DATABASE_URL` (ou default `sqlite+aiosqlite:///./bot.db`)

---

## Secções do Dashboard

### 1. Barra de Estado Global (topo, sempre visível)

Mostra o estado atual do bot numa linha:

- **Modo**: `PAPER` ou `LIVE` (lido de `LIVE_MODE` no `.env` / env var)
- **Circuit Breaker**: badge colorido — `NORMAL` (verde) / `PAUSED` (amarelo) / `RECOVERY` (laranja) / `HALTED` (vermelho). Ler da tabela `circuit_breaker_state` (última linha)
- **Bot online/offline**: timestamp do último heartbeat (se existir ficheiro `dashboard/heartbeat.txt` com timestamp ISO, mostrar "Online há Xm" ou "Offline")
- **Posições abertas**: número atual

Endpoint: `GET /api/status`

```json
{
  "mode": "PAPER",
  "circuit_breaker": "NORMAL",
  "circuit_breaker_reason": "...",
  "circuit_breaker_resumes_at": null,
  "open_positions_count": 3,
  "last_heartbeat": "2026-05-09T14:32:00"
}
```

---

### 2. Painel de Capital e P&L

Dois números grandes em destaque:

- **Capital atual** = soma de `size_usd` das posições com `status = OPEN` + cash reservado (calcular: `capital_inicial` × `(1 + pnl_acumulado)`)
- **P&L desta semana** (USDC e %) — soma de `realized_pnl_usd` de `bot_trades` com `status = FILLED` desde segunda-feira 00:00 UTC
- **P&L total acumulado** — soma histórica de `realized_pnl_usd` de todas as `bot_trades` com `status = FILLED`
- **Stop loss semanal**: barra de progresso horizontal — vermelho quando P&L semanal atinge −15% do capital inicial ($1000)
- **Cash disponível**: capital não alocado em posições abertas

Endpoint: `GET /api/capital`

```json
{
  "capital_initial": 1000.0,
  "pnl_week_usd": 34.5,
  "pnl_week_pct": 3.45,
  "pnl_total_usd": 87.2,
  "pnl_total_pct": 8.72,
  "allocated_usd": 420.0,
  "cash_available_usd": 580.0,
  "weekly_stop_loss_pct": -15.0,
  "weekly_stop_loss_usd": -150.0
}
```

---

### 3. Posições Abertas

Tabela com uma linha por posição (`status = OPEN` ou `PARTIALLY_CLOSED`):

| Mercado | Direção | Entrada | Tamanho | P&L atual | Stop Loss | Wallets | Aberta há |
|---------|---------|---------|---------|-----------|-----------|---------|-----------|

- **Mercado**: `market_id` com link para `https://polymarket.com/market/{market_id}` (ou usar `question` da tabela `markets` se disponível)
- **Direção**: badge YES/NO com cor (verde/vermelho)
- **Entrada**: `avg_entry_price` formatado como percentagem (ex: "34.2%")
- **Tamanho**: `size_usd` em USDC
- **P&L atual**: calcular com preço atual do mercado via Polymarket Gamma API (`GET https://gamma-api.polymarket.com/markets/{market_id}`) — campo `outcomePrices`. Se API falhar, mostrar "N/A"
- **Stop Loss**: calcular preço de stop (`avg_entry_price × 0.60` → hard stop de −40%) e mostrar em vermelho se o preço atual estiver a menos de 10% do stop
- **Wallets**: lista das wallets em `followed_wallets` (mostrar apenas primeiros 6 chars de cada address)
- **Aberta há**: tempo desde `opened_at`

Endpoint: `GET /api/positions`

---

### 4. Histórico de Trades

Tabela paginada (20 por página) de `bot_trades` ordenadas por `created_at DESC`:

| Hora | Mercado | Direção | Tamanho | Preço | Status | Wallet seguida | Motivo skip |
|------|---------|---------|---------|-------|--------|----------------|-------------|

- **Status**: badge colorido — `FILLED` (verde), `SKIPPED` (cinzento), `FAILED` (vermelho), `CANCELLED` (laranja), `PENDING`/`SUBMITTED` (azul)
- **Motivo skip**: mostrar apenas se `status = SKIPPED`, coluna `skip_reason`
- Filtros rápidos (botões): Todos | Executados | Ignorados | Falhados
- Mostrar contagem total de cada categoria no topo da secção

Endpoint: `GET /api/trades?status=ALL&page=1&per_page=20`

```json
{
  "trades": [...],
  "total": 142,
  "page": 1,
  "per_page": 20,
  "counts": {
    "FILLED": 89,
    "SKIPPED": 43,
    "FAILED": 6,
    "CANCELLED": 4
  }
}
```

---

### 5. Wallets Seguidas

Tabela das 7 wallets com `is_followed = true`, ordenadas por score descendente:

| Rank | Address | Tier | Score | Win Rate | Profit Factor | Drawdown Máx | Trades | Última atividade |
|------|---------|------|-------|----------|--------------|--------------|--------|-----------------|

- **Tier**: badge `TOP` (dourado) / `BOTTOM` (azul)
- **Score**: barra de progresso 0–100 com cor (verde > 70, amarelo 50–70, vermelho < 50)
- Ler da tabela `wallet_scores` (score mais recente por wallet, `MAX(scored_at)`)
- Link no address para `https://polymarket.com/profile/{address}`

Endpoint: `GET /api/wallets`

---

### 6. Circuit Breaker & Risk Monitor

Painel lateral direito (ou secção dedicada) com:

- **Estado atual** do circuit breaker (tabela `circuit_breaker_state`, última linha)
- **Contador de semanas negativas consecutivas** (`consecutive_negative_weeks`) — alerta vermelho se ≥ 2
- **Fator de redução de tamanho** (`size_reduction_factor`) — mostrar se < 1.0
- **Limites de risco em tempo real**:
  - Exposição por categoria: calcular de `positions` abertas agrupadas por `market_id` → join com `markets.category`
  - Mostrar barra para cada categoria: atual vs limite 25% da banca
  - Exposição total alocada vs limite 70% (cash reserve de 30%)
  - Nº posições abertas vs limite 10

Endpoint: `GET /api/risk`

```json
{
  "circuit_breaker": { "status": "NORMAL", "reason": "...", "consecutive_negative_weeks": 0, "size_reduction_factor": 1.0 },
  "exposure": {
    "total_allocated_pct": 42.0,
    "cash_reserve_pct": 58.0,
    "by_category": [
      { "category": "Sports", "allocated_usd": 120.0, "allocated_pct": 12.0, "limit_pct": 25.0 }
    ],
    "open_positions_count": 3,
    "open_positions_limit": 10
  }
}
```

---

### 7. Performance Semanal (mini gráfico)

Gráfico de barras simples (canvas HTML5 ou Chart.js via CDN) mostrando P&L por semana nas últimas 8 semanas:

- Barras verdes para semanas positivas, vermelhas para negativas
- Linha horizontal a 0
- Tooltip com valor exato em USDC e %
- Calcular agrupando `bot_trades` com `status = FILLED` por semana ISO (`strftime('%Y-%W', executed_at)`)

Endpoint: `GET /api/performance/weekly`

```json
{
  "weeks": [
    { "week": "2026-W18", "pnl_usd": 34.5, "pnl_pct": 3.45, "trades_won": 12, "trades_lost": 4 }
  ]
}
```

---

## Design e UX

- **Tema escuro** obrigatório — fundo `#0d1117`, painéis `#161b22`, bordas `#30363d`, texto `#e6edf3`
- **Accent**: verde `#3fb950` para positivo, vermelho `#f85149` para negativo, azul `#58a6ff` para neutro
- **Fonte**: system-ui ou Inter via Google Fonts
- **Layout**: grid responsivo — barra de estado no topo, depois 2 colunas (capital + gráfico | risk), depois posições, depois trades + wallets em baixo
- **Sem scroll infinito** — usar paginação simples
- **Badges**: pill-shaped com cores semânticas
- **Números monetários**: sempre 2 casas decimais, separador de milhar, símbolo `$`
- **Timestamps**: formato relativo ("há 3 min") com tooltip do timestamp absoluto
- **Loading state**: skeleton loaders enquanto os dados chegam
- **Erro de API**: mostrar banner amarelo "Sem dados — BD offline" se qualquer endpoint falhar

---

## Ficheiros a criar

```
dashboard/
├── app.py              # FastAPI app + endpoints
├── static/
│   └── index.html      # Frontend completo (HTML + CSS + JS inline)
└── README.md           # Como lançar: `pip install fastapi uvicorn aiosqlite && python dashboard/app.py`
```

**Não criar** ficheiros CSS ou JS separados — tudo no `index.html`.

---

## Notas de implementação

1. Reutilizar `src/polymarket_bot/db/session.py` para a conexão async à SQLite — não criar nova sessão.
2. O `app.py` deve ter `sys.path.insert(0, str(Path(__file__).parent.parent / "src"))` para importar os modelos.
3. Todos os endpoints devem ter `try/except` — se a BD não existir ainda, retornar dados vazios (não erro 500).
4. O preço atual das posições (P&L não realizado) deve ser buscado em background task assíncrona para não bloquear o render — usar `httpx.AsyncClient`.
5. Os endpoints devem aceitar tanto SQLite síncrono (`sqlite:///...`) como async (`sqlite+aiosqlite:///...`) — detetar automaticamente.
6. Adicionar `Access-Control-Allow-Origin: *` nos headers para desenvolvimento local.
7. O frontend deve detetar automaticamente se está em modo paper vs live e mostrar banner "PAPER MODE" bem visível no topo se `mode = PAPER`.
