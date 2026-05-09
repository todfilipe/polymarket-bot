# Dashboard — Polymarket Copytrade Bot

Dashboard web local para monitorizar o bot em tempo real. Lê directamente da SQLite do bot e da Polymarket Gamma API. Refresh automático a cada 30 segundos.

## Como lançar

```bash
pip install fastapi uvicorn aiosqlite httpx
python dashboard/app.py
```

Abre depois no browser: <http://127.0.0.1:8765>

## Variáveis de ambiente

| Variável | Default | Descrição |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/polymarket_bot.db` | Caminho da SQLite (aceita formato sync `sqlite:///...` — converte automaticamente) |
| `LIVE_MODE` | `false` | Lido directamente do env (não obriga `.env` completo). `true` mostra banner "LIVE MODE" |
| `DASHBOARD_HOST` | `127.0.0.1` | Host onde uvicorn escuta |
| `DASHBOARD_PORT` | `8765` | Porta |

## Endpoints

- `GET /` — frontend (single-file HTML)
- `GET /api/status` — modo, circuit breaker, heartbeat, contagem posições
- `GET /api/capital` — capital, P&L semana/total, alocado, cash, stop loss
- `GET /api/positions` — posições abertas (com preço actual via Gamma API)
- `GET /api/trades?status=ALL&page=1&per_page=20` — histórico paginado, com contagens por status
- `GET /api/wallets` — wallets seguidas + último score
- `GET /api/risk` — circuit breaker + exposição por categoria
- `GET /api/performance/weekly` — P&L semanal das últimas 8 semanas

## Heartbeat opcional

Se o bot escrever um timestamp ISO em `dashboard/heartbeat.txt` periodicamente (ex.: a cada minuto), o dashboard mostra "Online há Xm" — caso contrário, "Offline".

```python
from datetime import datetime, timezone
from pathlib import Path

Path("dashboard/heartbeat.txt").write_text(datetime.now(timezone.utc).isoformat())
```

## Notas de implementação

- Stack: FastAPI + uvicorn no backend; HTML/CSS/JS vanilla num único ficheiro no frontend.
- Sem build step. Edita `static/index.html` e recarrega.
- Tema escuro fixo. Layout responsivo até ~1000px.
- Se a BD não existir ainda ou a query falhar, os endpoints devolvem dados vazios com `"db_offline": true` — o frontend mostra um banner amarelo em vez de erro 500.
- Os preços actuais das posições são buscados em paralelo via `httpx.AsyncClient`. Se a Gamma API falhar para um mercado, mostra "N/A" e segue.
