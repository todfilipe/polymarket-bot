"""Dashboard de monitorização do Polymarket Copytrade Bot.

Servidor FastAPI que expõe a API de leitura sobre a SQLite do bot e serve
o frontend single-file (`static/index.html`).

Lançar: ``python dashboard/app.py``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import defaultdict
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
    weekly_stop_loss_pct = CONST.WEEKLY_STOP_LOSS * 100  # -25.0 (semana pausa)
    weekly_stop_loss_usd = CAPITAL_INITIAL * CONST.WEEKLY_STOP_LOSS

    open_rows: list[Position] = []
    try:
        async with await _open_session() as session:
            open_rows = (
                await session.execute(
                    select(Position).where(
                        Position.status.in_(
                            [PositionStatus.OPEN, PositionStatus.PARTIALLY_CLOSED]
                        )
                    )
                )
            ).scalars().all()
            allocated_usd = sum(_to_float(p.size_usd) for p in open_rows)

            # P&L realizado (positions fechadas)
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
                "pnl_unrealized_usd": 0.0,
                "pnl_unrealized_pct": 0.0,
                "allocated_usd": 0.0,
                "cash_available_usd": CAPITAL_INITIAL,
                "weekly_stop_loss_pct": weekly_stop_loss_pct,
                "weekly_stop_loss_usd": weekly_stop_loss_usd,
            }
        )

    # P&L unrealized — mark-to-market das posições abertas via Gamma (cacheado)
    pnl_unrealized_usd = 0.0
    if open_rows:
        async with httpx.AsyncClient() as client:
            metas = await asyncio.gather(
                *[_fetch_market_meta(client, p.market_id) for p in open_rows],
                return_exceptions=False,
            )
        for p, meta in zip(open_rows, metas):
            entry = _to_float(p.avg_entry_price)
            size = _to_float(p.size_usd)
            current = _price_from_meta(meta, p.outcome)
            if current is None or entry <= 0 or size <= 0:
                continue
            pnl_unrealized_usd += ((current - entry) / entry) * size

    capital_now = CAPITAL_INITIAL + pnl_total_usd
    cash_available = max(0.0, capital_now - allocated_usd)

    return {
        "capital_initial": CAPITAL_INITIAL,
        "capital_current": round(capital_now, 2),
        "pnl_week_usd": round(pnl_week_usd, 2),
        "pnl_week_pct": round(100 * pnl_week_usd / CAPITAL_INITIAL, 2),
        "pnl_total_usd": round(pnl_total_usd, 2),
        "pnl_total_pct": round(100 * pnl_total_usd / CAPITAL_INITIAL, 2),
        "pnl_unrealized_usd": round(pnl_unrealized_usd, 2),
        "pnl_unrealized_pct": round(100 * pnl_unrealized_usd / CAPITAL_INITIAL, 2),
        "allocated_usd": round(allocated_usd, 2),
        "cash_available_usd": round(cash_available, 2),
        "weekly_stop_loss_pct": weekly_stop_loss_pct,
        "weekly_stop_loss_usd": round(weekly_stop_loss_usd, 2),
        "db_offline": False,
    }


# ---------------------------------------------------------------------------
# /api/positions
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Meta-cache do Gamma — slug, eventSlug, question, category, outcomes, prices.
#
# A tabela `markets` no DB nunca é populada pelo bot, e os mercados-alvo são
# identificados por `conditionId` (0x...). O frontend antes construía URLs do
# tipo `polymarket.com/market/{conditionId}` que devolvem 404 — Polymarket usa
# slugs (`/event/{eventSlug}` ou `/market/{slug}`). Buscamos a meta on-demand
# e cacheamos in-process por TTL para não martelar o Gamma.
# ---------------------------------------------------------------------------

_META_CACHE_TTL_SECONDS = 300  # 5 min
_market_meta_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}


async def _fetch_market_meta(
    client: httpx.AsyncClient, market_id: str
) -> dict[str, Any] | None:
    """Busca meta + preços actuais do Gamma. Cacheado in-process por TTL.

    Devolve dict com `slug`, `event_slug`, `question`, `category`, `outcomes`
    (lista) e `prices` (lista alinhada com outcomes). Em falha devolve None.
    """
    now = datetime.now(timezone.utc)
    cached = _market_meta_cache.get(market_id)
    if cached:
        cached_at, data = cached
        if (now - cached_at).total_seconds() < _META_CACHE_TTL_SECONDS:
            return data

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
            if data is None:
                # Mercado pode estar resolvido — Gamma filtra por default. Tenta closed.
                r = await client.get(
                    f"{GAMMA_API_URL}/markets",
                    params={"condition_ids": market_id, "closed": "true"},
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

        # Os campos `outcomes` e `outcomePrices` vêm como string JSON em alguns
        # endpoints — normalizar para listas Python.
        outcomes_raw = data.get("outcomes")
        prices_raw = data.get("outcomePrices")
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

        meta: dict[str, Any] = {
            "slug": (data.get("slug") or "").strip() or None,
            "event_slug": (data.get("eventSlug") or "").strip() or None,
            "question": (data.get("question") or data.get("title") or "").strip() or None,
            "category": (data.get("category") or "").strip() or None,
            "outcomes": outcomes_raw if isinstance(outcomes_raw, list) else None,
            "prices": prices_raw if isinstance(prices_raw, list) else None,
        }
        _market_meta_cache[market_id] = (now, meta)
        return meta
    except (httpx.HTTPError, ValueError):
        return None


def _build_market_url(meta: dict[str, Any] | None) -> str | None:
    """Monta URL Polymarket. Preferência: /event/{eventSlug} > /market/{slug}.

    Devolve None se nenhum slug disponível — o frontend mostra texto sem link.
    """
    if not meta:
        return None
    event_slug = meta.get("event_slug")
    slug = meta.get("slug")
    if event_slug:
        return f"https://polymarket.com/event/{event_slug}"
    if slug:
        return f"https://polymarket.com/market/{slug}"
    return None


def _price_from_meta(meta: dict[str, Any] | None, outcome: str) -> float | None:
    """Extrai preço actual do outcome a partir da meta cacheada."""
    if not meta:
        return None
    outcomes = meta.get("outcomes")
    prices = meta.get("prices")
    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None
    outcome_up = outcome.upper()
    for name, price in zip(outcomes, prices):
        if str(name).upper() == outcome_up:
            try:
                return float(price)
            except (TypeError, ValueError):
                return None
    return None


async def _fetch_market_price(
    client: httpx.AsyncClient, market_id: str, outcome: str
) -> float | None:
    """Wrapper retro-compatível — usa o cache de meta."""
    meta = await _fetch_market_meta(client, market_id)
    return _price_from_meta(meta, outcome)


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

    # Buscar meta (slug+question+preço) em paralelo. Uma chamada por mercado
    # único — não por outcome — graças ao cache.
    async with httpx.AsyncClient() as client:
        meta_tasks = [_fetch_market_meta(client, p.market_id) for p in rows]
        metas = await asyncio.gather(*meta_tasks, return_exceptions=False)

    positions = []
    now = datetime.now(timezone.utc)
    for p, meta in zip(rows, metas):
        entry = _to_float(p.avg_entry_price)
        size = _to_float(p.size_usd)
        opened_at = p.opened_at
        if opened_at and opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)

        current_price = _price_from_meta(meta, p.outcome)
        pnl_usd: float | None = None
        pnl_pct: float | None = None
        if current_price is not None and entry > 0:
            pnl_pct = (current_price - entry) / entry
            pnl_usd = pnl_pct * size

        # Meta do Gamma tem prioridade; cai em Market do DB se existir.
        db_market = markets_map.get(p.market_id)
        question = (meta.get("question") if meta else None) or (
            db_market.question if db_market else None
        )
        category = (meta.get("category") if meta else None) or (
            db_market.category if db_market else None
        )
        market_url = _build_market_url(meta)

        followed = list(p.followed_wallets or [])

        positions.append(
            {
                "id": p.id,
                "market_id": p.market_id,
                "market_url": market_url,
                "question": question,
                "category": category,
                "outcome": p.outcome,
                "side": p.side.value,
                "is_paper": p.is_paper,
                "avg_entry_price": entry,
                "entries_count": int(p.entries_count or 1),
                "size_usd": size,
                "current_price": current_price,
                "pnl_usd": round(pnl_usd, 2) if pnl_usd is not None else None,
                "pnl_pct": round(100 * pnl_pct, 2) if pnl_pct is not None else None,
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
# /api/positions/closed — histórico de posições fechadas com filtro WIN/LOSE
# ---------------------------------------------------------------------------

# Threshold para classificar BREAKEVEN — abaixo disto considera-se zero (rounding).
_BREAKEVEN_USD_THRESHOLD = 0.01

VALID_RESULT_FILTERS = {"ALL", "WIN", "LOSS", "BREAKEVEN"}

# Períodos suportados para filtro temporal — chave → (label, timedelta).
# "all" = sem filtro temporal.
_PERIOD_DELTAS: dict[str, timedelta | None] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


def _classify_result(pnl_usd: float) -> str:
    if pnl_usd > _BREAKEVEN_USD_THRESHOLD:
        return "WIN"
    if pnl_usd < -_BREAKEVEN_USD_THRESHOLD:
        return "LOSS"
    return "BREAKEVEN"


@app.get("/api/positions/closed")
async def api_positions_closed(
    result: str = Query("ALL"),
    wallet: str | None = Query(None),
    period: str = Query("all"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    result = result.upper()
    if result not in VALID_RESULT_FILTERS:
        raise HTTPException(
            status_code=400, detail=f"result inválido: {result}"
        )
    period_lc = period.lower()
    if period_lc not in _PERIOD_DELTAS:
        raise HTTPException(
            status_code=400,
            detail=f"period inválido: {period}. Válidos: {sorted(_PERIOD_DELTAS)}",
        )
    delta = _PERIOD_DELTAS[period_lc]
    since_dt: datetime | None = (
        datetime.now(timezone.utc) - delta if delta is not None else None
    )

    try:
        async with await _open_session() as session:
            stmt = (
                select(Position)
                .where(Position.status == PositionStatus.CLOSED)
                .where(Position.realized_pnl_usd.isnot(None))
            )
            if since_dt is not None:
                stmt = stmt.where(Position.closed_at >= since_dt)
            rows = (
                await session.execute(stmt.order_by(desc(Position.closed_at)))
            ).scalars().all()
    except SQLAlchemyError:
        return _empty_payload(
            {
                "positions": [],
                "total": 0,
                "page": page,
                "per_page": per_page,
                "period": period_lc,
                "counts": {"WIN": 0, "LOSS": 0, "BREAKEVEN": 0, "ALL": 0},
            }
        )

    # Filtro por wallet (Python-side; SQLite não tem suporte JSON portátil).
    if wallet:
        wallet_lc = wallet.lower()
        rows = [
            p for p in rows
            if any(w.lower() == wallet_lc for w in (p.followed_wallets or []))
        ]

    # Counts globais (depois do filtro de wallet, antes do filtro de result)
    counts = {"WIN": 0, "LOSS": 0, "BREAKEVEN": 0}
    for p in rows:
        counts[_classify_result(_to_float(p.realized_pnl_usd))] += 1
    counts["ALL"] = sum(counts.values())

    # Filtro de resultado
    if result != "ALL":
        rows = [
            p for p in rows
            if _classify_result(_to_float(p.realized_pnl_usd)) == result
        ]

    total = len(rows)
    offset = (page - 1) * per_page
    page_rows = rows[offset : offset + per_page]

    # Buscar meta (slug+question) em paralelo apenas para a página actual
    if page_rows:
        async with httpx.AsyncClient() as client:
            metas = await asyncio.gather(
                *[_fetch_market_meta(client, p.market_id) for p in page_rows],
                return_exceptions=False,
            )
    else:
        metas = []

    positions = []
    for p, meta in zip(page_rows, metas):
        entry = _to_float(p.avg_entry_price)
        size = _to_float(p.size_usd)
        pnl = _to_float(p.realized_pnl_usd)
        pnl_pct = (pnl / size * 100) if size > 0 else 0.0
        # Preço de saída implícito: entry × (1 + pnl_pct_unitario)
        exit_price: float | None
        if size > 0 and entry > 0:
            exit_price = round(entry * (1 + pnl / size), 4)
        else:
            exit_price = None

        followed = list(p.followed_wallets or [])
        opened_at = p.opened_at
        if opened_at and opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        closed_at = p.closed_at
        if closed_at and closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        held_seconds = (
            int((closed_at - opened_at).total_seconds())
            if opened_at and closed_at
            else None
        )

        positions.append(
            {
                "id": p.id,
                "market_id": p.market_id,
                "market_url": _build_market_url(meta),
                "question": meta.get("question") if meta else None,
                "category": meta.get("category") if meta else None,
                "outcome": p.outcome,
                "side": p.side.value,
                "is_paper": p.is_paper,
                "avg_entry_price": entry,
                "exit_price": exit_price,
                "size_usd": size,
                "realized_pnl_usd": round(pnl, 2),
                "realized_pnl_pct": round(pnl_pct, 2),
                "result": _classify_result(pnl),
                "entries_count": int(p.entries_count or 1),
                "followed_wallets": followed,
                "followed_wallets_short": [w[:6] for w in followed],
                "opened_at": _iso(opened_at),
                "closed_at": _iso(closed_at),
                "held_seconds": held_seconds,
            }
        )

    return {
        "positions": positions,
        "total": total,
        "page": page,
        "per_page": per_page,
        "period": period_lc,
        "counts": counts,
        "db_offline": False,
    }


# ---------------------------------------------------------------------------
# /api/snapshots/weekly — snapshots semanais (paper-mode reset)
# ---------------------------------------------------------------------------

@app.get("/api/snapshots/weekly")
async def api_snapshots_weekly(
    limit: int = Query(12, ge=1, le=52),
) -> dict[str, Any]:
    """Devolve os últimos N snapshots semanais (mais recente primeiro).

    Cada snapshot é o "fecho" de uma semana paper-mode com capital inicial,
    capital final, contagens e atribuição de wallets.
    """
    try:
        async with await _open_session() as session:
            from polymarket_bot.db.models import WeeklySnapshot

            rows = (
                await session.execute(
                    select(WeeklySnapshot)
                    .order_by(desc(WeeklySnapshot.started_at))
                    .limit(limit)
                )
            ).scalars().all()
    except SQLAlchemyError:
        return _empty_payload({"snapshots": []})

    snapshots = [
        {
            "id": s.id,
            "week_iso": s.week_iso,
            "is_paper": s.is_paper,
            "started_at": _iso(s.started_at),
            "ended_at": _iso(s.ended_at),
            "capital_start_usd": _to_float(s.capital_start_usd),
            "capital_end_usd": _to_float(s.capital_end_usd),
            "pnl_usd": round(
                _to_float(s.capital_end_usd) - _to_float(s.capital_start_usd), 2
            ),
            "pnl_pct": round(
                (
                    (_to_float(s.capital_end_usd) - _to_float(s.capital_start_usd))
                    / max(_to_float(s.capital_start_usd), 1e-9)
                )
                * 100,
                2,
            ),
            "pnl_realized_usd": _to_float(s.pnl_realized_usd),
            "pnl_unrealized_at_close_usd": _to_float(s.pnl_unrealized_at_close_usd),
            "n_positions_opened": int(s.n_positions_opened),
            "n_positions_closed": int(s.n_positions_closed),
            "n_wins": int(s.n_wins),
            "n_losses": int(s.n_losses),
            "n_force_closed": int(s.n_force_closed),
            "wallets_followed": list(s.wallets_followed or []),
        }
        for s in rows
    ]
    return {"snapshots": snapshots, "db_offline": False}


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

            # Carregar todas as positions fechadas para atribuir P&L por wallet.
            # Atribuição 50/50: cada wallet em followed_wallets leva pnl/N.
            closed_rows = (
                await session.execute(
                    select(Position)
                    .where(Position.status == PositionStatus.CLOSED)
                    .where(Position.realized_pnl_usd.isnot(None))
                )
            ).scalars().all()

            wallet_pnl: dict[str, float] = defaultdict(float)
            wallet_wins: dict[str, int] = defaultdict(int)
            wallet_losses: dict[str, int] = defaultdict(int)
            for p in closed_rows:
                addrs = list(p.followed_wallets or [])
                if not addrs:
                    continue
                pnl = _to_float(p.realized_pnl_usd)
                share = pnl / len(addrs)
                # Win/loss atribuído por position completa (não por share),
                # para reflectir "esta wallet entrou em N trades vencedoras".
                is_win = pnl > _BREAKEVEN_USD_THRESHOLD
                is_loss = pnl < -_BREAKEVEN_USD_THRESHOLD
                for addr in addrs:
                    wallet_pnl[addr] += share
                    if is_win:
                        wallet_wins[addr] += 1
                    elif is_loss:
                        wallet_losses[addr] += 1

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

                addr = w.address
                bot_pnl = wallet_pnl.get(addr, 0.0)
                bot_wins = wallet_wins.get(addr, 0)
                bot_losses = wallet_losses.get(addr, 0)
                bot_total = bot_wins + bot_losses
                bot_win_rate = bot_wins / bot_total if bot_total > 0 else None

                results.append(
                    {
                        "address": addr,
                        "address_short": addr[:10],
                        "tier": w.current_tier.value if w.current_tier else None,
                        "profile_url": f"https://polymarket.com/profile/{addr}",
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
                        # P&L atribuído pelo bot (50/50 em consenso)
                        "bot_pnl_usd": round(bot_pnl, 2),
                        "bot_wins": bot_wins,
                        "bot_losses": bot_losses,
                        "bot_win_rate": bot_win_rate,
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
    max_per_event_pct = CONST.MAX_RATIO_PER_EVENT * 100   # 15%
    max_per_position_pct = CONST.MAX_SIZE_RATIO * 100     # 8%
    max_open_positions = CONST.MAX_OPEN_POSITIONS

    cb_payload = {
        "status": CircuitBreakerStatus.NORMAL.value,
        "reason": None,
        "consecutive_negative_weeks": 0,
        "size_reduction_factor": 1.0,
        "triggered_at": None,
        "resumes_at": None,
    }

    open_rows: list[Position] = []
    capital_now: float = CAPITAL_INITIAL

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

            pnl_total_realized = (
                await session.execute(
                    select(func.coalesce(func.sum(Position.realized_pnl_usd), 0)).where(
                        Position.status == PositionStatus.CLOSED
                    )
                )
            ).scalar() or 0
            capital_now = CAPITAL_INITIAL + _to_float(pnl_total_realized)

            open_rows = (
                await session.execute(
                    select(Position).where(
                        Position.status.in_(
                            [PositionStatus.OPEN, PositionStatus.PARTIALLY_CLOSED]
                        )
                    )
                )
            ).scalars().all()
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
                    "max_per_event_pct": max_per_event_pct,
                    "max_per_position_pct": max_per_position_pct,
                    "by_wallet": [],
                    "open_positions_count": 0,
                    "open_positions_limit": max_open_positions,
                },
            }
        )

    allocated_total = sum(_to_float(p.size_usd) for p in open_rows)
    base = capital_now if capital_now > 0 else CAPITAL_INITIAL
    total_alloc_pct = (allocated_total / base) * 100 if base else 0.0
    cash_pct = max(0.0, 100.0 - total_alloc_pct)

    # Atribuição 50/50: cada wallet em followed_wallets leva size/len(wallets).
    # Um trade de consenso (2 wallets) reparte 50/50; solo wallet leva tudo.
    wallet_alloc: dict[str, float] = defaultdict(float)
    for p in open_rows:
        wallets = list(p.followed_wallets or [])
        if not wallets:
            continue
        share = _to_float(p.size_usd) / len(wallets)
        for w in wallets:
            wallet_alloc[w] += share

    by_wallet = [
        {
            "address": addr,
            "address_short": addr[:6],
            "allocated_usd": round(val, 2),
            "allocated_pct": round((val / base) * 100, 2) if base else 0.0,
            "limit_pct": max_per_position_pct,
        }
        for addr, val in sorted(wallet_alloc.items(), key=lambda kv: kv[1], reverse=True)
    ]

    return {
        "circuit_breaker": cb_payload,
        "exposure": {
            "capital_current": round(base, 2),
            "total_allocated_usd": round(allocated_total, 2),
            "total_allocated_pct": round(total_alloc_pct, 2),
            "cash_reserve_pct": round(cash_pct, 2),
            "cash_reserve_limit_pct": cash_reserve_pct_limit,
            "max_per_event_pct": max_per_event_pct,
            "max_per_position_pct": max_per_position_pct,
            "by_wallet": by_wallet,
            "open_positions_count": len(open_rows),
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
