# Polymarket Copytrade Bot

Pure-copytrade bot for [Polymarket](https://polymarket.com) — replicates trades from a curated set of profitable traders, with strict risk caps and a real-time dashboard.

The bot detects on-chain trades from selected wallets the moment they execute (sub-second via Polygon WebSocket), sizes a paper or live order against the local bankroll, and exits via stop loss / take profit / wallet exit.

> **Disclaimer**: This bot trades with real money when `LIVE_MODE=true`. Markets are volatile, copytrade strategies do not guarantee profit, and bugs can cause losses. Run in paper mode for at least two weeks before considering live trading. **Use at your own risk.**

---

## Table of contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Quick start](#quick-start)
4. [Configuration](#configuration)
5. [Running](#running)
6. [Dashboard](#dashboard)
7. [Telegram bot](#telegram-bot)
8. [Strategy](#strategy)
9. [Wallet discovery](#wallet-discovery)
10. [Risk management](#risk-management)
11. [Development](#development)
12. [Project structure](#project-structure)

---

## Features

- **Real-time on-chain detection** — listens to `OrderFilled` events on Polymarket's V2 exchanges (CTF + NegRisk) via Polygon WebSocket / HTTP polling fallback.
- **Per-wallet position tracking** — each followed wallet's signal opens an independent position, so exits are correctly attributed.
- **Pure copytrade** — no alpha filters (volume, EV, probability, time). Trust the wallet's edge.
- **Risk-first sizing** — 8% per-trade cap, 15% per-event cap, 30% cash reserve, weekly stop loss, hard stop per position.
- **Adaptive RPC scanning** — chunks halve on timeouts; works with any provider.
- **Live + paper mode** — `LIVE_MODE=false` (default) records trades without touching the CLOB.
- **Telegram bidirectional** — `/historico`, `/posicoes`, `/skips`, `/status`, `/balance`, `/pause`, `/resume`, `/halt`.
- **Web dashboard** — single-file FastAPI app with bankroll, P&L, open positions, trades, risk and weekly performance.
- **Idempotency** — dedup by `tx_hash` on every order; restart-safe.
- **Circuit breakers** — auto-pause on −15% weekly, halt on 3 negative weeks, recovery mode after 2 consecutive stop losses.

---

## Architecture

```
┌─────────────────────┐
│  Polygon WebSocket  │  (Alchemy / publicnode / similar)
└──────────┬──────────┘
           │ OrderFilled events
           ▼
┌─────────────────────┐         ┌──────────────────────┐
│   ChainWatcher      │────────▶│   SignalReader       │
│ (parses logs,       │         │  (buffers, dedups    │
│  matches makers)    │         │   by tx_hash)        │
└─────────────────────┘         └──────────┬───────────┘
                                            │
                                            ▼
┌──────────────────────────────────────────────────────┐
│                  WalletMonitor                        │
│                                                       │
│  group(market, outcome) ──▶ MarketBuilder            │
│                              (Gamma + CLOB book)      │
│                                   │                   │
│                                   ▼                   │
│                          ExecutionPipeline            │
│                                   │                   │
│        ┌──────────────────────────┴──────────┐        │
│        ▼                                     ▼        │
│   sizing (8%, tier, consensus)         exit signals   │
│        │                                              │
│        ▼                                              │
│   ExposureGuard ──▶ OrderManager (paper / live)       │
│                          │                            │
│                          ▼                            │
│                     SQLite + Telegram                 │
└──────────────────────────────────────────────────────┘
                                ▲
                                │
                       ┌────────┴────────┐
                       │   ExitManager   │
                       │ (stop loss +50%,│
                       │  take profit,   │
                       │  wallet exits)  │
                       └─────────────────┘
```

Single-process asyncio. APScheduler runs the Sunday rebalancing jobs (wallet rediscovery + scoring + top-7 selection) within the same loop.

---

## Quick start

### Requirements

- Python 3.11+
- A Polygon RPC endpoint (Alchemy free tier works perfectly)
- A Polygon wallet with USDC (skip if running paper mode only)
- A Telegram bot token (created via [@BotFather](https://t.me/BotFather))

### Install

```bash
git clone https://github.com/todfilipe/polymarket-bot.git
cd polymarket-bot
python -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .\.venv\Scripts\Activate.ps1     # Windows PowerShell
pip install -e .
```

### Configure

```bash
cp .env.example .env
# edit .env with your Polygon key, RPC URL, Telegram token
```

### First-time wallet discovery (manual)

The bot's wallet list is normally rebuilt every Sunday at 23:00 UTC. To populate it now:

```bash
python scripts/force_rebalance.py
```

This pulls the top-150 traders from Polymarket's leaderboard, scores them, and saves the top-7 to SQLite.

### Run

```bash
python -m polymarket_bot.main
```

You should see Telegram receive `🤖 Bot iniciado | modo: PAPER | wallets: 7` within seconds.

---

## Configuration

All settings come from a `.env` file (Pydantic Settings). See [`.env.example`](.env.example) for the full list.

| Variable | Default | Description |
|---|---|---|
| `LIVE_MODE` | `false` | `true` enables real CLOB submissions. Default is paper trading. |
| `POLYGON_PRIVATE_KEY` | — | Required. 64 hex chars (no `0x` prefix). |
| `POLYGON_CHAIN_ID` | `137` | `137` = mainnet, `80002` = Amoy testnet. |
| `POLYGON_RPC_URL` | — | HTTP RPC endpoint. Alchemy strongly recommended. |
| `POLYGON_WS_URL` | — | Optional. WebSocket endpoint for sub-second detection. |
| `SIGNAL_SOURCE` | `chain` | `chain` (on-chain only), `api` (Data API polling), `both`. |
| `CLOB_API_URL` | `https://clob.polymarket.com` | Polymarket CLOB. |
| `POLYMARKET_GAMMA_API_URL` | `https://gamma-api.polymarket.com` | Gamma API for market metadata. |
| `CLOB_API_KEY` / `CLOB_API_SECRET` / `CLOB_API_PASSPHRASE` | — | Required for live mode. Generated on first run from `POLYGON_PRIVATE_KEY`. |
| `PROXY_WALLET_ADDRESS` | — | Generated on first run. Filled into `.env` for subsequent runs. |
| `TELEGRAM_BOT_TOKEN` | — | From @BotFather. |
| `TELEGRAM_CHAT_ID` | — | The chat where notifications and commands are exchanged. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/polymarket_bot.db` | SQLite path. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `LOG_FORMAT` | `json` | `json` (structured) or `text`. |
| `WALLET_DISCOVERY_TOP_N` | `150` | Pool size scored on Sunday rebalancing. |
| `SCORING_WINDOW_WEEKS` | `4` | Look-back window for scoring. |

---

## Running

### Local (development)

```bash
python -m polymarket_bot.main
```

`Ctrl+C` to stop. The scheduler keeps running in the same loop.

### Production (systemd)

```ini
# /etc/systemd/system/polymarket-bot.service
[Unit]
Description=Polymarket Copytrade Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/polymarket-bot
ExecStart=/root/polymarket-bot/.venv/bin/python -m polymarket_bot.main
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now polymarket-bot
journalctl -u polymarket-bot -f
```

### Dry-run check

Validates the configuration without arrancar do loop:

```bash
python -m polymarket_bot.main --dry-run-check
```

---

## Dashboard

A single-page web dashboard reads SQLite + Gamma API in real time.

### Install

```bash
pip install -e ".[dashboard]"      # adds fastapi, uvicorn, httpx
```

### Run

```bash
python dashboard/app.py
```

Listens on `127.0.0.1:8765`. Override with `DASHBOARD_HOST` and `DASHBOARD_PORT`.

For remote VPS access, use **Tailscale** (private network — no public exposure) or an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 root@your-vps-ip
# then: http://127.0.0.1:8765 in your browser
```

The dashboard shows: bankroll, P&L (week/total), open positions with live prices, trades (paginated, filtered by status), wallet stats, weekly performance, and circuit-breaker state.

---

## Telegram bot

Once `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, the bot listens on long-polling and accepts:

| Command | What it does |
|---|---|
| `/status` | Mode, circuit breaker state, open positions count, last activity. |
| `/balance` | Bankroll, capital deployed, cash reserve, weekly P&L. |
| `/posicoes` | Open positions (per wallet, per market). |
| `/historico` | Last 15 executed trades. |
| `/skips` | Last 10 skipped trades + reason. |
| `/wallets` | The 7 followed wallets, tier, last activity. |
| `/pause` | Pause new entries (positions still close). |
| `/resume` | Resume normal operation. |
| `/halt` | Force HALTED state — full stop until restart. |

Push notifications are sent for: bot start/stop, trade executed, trade skipped, hard stop loss triggered, circuit breaker change, weekly report (Sundays 22:00 UTC).

---

## Strategy

### Sizing

```
size = bankroll × 5% × tier_mult × signal_mult × recovery_mult
```

Then capped at 8% of bankroll, with a minimum of $20.

| Multiplier | Value | When |
|---|---|---|
| Base | 5% | Always |
| Tier | 1.4 | At least one TOP-tier wallet signals |
| | 0.7 | Only BOTTOM-tier wallets signal |
| Signal strength | 0.5 | Solo (1 wallet) |
| | 1.3 | Consensus (2+ same direction) |
| | 1.5 | All 7 wallets |
| Recovery | 0.6 | After 2 consecutive stop losses |
| | 1.0 | Normal |

Examples (with $1000 bankroll):
- 1 TOP wallet solo → $35
- 2 wallets, 1 TOP → $80 (cap)
- All 7 wallets → $80 (cap)

### Filters (entry)

In **pure copytrade mode**, the bot **does not filter** by EV, volume, probability, or time-to-resolution. The only entry gates are:

- **Sizing** — minimum $20 per trade
- **Orderbook depth** — must be able to fill at acceptable slippage
- **Exposure guard** — 15%/event, 30% cash reserve, max 10 open positions
- **Dedup** — same `tx_hash` never replays

### Exits

```
PnL ≤ −40%                     → close 100% (hard stop loss)
PnL ≥ +50% (1× per position)   → close 50% (take profit partial)
Originating wallet sells       → close 100% (wallet exit)
Otherwise                      → hold to resolution
```

Each rule is per-position (i.e., per-wallet). A 50% partial leaves the remaining half subject to the same triggers.

---

## Wallet discovery

The bot follows 7 wallets, refreshed every Sunday at 23:00 UTC by `RebalancingScheduler`.

### Pipeline

1. **Discovery**: pull top-150 by P&L from `data-api.polymarket.com/v1/leaderboard` (last 4 weeks).
2. **Data**: for each candidate, fetch full trade + position history.
3. **Filter** (any failure → excluded):
   - ≥ 50 trades
   - Active in last 2 weeks
   - Drawdown ≤ 35%
   - Volume ≥ $500
   - Profit factor ≥ 1.5
   - ≥ 2 categories
   - Win rate ≥ 55%
   - Luck concentration ≤ 60% (no more than 60% of profit from 2 trades)
4. **Score 0–100** (weighted):

| Metric | Weight |
|---|---|
| Profit factor | 30% |
| Consistency (% positive weeks) | 20% |
| Win rate | 20% |
| Drawdown (penalty) | 15% |
| Diversification (categories) | 10% |
| Recency | 5% |

5. **Selection**: top 3 ranked → `TIER.TOP`, next 4 → `TIER.BOTTOM`.

### Manual rebalance

```bash
python scripts/force_rebalance.py            # full rebalance + persist
python scripts/force_rebalance.py --verbose  # diagnostic, no persistence
```

---

## Risk management

| Limit | Value |
|---|---|
| Max per trade | 8% of bankroll |
| Max per event (single resolution) | 15% of bankroll |
| Cash reserve | ≥ 30% always |
| Max open positions | 10 |
| Hard stop loss per position | −40% |
| Weekly stop loss (auto-pause) | −15% |
| Consecutive stop losses → recovery mode | 2 |
| Negative weeks → halt | 3 |

Circuit breakers are persisted in SQLite and persist across restarts. Manual `/halt` requires a process restart to clear.

---

## Development

### Tests

```bash
pip install -e ".[dev]"
pytest                              # all unit tests
pytest tests/unit/test_pipeline.py  # one file
pytest -k "test_take_profit"        # by name
```

The suite has 180+ unit tests covering:
- Chain watcher (event parsing, address matching, market resolution)
- Pipeline (sizing, exposure, dedup)
- Exit manager (stop loss, partial close, wallet exit)
- Order manager (paper/live, idempotency)
- Sizer, EV, market filters
- Telegram commander

### Diagnostic scripts

`scripts/` contains read-only diagnostic tools used during development:

| Script | Purpose |
|---|---|
| `force_rebalance.py` | Run wallet discovery + scoring on demand. |
| `diag_watch.py` | Watch chain in real time for N minutes; print every detected signal. |
| `diag_integration.py` | Integration test: cross-references chain detections vs Data API. |
| `diag_e2e_chain.py` | End-to-end parse: confirms a known wallet's last trade was decoded correctly. |
| `diag_check_exchanges.py` | Detect which Polymarket contracts the followed wallets trade on. |
| `setup_credentials.py` | Generate CLOB API credentials from the Polygon private key (first-time setup). |

---

## Project structure

```
polymarket-bot/
├── README.md
├── pyproject.toml              # deps + tooling
├── .env.example                # config template
├── data/                       # SQLite (gitignored)
├── dashboard/                  # FastAPI dashboard (single file)
│   ├── app.py
│   └── static/index.html
├── scripts/                    # diagnostic + setup utilities
│   ├── force_rebalance.py
│   ├── setup_credentials.py
│   └── diag_*.py
├── src/polymarket_bot/
│   ├── main.py                 # entry point
│   ├── api/
│   │   ├── polymarket_clob.py  # CLOB client wrapper
│   │   └── polymarket_data.py  # Data API client
│   ├── config/
│   │   ├── settings.py         # Pydantic Settings
│   │   └── constants.py        # strategy constants
│   ├── db/
│   │   ├── models.py           # SQLAlchemy models
│   │   ├── session.py          # async session factory
│   │   └── enums.py
│   ├── monitoring/
│   │   ├── chain_watcher.py    # Polygon WebSocket / HTTP polling
│   │   ├── signal_reader.py    # signal buffer + dedup
│   │   ├── wallet_monitor.py   # main tick loop
│   │   ├── market_builder.py   # Gamma + CLOB book
│   │   └── exit_manager.py     # stop loss / take profit / wallet exit
│   ├── execution/
│   │   ├── pipeline.py         # ExecutionPipeline orchestrator
│   │   ├── order_manager.py    # paper + live submission
│   │   ├── consensus.py        # multi-wallet aggregation
│   │   ├── sizer.py            # size formula
│   │   ├── ev.py               # expected value calc
│   │   └── dedup.py            # tx_hash hash + window
│   ├── market/
│   │   ├── models.py           # MarketSnapshot, OrderBook
│   │   └── filters.py          # market filters
│   ├── portfolio/
│   │   ├── exposure_guard.py   # 15% event cap, cash reserve
│   │   └── portfolio_manager.py
│   ├── risk/
│   │   └── circuit_breaker.py  # weekly P&L tracking
│   ├── notifications/
│   │   ├── telegram_notifier.py
│   │   └── telegram_commander.py
│   ├── scheduler/
│   │   └── rebalancer.py       # APScheduler Sunday jobs
│   └── wallets/
│       ├── discovery.py        # leaderboard pull
│       ├── metrics.py          # profit factor, drawdown, etc.
│       ├── filters.py          # eligibility filters
│       └── scoring.py          # weighted score
└── tests/
    └── unit/                   # 180+ tests
```

---

## License

MIT.

---

## Contributing

Issues and PRs welcome. Make sure `pytest` passes before submitting.
