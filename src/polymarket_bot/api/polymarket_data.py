"""Cliente async para a Polymarket Data API.

Endpoints usados:
- `/trades?user=<address>&limit=<n>` — histórico de trades por wallet
- `/positions?user=<address>` — posições actuais
- `/activity?user=<address>` — feed de atividade (inclui P&L realizado)
- `/profit?user=<address>&interval=<period>` — profit agregado (usado para discovery)

NOTA: os endpoints exatos e nomes de parâmetros devem ser verificados contra a
documentação ao vivo antes de deploy. O cliente abstrai a resposta em dataclasses
para que o algoritmo de scoring não dependa do formato raw. Se a Data API
devolver pouca informação histórica, cair para `polygon_rpc` como fallback
(ver §6 da conversação original).
"""

from __future__ import annotations

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

    # -------------------------------------------------- discovery (top wallets)
    async def get_top_wallets_by_profit(
        self, window_weeks: int = 4, top_n: int = 150
    ) -> list[WalletProfitSummary]:
        """Leaderboard de wallets por profit nas últimas N semanas.

        Usado no scoring de domingo para construir o universo candidato.
        """
        params = {
            "interval": self._window_to_interval(window_weeks),
            "limit": top_n,
            "sortBy": "profit",
            "order": "desc",
        }
        raw = await self._get("/profit/leaderboard", params=params)
        return [self._parse_profit_summary(entry) for entry in raw or []]

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _window_to_interval(weeks: int) -> str:
        if weeks <= 1:
            return "1w"
        if weeks <= 4:
            return "1m"
        return "all"

    @staticmethod
    def _parse_trade(wallet_address: str, entry: dict[str, Any]) -> RawTrade | None:
        try:
            side_raw = (entry.get("side") or entry.get("type") or "").upper()
            side = TradeSide.BUY if side_raw in ("BUY", "TAKER_BUY") else TradeSide.SELL
            ts_raw = entry.get("timestamp") or entry.get("transactionTimestamp")
            if ts_raw is None:
                return None
            executed_at = (
                datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if isinstance(ts_raw, str)
                else datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
            )
            price = Decimal(str(entry.get("price", "0")))
            size = Decimal(str(entry.get("size", entry.get("amount", "0"))))
            usd_value = Decimal(str(entry.get("usdValue", price * size)))
            realized = entry.get("realizedPnl")
            return RawTrade(
                wallet_address=wallet_address.lower(),
                tx_hash=str(entry.get("txHash") or entry.get("transactionHash") or ""),
                market_id=str(entry.get("marketId") or entry.get("market") or ""),
                category=entry.get("category") or entry.get("marketCategory"),
                side=side,
                outcome=str(entry.get("outcome", "")).upper() or "YES",
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
        return WalletProfitSummary(
            wallet_address=str(entry.get("user") or entry.get("address") or "").lower(),
            profit_usd=Decimal(str(entry.get("profit", "0"))),
            trades_count=int(entry.get("trades", 0)),
            volume_usd=Decimal(str(entry.get("volume", "0"))),
        )
