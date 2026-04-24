"""Cliente async para a Polymarket Data API.

Endpoints usados (payloads inspecionados em 2026-04-24):

- `/trades?user=<address>&limit=<n>` — trades executados. Campos:
  ``proxyWallet``, ``side`` (BUY/SELL), ``conditionId`` (market), ``asset``
  (tokenId), ``size`` (shares), ``price`` ([0,1]), ``timestamp`` (unix s),
  ``outcome``, ``eventSlug``, ``title``, ``transactionHash``.
  **Não contém P&L nem categoria** — isso vem de `/positions`.
- `/positions?user=<address>` — posições open + recentemente resolvidas, com
  ``realizedPnl``, ``cashPnl``, ``initialValue``, ``currentValue``,
  ``endDate``, ``redeemable``. Esta é a fonte de verdade para P&L realizado.
- `/activity?user=<address>` — feed combinado (TRADE/REDEEM). Tem ``usdcSize``
  mas também não expõe P&L per-evento — redundante vs /trades para scoring.
- `/v1/leaderboard?category=OVERALL&timePeriod=<WEEK|MONTH|ALL>&orderBy=PNL`
  — ranking de wallets por PnL (discovery; paginação com offset, limit máx 50).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import aiohttp
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from polymarket_bot.config import get_settings
from polymarket_bot.db.enums import TradeSide


@dataclass(frozen=True)
class RawTrade:
    """Trade bruto devolvido pela Data API, normalizado para a nossa tipagem."""

    wallet_address: str
    tx_hash: str
    market_id: str
    category: str | None
    side: TradeSide
    outcome: str
    price: Decimal
    size: Decimal
    usd_value: Decimal
    executed_at: datetime
    realized_pnl_usd: Decimal | None


@dataclass(frozen=True)
class RawPosition:
    """Posição devolvida por `/positions` — única fonte de P&L realizado."""

    wallet_address: str
    condition_id: str
    event_slug: str
    title: str
    outcome: str
    category: str | None
    size: Decimal
    initial_value_usd: Decimal
    current_value_usd: Decimal
    cash_pnl_usd: Decimal
    realized_pnl_usd: Decimal
    end_date: datetime | None
    is_resolved: bool

    @property
    def effective_pnl_usd(self) -> Decimal:
        """P&L efectivo para scoring: usa cashPnl se resolvida, senão realizedPnl."""
        return self.cash_pnl_usd if self.is_resolved else self.realized_pnl_usd


@dataclass(frozen=True)
class WalletProfitSummary:
    wallet_address: str
    profit_usd: Decimal
    trades_count: int
    volume_usd: Decimal


class PolymarketDataError(Exception):
    """Erros recuperáveis da Data API — desencadeiam retry."""


class PolymarketDataClient:
    """Cliente async com retry exponencial conforme §9.1 (5s → 30s → ...)."""

    def __init__(self, base_url: str | None = None, session: aiohttp.ClientSession | None = None):
        settings = get_settings()
        self._base_url = (base_url or settings.polymarket_data_api_url).rstrip("/")
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "PolymarketDataClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"Accept": "application/json"},
            )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=5, max=60),
        retry=retry_if_exception_type((aiohttp.ClientError, PolymarketDataError)),
        reraise=True,
    )
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        assert self._session is not None, "Use como async context manager"
        url = f"{self._base_url}{path}"
        async with self._session.get(url, params=params) as resp:
            if resp.status >= 500:
                raise PolymarketDataError(f"{resp.status} em {url}")
            if resp.status == 429:
                raise PolymarketDataError(f"Rate-limited em {url}")
            resp.raise_for_status()
            return await resp.json()

    # ---------------------------------------------------------------- trades
    async def get_wallet_trades(
        self,
        wallet_address: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[RawTrade]:
        """Devolve trades de uma wallet ordenadas por `executed_at` desc.

        Filtra localmente por `since` em vez de confiar em parâmetros de query
        inconsistentes entre endpoints.
        """
        params: dict[str, Any] = {"user": wallet_address.lower(), "limit": limit}
        raw = await self._get("/trades", params=params)

        trades = [self._parse_trade(wallet_address, entry) for entry in raw or []]
        trades = [t for t in trades if t is not None]
        if since is not None:
            trades = [t for t in trades if t.executed_at >= since]
        return trades

    async def get_wallet_trades_for_scoring(
        self, wallet_address: str, window_weeks: int = 4
    ) -> list[RawTrade]:
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=window_weeks)
        return await self.get_wallet_trades(wallet_address, since=cutoff, limit=1000)

    # ------------------------------------------------------------- positions
    async def get_wallet_positions(
        self, wallet_address: str, limit: int = 500
    ) -> list[RawPosition]:
        """Posições open + recentemente resolvidas (fonte de P&L realizado).

        NOTA: este endpoint retorna sobretudo *open + redeemable* — winners já
        reclamados desaparecem do payload. Para histórico completo de P&L
        usar `get_wallet_positions_from_activity` (reconstrução por cash-flow).
        """
        params: dict[str, Any] = {"user": wallet_address.lower(), "limit": limit}
        raw = await self._get("/positions", params=params)
        positions = [self._parse_position(wallet_address, entry) for entry in raw or []]
        return [p for p in positions if p is not None]

    # ------------------------------------------------------------- activity
    _ACTIVITY_PAGE_SIZE = 500
    _ACTIVITY_MAX_PAGES = 20  # 20 × 500 = 10k eventos — chega para 4 semanas

    async def get_wallet_activity(
        self,
        wallet_address: str,
        since: datetime | None = None,
        max_events: int | None = None,
    ) -> list[dict[str, Any]]:
        """Devolve eventos brutos de `/activity` (TRADE, REDEEM, …) paginados.

        Filtra por ``since`` localmente; pára quando o página devolve eventos
        mais antigos que o cutoff (a API ordena desc por timestamp).
        """
        cap = max_events or self._ACTIVITY_PAGE_SIZE * self._ACTIVITY_MAX_PAGES
        cutoff_ts = int(since.timestamp()) if since else None
        events: list[dict[str, Any]] = []
        for page in range(self._ACTIVITY_MAX_PAGES):
            offset = page * self._ACTIVITY_PAGE_SIZE
            params: dict[str, Any] = {
                "user": wallet_address.lower(),
                "limit": self._ACTIVITY_PAGE_SIZE,
                "offset": offset,
            }
            raw = await self._get("/activity", params=params)
            if not raw:
                break
            events.extend(raw)
            oldest_ts = min((int(e.get("timestamp") or 0) for e in raw), default=0)
            if cutoff_ts is not None and oldest_ts < cutoff_ts:
                break
            if len(raw) < self._ACTIVITY_PAGE_SIZE or len(events) >= cap:
                break
        if cutoff_ts is not None:
            events = [e for e in events if int(e.get("timestamp") or 0) >= cutoff_ts]
        return events

    async def get_wallet_positions_from_activity(
        self,
        wallet_address: str,
        window_weeks: int = 4,
    ) -> list[RawPosition]:
        """Reconstrói posições por agregação de cash-flow em `/activity`.

        Para cada `conditionId`, soma:
        - `+usdcSize` em SELLs e REDEEMs (entradas de cash)
        - `-usdcSize` em BUYs (saídas de cash)
        Quando o saldo de shares cai a ~0, a posição está realizada e
        `pnl = sum(cash_flow)`. Posições ainda open são ignoradas (sem P&L).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=window_weeks)
        events = await self.get_wallet_activity(wallet_address, since=cutoff)
        return _positions_from_activity_events(wallet_address, events)

    async def get_realized_positions(
        self,
        wallet_address: str,
        window_weeks: int = 4,
    ) -> list[RawPosition]:
        """Visão completa de P&L realizado: combina activity + /positions.

        - `/activity` (cash-flow) cobre **vencedoras** (closed por SELL/REDEEM).
        - `/positions` (snapshot) cobre **perdedoras resolvidas** que nunca foram
          redeemed (shares ainda no wallet, currentValue=0, endDate no passado).
          Sem este merge, o win-rate inflaciona artificialmente para 100%.
        Cutoff por ``end_date`` (último evento ou endDate da posição).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=window_weeks)
        activity_positions, snapshot_positions = await asyncio.gather(
            self.get_wallet_positions_from_activity(wallet_address, window_weeks),
            self.get_wallet_positions(wallet_address),
        )
        snapshot_by_cid = {p.condition_id: p for p in snapshot_positions}
        merged_by_cid: dict[str, RawPosition] = {}
        for p in activity_positions:
            snapshot = snapshot_by_cid.get(p.condition_id)
            # Quando a snapshot diz que a posição já resolveu (currentValue=0 e
            # endDate passada) mas a activity ainda a vê open (sem REDEEM), é
            # tipicamente uma perdedora — preferimos o cashPnl da snapshot.
            if snapshot is not None and snapshot.is_resolved and not p.is_resolved:
                merged_by_cid[p.condition_id] = snapshot
            else:
                merged_by_cid[p.condition_id] = p
        # Adiciona perdedoras que nunca apareceram em /activity (raro, mas possível
        # quando a janela de activity não chega ao BUY original).
        for cid, snapshot in snapshot_by_cid.items():
            if cid in merged_by_cid or not snapshot.is_resolved:
                continue
            if snapshot.end_date is not None and snapshot.end_date < cutoff:
                continue
            merged_by_cid[cid] = snapshot
        return list(merged_by_cid.values())

    # -------------------------------------------------- discovery (top wallets)
    _LEADERBOARD_PATH = "/v1/leaderboard"
    _LEADERBOARD_PAGE_SIZE = 50  # máximo aceite pela Data API
    _LEADERBOARD_MAX_OFFSET = 1000

    async def get_top_wallets_by_profit(
        self, window_weeks: int = 4, top_n: int = 150
    ) -> list[WalletProfitSummary]:
        """Leaderboard de wallets por profit nas últimas N semanas.

        Usado no scoring de domingo para construir o universo candidato.
        Endpoint: GET /v1/leaderboard — `limit` máx 50, paginação via `offset`.
        """
        time_period = self._window_to_time_period(window_weeks)
        results: list[WalletProfitSummary] = []
        offset = 0
        while len(results) < top_n and offset <= self._LEADERBOARD_MAX_OFFSET:
            page_size = min(self._LEADERBOARD_PAGE_SIZE, top_n - len(results))
            params = {
                "category": "OVERALL",
                "timePeriod": time_period,
                "orderBy": "PNL",
                "limit": page_size,
                "offset": offset,
            }
            raw = await self._get(self._LEADERBOARD_PATH, params=params)
            page = [self._parse_profit_summary(entry) for entry in raw or []]
            if not page:
                break
            results.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return results[:top_n]

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _window_to_time_period(weeks: int) -> str:
        if weeks <= 1:
            return "WEEK"
        if weeks <= 4:
            return "MONTH"
        return "ALL"

    @staticmethod
    def _parse_trade(wallet_address: str, entry: dict[str, Any]) -> RawTrade | None:
        """Parse dum item de `/trades` para `RawTrade`.

        O payload real da Data API (2026-04) usa ``proxyWallet``,
        ``transactionHash``, ``conditionId``, ``timestamp`` (unix int), e **não**
        inclui ``realizedPnl`` ou ``category``. Categoria é inferida do
        ``eventSlug``/``title``; P&L fica ``None`` (vem depois de `/positions`).
        """
        try:
            side_raw = (entry.get("side") or entry.get("type") or "").upper()
            if side_raw not in ("BUY", "SELL"):
                # Linhas não-trade (REDEEM, SPLIT, MERGE em /activity) — ignorar.
                return None
            side = TradeSide.BUY if side_raw == "BUY" else TradeSide.SELL

            ts_raw = entry.get("timestamp") or entry.get("transactionTimestamp")
            if ts_raw is None:
                return None
            if isinstance(ts_raw, str):
                executed_at = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            else:
                executed_at = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)

            price = Decimal(str(entry.get("price", "0")))
            size = Decimal(str(entry.get("size", entry.get("amount", "0"))))
            usd_value = Decimal(
                str(entry.get("usdcSize") or entry.get("usdValue") or price * size)
            )

            market_id = str(
                entry.get("conditionId")
                or entry.get("marketId")
                or entry.get("market")
                or entry.get("asset")
                or ""
            )
            wallet_raw = entry.get("proxyWallet") or wallet_address
            event_slug = str(entry.get("eventSlug") or entry.get("slug") or "")
            title = str(entry.get("title") or "")
            category = (
                entry.get("category")
                or entry.get("marketCategory")
                or _infer_category(event_slug, title)
            )
            # P&L não existe em /trades — fica None e é preenchido via /positions.
            realized = entry.get("realizedPnl")

            return RawTrade(
                wallet_address=str(wallet_raw).lower(),
                tx_hash=str(entry.get("transactionHash") or entry.get("txHash") or ""),
                market_id=market_id,
                category=category,
                side=side,
                outcome=str(entry.get("outcome") or "YES").strip() or "YES",
                price=price,
                size=size,
                usd_value=usd_value,
                executed_at=executed_at,
                realized_pnl_usd=Decimal(str(realized)) if realized is not None else None,
            )
        except (ValueError, TypeError, KeyError):
            return None

    @staticmethod
    def _parse_profit_summary(entry: dict[str, Any]) -> WalletProfitSummary:
        wallet = (
            entry.get("proxyWallet")
            or entry.get("user")
            or entry.get("address")
            or ""
        )
        profit = entry.get("pnl", entry.get("profit", "0"))
        volume = entry.get("vol", entry.get("volume", "0"))
        return WalletProfitSummary(
            wallet_address=str(wallet).lower(),
            profit_usd=Decimal(str(profit)),
            trades_count=int(entry.get("trades", 0)),
            volume_usd=Decimal(str(volume)),
        )

    @staticmethod
    def _parse_position(
        wallet_address: str, entry: dict[str, Any]
    ) -> RawPosition | None:
        """Parse dum item de `/positions`.

        Usa ``cashPnl`` (realized + unrealized) e ``realizedPnl``. Marca como
        resolved quando a posição já não pode ir a lado nenhum (``currentValue``
        zero ou ``endDate`` no passado).
        """
        try:
            wallet_raw = entry.get("proxyWallet") or wallet_address
            condition_id = str(entry.get("conditionId") or entry.get("asset") or "")
            event_slug = str(entry.get("eventSlug") or entry.get("slug") or "")
            title = str(entry.get("title") or "")
            size = Decimal(str(entry.get("size", "0")))
            initial_value = Decimal(str(entry.get("initialValue", "0")))
            current_value = Decimal(str(entry.get("currentValue", "0")))
            cash_pnl = Decimal(str(entry.get("cashPnl", "0")))
            realized_pnl = Decimal(str(entry.get("realizedPnl", "0")))

            end_date_raw = entry.get("endDate")
            end_date: datetime | None = None
            if isinstance(end_date_raw, str) and end_date_raw:
                try:
                    end_date = datetime.fromisoformat(
                        end_date_raw.replace("Z", "+00:00")
                    )
                    if end_date.tzinfo is None:
                        end_date = end_date.replace(tzinfo=timezone.utc)
                except ValueError:
                    end_date = None

            now = datetime.now(timezone.utc)
            ended = end_date is not None and end_date <= now
            fully_exited = current_value == 0 and size == 0
            is_resolved = bool(ended or fully_exited or entry.get("redeemable"))

            return RawPosition(
                wallet_address=str(wallet_raw).lower(),
                condition_id=condition_id,
                event_slug=event_slug,
                title=title,
                outcome=str(entry.get("outcome") or ""),
                category=_infer_category(event_slug, title),
                size=size,
                initial_value_usd=initial_value,
                current_value_usd=current_value,
                cash_pnl_usd=cash_pnl,
                realized_pnl_usd=realized_pnl,
                end_date=end_date,
                is_resolved=is_resolved,
            )
        except (ValueError, TypeError, KeyError):
            return None


# ---------------------------------------------- category inference (heurística)
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "SPORTS": (
        "nba", "nfl", "mlb", "nhl", "ufc", "pga", "f1", "formula",
        "epl", "premier-league", "champions-league", "la-liga", "serie-a",
        "bundesliga", "ligue-1", "world-cup", "super-bowl", "march-madness",
        "olympics", "tennis", "boxing", "soccer", "basketball", "football",
        "hockey", "baseball", "cricket", "golf",
    ),
    "POLITICS": (
        "trump", "biden", "harris", "election", "congress", "senate",
        "president", "governor", "primary", "republican", "democrat",
        "nominee", "impeach", "putin", "zelensky", "supreme-court",
    ),
    "CRYPTO": (
        "btc", "eth", "bitcoin", "ethereum", "solana", "sol-",
        "crypto", "coinbase", "binance", "memecoin", "stablecoin",
    ),
    "ECONOMICS": (
        "fed-", "fomc", "cpi", "inflation", "gdp", "rates", "recession",
        "unemployment", "jobs-report", "interest-rate",
    ),
    "CULTURE": (
        "oscar", "grammy", "emmy", "cannes", "movie-", "netflix",
        "song-of-the-year", "album-", "taylor-swift", "tiktok",
    ),
    "WEATHER": (
        "weather", "hurricane", "storm", "temperature", "snow", "rain",
    ),
    "TECH": (
        "openai", "chatgpt", "anthropic", "tesla", "apple", "microsoft",
        "google", "meta-", "nvidia", "ai-",
    ),
}


def _infer_category(event_slug: str, title: str = "") -> str | None:
    """Mapeia eventSlug/title para uma das categorias do leaderboard.

    Heurística por keywords — se não bater, devolve None (wallet poderá falhar
    o filtro de diversificação, mas isso é preferível a inventar categoria).
    """
    blob = f"{event_slug} {title}".lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in blob:
                return category
    return None


# Tolerância na verificação "shares ~ 0" — Polymarket retorna shares com 6+ casas
# decimais e arredondamentos podem deixar resíduos minúsculos.
_CLOSED_POSITION_SHARES_TOLERANCE = Decimal("0.01")


def _positions_from_activity_events(
    wallet_address: str, events: list[dict[str, Any]]
) -> list[RawPosition]:
    """Agrega eventos de `/activity` em posições com P&L realizado por mercado."""
    by_condition: dict[str, dict[str, Any]] = {}
    for ev in events:
        condition_id = str(ev.get("conditionId") or "")
        if not condition_id:
            continue
        ev_type = str(ev.get("type") or "").upper()
        side = str(ev.get("side") or "").upper()
        size = Decimal(str(ev.get("size", "0")))
        usdc = Decimal(str(ev.get("usdcSize", "0")))
        ts_raw = ev.get("timestamp") or 0
        try:
            ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            ts = datetime.now(timezone.utc)

        bucket = by_condition.setdefault(
            condition_id,
            {
                "shares": Decimal("0"),
                "cash_flow": Decimal("0"),
                "buy_usdc": Decimal("0"),
                "sell_usdc": Decimal("0"),
                "redeem_usdc": Decimal("0"),
                "last_ts": ts,
                "event_slug": str(ev.get("eventSlug") or ""),
                "title": str(ev.get("title") or ""),
                "outcome": str(ev.get("outcome") or ""),
            },
        )

        if ev_type == "TRADE" and side == "BUY":
            bucket["shares"] += size
            bucket["cash_flow"] -= usdc
            bucket["buy_usdc"] += usdc
        elif ev_type == "TRADE" and side == "SELL":
            bucket["shares"] -= size
            bucket["cash_flow"] += usdc
            bucket["sell_usdc"] += usdc
        elif ev_type == "REDEEM":
            # Em REDEEM, `size` representa shares queimadas; `usdcSize` o payout.
            bucket["shares"] -= size
            bucket["cash_flow"] += usdc
            bucket["redeem_usdc"] += usdc
        else:
            # MAKER_REBATE / REWARD / SPLIT / MERGE — não afectam P&L de mercado.
            continue

        if ts > bucket["last_ts"]:
            bucket["last_ts"] = ts

    positions: list[RawPosition] = []
    for condition_id, b in by_condition.items():
        is_closed = abs(b["shares"]) < _CLOSED_POSITION_SHARES_TOLERANCE
        cash_flow = b["cash_flow"]
        category = _infer_category(b["event_slug"], b["title"])
        positions.append(
            RawPosition(
                wallet_address=wallet_address.lower(),
                condition_id=condition_id,
                event_slug=b["event_slug"],
                title=b["title"],
                outcome=b["outcome"],
                category=category,
                size=b["shares"],
                initial_value_usd=b["buy_usdc"],
                current_value_usd=Decimal("0") if is_closed else b["buy_usdc"],
                cash_pnl_usd=cash_flow if is_closed else Decimal("0"),
                realized_pnl_usd=cash_flow if is_closed else Decimal("0"),
                end_date=b["last_ts"],
                is_resolved=is_closed,
            )
        )
    return positions
