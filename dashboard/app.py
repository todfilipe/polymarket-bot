"""Dashboard de monitorização do Polymarket Copytrade Bot.

Servidor FastAPI que expõe a API de leitura sobre a SQLite do bot e serve
o frontend single-file (`static/index.html`).

Lançar: ``python dashboard/app.py``
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

# Garante que `polymarket_bot` é importável sem `pip install -e .`
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import desc, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from polymarket_bot.config.constants import CONST
from polymarket_bot.db.enums import (
    BotTradeStatus,
    CircuitBreakerStatus,
    PositionStatus,
)
from polymarket_bot.db.models import (
    BotTrade,
    CircuitBreakerState,
    Market,
    Position,
    Wallet,
    WalletScore,
)


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/polymarket_bot.db"
CAPITAL_INITIAL = 1_000.0
GAMMA_API_URL = "https://gamma-api.polymarket.com"
HEARTBEAT_FILE = Path(__file__).resolve().parent / "heartbeat.txt"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _resolve_database_url() -> str:
    """Lê DATABASE_URL do env e normaliza para driver async."""
    raw = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    # Detetar se é síncrono e converter para async (aiosqlite)
    if raw.startswith("sqlite:///"):
        return raw.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return raw


def _read_live_mode() -> bool:
    """Lê LIVE_MODE do env (sem instanciar settings — evita exigir todas as creds)."""
    raw = os.environ.get("LIVE_MODE", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# DB session local (evita reutilizar engine global do bot, que pode estar
# configurado para outra base ou nem ter sido inicializado)
# ---------------------------------------------------------------------------

_engine = create_async_engine(_resolve_database_url(), echo=False, future=True)
_sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


async def _open_session() -> AsyncSession:
    return _sessionmaker()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Polymarket Bot Dashboard", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(value: Decimal | float | int | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.isoformat()


def _start_of_iso_week_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday


def _read_heartbeat() -> str | None:
    try:
        if not HEARTBEAT_FILE.exists():
            return None
        raw = HEARTBEAT_FILE.read_text(encoding="utf-8").strip()
        return raw or None
    except OSError:
        return None


def _empty_payload(payload: dict[str, Any]) -> JSONResponse:
    """Resposta com flag db_offline para o frontend mostrar banner."""
    return JSONResponse({**payload, "db_offline": True}, status_code=200)


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    mode = "LIVE" if _read_live_mode() else "PAPER"
    heartbeat = _read_heartbeat()

    cb_status = CircuitBreakerStatus.NORMAL.value
    cb_reason: str | None = None
    cb_resumes_at: str | None = None
    open_positions_count = 0

    try:
        async with await _open_session() as session:
            cb = (
                await session.execute(
                    select(CircuitBreakerState).order_by(desc(CircuitBreakerState.id)).limit(1)
                )
            ).scalar_one_or_none()
            if cb is not None:
                cb_status = cb.status.value
                cb_reason = cb.reason
                cb_resumes_at = _iso(cb.resumes_at)

            open_count = (
                await session.execute(
                    select(func.count(Position.id)).where(
                        Position.status.in_(
                            [PositionStatus.OPEN, PositionStatus.PARTIALLY_CLOSED]
                        )
                    )
                )
            ).scalar() or 0
            open_positions_count = int(open_count)
    except SQLAlchemyError:
        return _empty_payload(
            {
                "mode": mode,
                "circuit_breaker": cb_status,
                "circuit_breaker_reason": None,
                "circuit_breaker_resumes_at": None,
                "open_positions_count": 0,
                "last_heartbeat": heartbeat,
            }
        )

    return {
        "mode": mode,
        "circuit_breaker": cb_status,
        "circuit_breaker_reason": cb_reason,
        "circuit_breaker_resumes_at": cb_resumes_at,
        "open_positions_count": open_positions_count,
        "last_heartbeat": heartbeat,
        "db_offline": False,
    }


# ---------------------------------------------------------------------------
# /api/capital
# ---------------------------------------------------------------------------

@app.get("/api/capital")
async def api_capital() -> dict[str, Any]:
    week_start = _start_of_iso_week_utc()
    weekly_stop_loss_pct = CONST.WEEKLY_STOP_LOSS * 100  # -15.0
    weekly_stop_loss_usd = CAPITAL_INITIAL * CONST.WEEKLY_STOP_LOSS

    try:
        async with await _open_session() as session:
            allocated = (
                await session.execute(
                    select(func.coalesce(func.sum(Position.size_usd), 0)).where(
                        Position.status.in_(
                            [PositionStatus.OPEN, PositionStatus.PARTIALLY_CLOSED]
                        )
                    )
                )
            ).scalar() or 0
            allocated_usd = _to_float(allocated)

            pnl_total = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(BotTrade.expected_value), 0)
                    )  # placeholder, vai ser substituído
                )
            ).scalar()
            # P&L acumulado real lê-se de positions fechadas + bot_trades realized
            pnl_total_realized = (
                await session.execute(
                    select(func.coalesce(func.sum(Position.realized_pnl_usd), 0)).where(
                        Position.status == PositionStatus.CLOSED
                    )
                )
            ).scalar() or 0
            pnl_total_usd = _to_float(pnl_total_realized)

            pnl_week_realized = (
                await session.execute(
                    select(func.coalesce(func.sum(Position.realized_pnl_usd), 0))
                    .where(Position.status == PositionStatus.CLOSED)
                    .where(Position.closed_at >= week_start)
                )
            ).scalar() or 0
            pnl_week_usd = _to_float(pnl_week_realized)

    except SQLAlchemyError:
        return _empty_payload(
            {
                "capital_initial": CAPITAL_INITIAL,
                "pnl_week_usd": 0.0,
                "pnl_week_pct": 0.0,
                "pnl_total_usd": 0.0,
                "pnl_total_pct": 0.0,
                "allocated_usd": 0.0,
                "cash_available_usd": CAPITAL_INITIAL,
                "weekly_stop_loss_pct": weekly_stop_loss_pct,
                "weekly_stop_loss_usd": weekly_stop_loss_usd,
            }
        )

    capital_now = CAPITAL_INITIAL + pnl_total_usd
    cash_available = max(0.0, capital_now - allocated_usd)

    return {
        "capital_initial": CAPITAL_INITIAL,
        "capital_current": round(capital_now, 2),
        "pnl_week_usd": round(pnl_week_usd, 2),
        "pnl_week_pct": round(100 * pnl_week_usd / CAPITAL_INITIAL, 2),
        "pnl_total_usd": round(pnl_total_usd, 2),
        "pnl_total_pct": round(100 * pnl_total_usd / CAPITAL_INITIAL, 2),
        "allocated_usd": round(allocated_usd, 2),
        "cash_available_usd": round(cash_available, 2),
        "weekly_stop_loss_pct": weekly_stop_loss_pct,
        "weekly_stop_loss_usd": round(weekly_stop_loss_usd, 2),
        "db_offline": False,
    }


# ---------------------------------------------------------------------------
# /api/positions
# ---------------------------------------------------------------------------

async def _fetch_market_price(
    client: httpx.AsyncClient, market_id: str, outcome: str
) -> float | None:
    """Lê o preço atual do outcome via Gamma API. Retorna None em falha."""
    try:
        if market_id.lower().startswith("0x"):
            r = await client.get(
                f"{GAMMA_API_URL}/markets",
                params={"condition_ids": market_id},
                timeout=5.0,
            )
            r.raise_for_status()
            payload = r.json()
            data = payload[0] if isinstance(payload, list) and payload else None
        else:
            r = await client.get(f"{GAMMA_API_URL}/markets/{market_id}", timeout=5.0)
            r.raise_for_status()
            data = r.json()

        if not isinstance(data, dict):
            return None

        outcomes_raw = data.get("outcomes")
        prices_raw = data.get("outcomePrices")

        # Os campos vêm como string JSON em alguns endpoints
        import json

        if isinstance(outcomes_raw, str):
            try:
                outcomes_raw = json.loads(outcomes_raw)
            except json.JSONDecodeError:
                outcomes_raw = None
        if isinstance(prices_raw, str):
            try:
                prices_raw = json.loads(prices_raw)
            except json.JSONDecodeError:
                prices_raw = None

        if not isinstance(outcomes_raw, list) or not isinstance(prices_raw, list):
            return None

        outcome_upper = outcome.upper()
        for name, price in zip(outcomes_raw, prices_raw):
            if str(name).upper() == outcome_upper:
                try:
                    return float(price)
                except (TypeError, ValueError):
                    return None
        return None
    except (httpx.HTTPError, ValueError):
        return None


@app.get("/api/positions")
async def api_positions() -> dict[str, Any]:
    try:
        async with await _open_session() as session:
            rows = (
                await session.execute(
                    select(Position)
                    .where(
                        Position.status.in_(
                            [PositionStatus.OPEN, PositionStatus.PARTIALLY_CLOSED]
                        )
                    )
                    .order_by(desc(Position.opened_at))
                )
            ).scalars().all()

            # Pré-carregar `Market.question` em batch
            market_ids = list({p.market_id for p in rows})
            markets_map: dict[str, Market] = {}
            if market_ids:
                m_rows = (
                    await session.execute(
                        select(Market).where(Market.market_id.in_(market_ids))
                    )
                ).scalars().all()
                markets_map = {m.market_id: m for m in m_rows}
    except SQLAlchemyError:
        return _empty_payload({"positions": []})

    if not rows:
        return {"positions": [], "db_offline": False}

    # Buscar preços actuais em paralelo
    async with httpx.AsyncClient() as client:
        price_tasks = [
            _fetch_market_price(client, p.market_id, p.outcome) for p in rows
        ]
        prices = await asyncio.gather(*price_tasks, return_exceptions=False)

    positions = []
    now = datetime.now(timezone.utc)
    for p, current_price in zip(rows, prices):
        entry = _to_float(p.avg_entry_price)
        size = _to_float(p.size_usd)
        opened_at = p.opened_at
        if opened_at and opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)

        pnl_usd: float | None = None
        pnl_pct: float | None = None
        if current_price is not None and entry > 0:
            pnl_pct = (current_price - entry) / entry
            pnl_usd = pnl_pct * size

        # Hard stop: -40% do investido — preço de saída é entry × (1 + (-0.40)) = entry × 0.60
        stop_price = entry * (1 + CONST.HARD_STOP_LOSS_PER_POSITION)
        near_stop = (
            current_price is not None
            and entry > 0
            and current_price <= stop_price * 1.10
        )

        market_meta = markets_map.get(p.market_id)
        question = market_meta.question if market_meta else None
        category = market_meta.category if market_meta else None

        followed = list(p.followed_wallets or [])

        positions.append(
            {
                "id": p.id,
                "market_id": p.market_id,
                "market_url": f"https://polymarket.com/market/{p.market_id}",
                "question": question,
                "category": category,
                "outcome": p.outcome,
                "side": p.side.value,
                "is_paper": p.is_paper,
                "avg_entry_price": entry,
                "size_usd": size,
                "current_price": current_price,
                "pnl_usd": round(pnl_usd, 2) if pnl_usd is not None else None,
                "pnl_pct": round(100 * pnl_pct, 2) if pnl_pct is not None else None,
                "stop_price": round(stop_price, 4),
                "near_stop": near_stop,
                "followed_wallets": followed,
                "followed_wallets_short": [w[:6] for w in followed],
                "opened_at": _iso(opened_at),
                "opened_seconds_ago": int((now - opened_at).total_seconds())
                if opened_at
                else None,
            }
        )

    return {"positions": positions, "db_offline": False}


# ---------------------------------------------------------------------------
# /api/trades
# ---------------------------------------------------------------------------

VALID_TRADE_STATUSES = {s.value for s in BotTradeStatus} | {"ALL"}


@app.get("/api/trades")
async def api_trades(
    status: str = Query("ALL"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    status = status.upper()
    if status not in VALID_TRADE_STATUSES:
        raise HTTPException(status_code=400, detail=f"status inválido: {status}")

    try:
        async with await _open_session() as session:
            base = select(BotTrade)
            if status != "ALL":
                base = base.where(BotTrade.status == BotTradeStatus(status))

            total = (
                await session.execute(
                    select(func.count()).select_from(base.subquery())
                )
            ).scalar() or 0

            offset = (page - 1) * per_page
            rows = (
                await session.execute(
                    base.order_by(desc(BotTrade.created_at))
                    .offset(offset)
                    .limit(per_page)
                )
            ).scalars().all()

            # Contagens por status (sem filtro)
            count_rows = (
                await session.execute(
                    select(BotTrade.status, func.count(BotTrade.id)).group_by(
                        BotTrade.status
                    )
                )
            ).all()
            counts: dict[str, int] = {s.value: 0 for s in BotTradeStatus}
            for s, c in count_rows:
                key = s.value if hasattr(s, "value") else str(s)
                counts[key] = int(c)
    except SQLAlchemyError:
        return _empty_payload(
            {
                "trades": [],
                "total": 0,
                "page": page,
                "per_page": per_page,
                "counts": {s.value: 0 for s in BotTradeStatus},
            }
        )

    trades = []
    for t in rows:
        trades.append(
            {
                "id": t.id,
                "created_at": _iso(t.created_at),
                "executed_at": _iso(t.executed_at),
                "submitted_at": _iso(t.submitted_at),
                "market_id": t.market_id,
                "market_url": f"https://polymarket.com/market/{t.market_id}",
                "outcome": t.outcome,
                "side": t.side.value,
                "is_paper": t.is_paper,
                "size_usd": _to_float(t.size_usd),
                "intended_price": _to_float(t.intended_price),
                "executed_price": _to_float(t.executed_price)
                if t.executed_price is not None
                else None,
                "expected_value": float(t.expected_value),
                "status": t.status.value,
                "skip_reason": t.skip_reason,
                "followed_wallet_address": t.followed_wallet_address,
                "followed_wallet_short": t.followed_wallet_address[:6]
                if t.followed_wallet_address
                else None,
                "slippage": t.slippage,
            }
        )

    return {
        "trades": trades,
        "total": int(total),
        "page": page,
        "per_page": per_page,
        "counts": counts,
        "db_offline": False,
    }


# ---------------------------------------------------------------------------
# /api/wallets
# ---------------------------------------------------------------------------

@app.get("/api/wallets")
async def api_wallets() -> dict[str, Any]:
    try:
        async with await _open_session() as session:
            wallets = (
                await session.execute(
                    select(Wallet).where(Wallet.is_followed == True)  # noqa: E712
                )
            ).scalars().all()

            results: list[dict[str, Any]] = []
            for w in wallets:
                latest_score = (
                    await session.execute(
                        select(WalletScore)
                        .where(WalletScore.wallet_address == w.address)
                        .order_by(desc(WalletScore.scored_at))
                        .limit(1)
                    )
                ).scalar_one_or_none()

                results.append(
                    {
                        "address": w.address,
                        "address_short": w.address[:10],
                        "tier": w.current_tier.value if w.current_tier else None,
                        "profile_url": f"https://polymarket.com/profile/{w.address}",
                        "score": float(latest_score.total_score) if latest_score else None,
                        "win_rate": float(latest_score.win_rate) if latest_score else None,
                        "profit_factor": float(latest_score.profit_factor)
                        if latest_score
                        else None,
                        "max_drawdown": float(latest_score.max_drawdown)
                        if latest_score
                        else None,
                        "total_trades": int(latest_score.total_trades)
                        if latest_score
                        else None,
                        "scored_at": _iso(latest_score.scored_at) if latest_score else None,
                    }
                )
    except SQLAlchemyError:
        return _empty_payload({"wallets": []})

    results.sort(key=lambda r: (r.get("score") or -1), reverse=True)
    for i, r in enumerate(results, start=1):
        r["rank"] = i

    return {"wallets": results, "db_offline": False}


# ---------------------------------------------------------------------------
# /api/risk
# ---------------------------------------------------------------------------

@app.get("/api/risk")
async def api_risk() -> dict[str, Any]:
    cash_reserve_pct_limit = CONST.CASH_RESERVE_RATIO * 100
    max_per_category_pct = CONST.MAX_RATIO_PER_CATEGORY * 100
    max_open_positions = CONST.MAX_OPEN_POSITIONS

    cb_payload = {
        "status": CircuitBreakerStatus.NORMAL.value,
        "reason": None,
        "consecutive_negative_weeks": 0,
        "size_reduction_factor": 1.0,
        "triggered_at": None,
        "resumes_at": None,
    }

    try:
        async with await _open_session() as session:
            cb = (
                await session.execute(
                    select(CircuitBreakerState).order_by(desc(CircuitBreakerState.id)).limit(1)
                )
            ).scalar_one_or_none()
            if cb is not None:
                cb_payload = {
                    "status": cb.status.value,
                    "reason": cb.reason,
                    "consecutive_negative_weeks": int(cb.consecutive_negative_weeks),
                    "size_reduction_factor": float(cb.size_reduction_factor),
                    "triggered_at": _iso(cb.triggered_at),
                    "resumes_at": _iso(cb.resumes_at),
                }

            # P&L total para calcular capital actual
            pnl_total_realized = (
                await session.execute(
                    select(func.coalesce(func.sum(Position.realized_pnl_usd), 0)).where(
                        Position.status == PositionStatus.CLOSED
                    )
                )
            ).scalar() or 0
            capital_now = CAPITAL_INITIAL + _to_float(pnl_total_realized)

            # Posições abertas + categorias
            open_rows = (
                await session.execute(
                    select(Position).where(
                        Position.status.in_(
                            [PositionStatus.OPEN, PositionStatus.PARTIALLY_CLOSED]
                        )
                    )
                )
            ).scalars().all()

            allocated_total = sum(_to_float(p.size_usd) for p in open_rows)

            cat_map: dict[str, float] = {}
            if open_rows:
                market_ids = list({p.market_id for p in open_rows})
                m_rows = (
                    await session.execute(
                        select(Market).where(Market.market_id.in_(market_ids))
                    )
                ).scalars().all()
                m_cat = {m.market_id: (m.category or "uncategorized") for m in m_rows}

                for p in open_rows:
                    cat = m_cat.get(p.market_id, "uncategorized")
                    cat_map[cat] = cat_map.get(cat, 0.0) + _to_float(p.size_usd)
    except SQLAlchemyError:
        return _empty_payload(
            {
                "circuit_breaker": cb_payload,
                "exposure": {
                    "capital_current": CAPITAL_INITIAL,
                    "total_allocated_usd": 0.0,
                    "total_allocated_pct": 0.0,
                    "cash_reserve_pct": 100.0,
                    "cash_reserve_limit_pct": cash_reserve_pct_limit,
                    "by_category": [],
                    "open_positions_count": 0,
                    "open_positions_limit": max_open_positions,
                },
            }
        )

    base = capital_now if capital_now > 0 else CAPITAL_INITIAL
    total_alloc_pct = (allocated_total / base) * 100 if base else 0.0
    cash_pct = max(0.0, 100.0 - total_alloc_pct)

    by_cat = []
    for cat, val in sorted(cat_map.items(), key=lambda kv: kv[1], reverse=True):
        by_cat.append(
            {
                "category": cat,
                "allocated_usd": round(val, 2),
                "allocated_pct": round((val / base) * 100, 2) if base else 0.0,
                "limit_pct": max_per_category_pct,
            }
        )

    return {
        "circuit_breaker": cb_payload,
        "exposure": {
            "capital_current": round(base, 2),
            "total_allocated_usd": round(allocated_total, 2),
            "total_allocated_pct": round(total_alloc_pct, 2),
            "cash_reserve_pct": round(cash_pct, 2),
            "cash_reserve_limit_pct": cash_reserve_pct_limit,
            "by_category": by_cat,
            "open_positions_count": len(open_rows) if "open_rows" in locals() else 0,
            "open_positions_limit": max_open_positions,
        },
        "db_offline": False,
    }


# ---------------------------------------------------------------------------
# /api/performance/weekly
# ---------------------------------------------------------------------------

@app.get("/api/performance/weekly")
async def api_performance_weekly() -> dict[str, Any]:
    """P&L por semana ISO nas últimas 8 semanas, baseado em Position.closed_at."""
    cutoff = _start_of_iso_week_utc() - timedelta(weeks=7)
    try:
        async with await _open_session() as session:
            rows = (
                await session.execute(
                    select(Position)
                    .where(Position.status == PositionStatus.CLOSED)
                    .where(Position.closed_at.isnot(None))
                    .where(Position.closed_at >= cutoff)
                )
            ).scalars().all()
    except SQLAlchemyError:
        return _empty_payload({"weeks": []})

    # Agrupar manualmente por (year, iso_week) para evitar dependência do dialect
    buckets: dict[str, dict[str, Any]] = {}
    for p in rows:
        if p.closed_at is None:
            continue
        d = p.closed_at
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        iso = d.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        b = buckets.setdefault(
            key, {"week": key, "pnl_usd": 0.0, "trades_won": 0, "trades_lost": 0}
        )
        pnl = _to_float(p.realized_pnl_usd)
        b["pnl_usd"] += pnl
        if pnl >= 0:
            b["trades_won"] += 1
        else:
            b["trades_lost"] += 1

    # Garantir as 8 semanas mais recentes (mesmo as vazias)
    weeks: list[dict[str, Any]] = []
    week_anchor = _start_of_iso_week_utc()
    for offset in range(7, -1, -1):
        anchor = week_anchor - timedelta(weeks=offset)
        iso = anchor.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        b = buckets.get(
            key, {"week": key, "pnl_usd": 0.0, "trades_won": 0, "trades_lost": 0}
        )
        weeks.append(
            {
                "week": key,
                "pnl_usd": round(b["pnl_usd"], 2),
                "pnl_pct": round(100 * b["pnl_usd"] / CAPITAL_INITIAL, 2),
                "trades_won": b["trades_won"],
                "trades_lost": b["trades_lost"],
            }
        )

    return {"weeks": weeks, "db_offline": False}


# ---------------------------------------------------------------------------
# Static / root
# ---------------------------------------------------------------------------

@app.get("/")
async def root_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    port = int(os.environ.get("DASHBOARD_PORT", "8765"))
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
