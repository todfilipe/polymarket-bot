"""Geradores de MarketSnapshot / OrderBook para testes de mercado."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_bot.market.models import MarketSnapshot, OrderBook, OrderBookLevel


def make_orderbook(
    market_id: str = "m1",
    outcome: str = "YES",
    best_bid: str = "0.39",
    best_ask: str = "0.41",
    depth_size: str = "200",
    levels: int = 5,
    step: str = "0.01",
) -> OrderBook:
    """Order book sintético com `levels` níveis simétricos."""
    bid_price = Decimal(best_bid)
    ask_price = Decimal(best_ask)
    step_d = Decimal(step)
    size = Decimal(depth_size)

    bids = tuple(
        OrderBookLevel(price=bid_price - step_d * i, size=size) for i in range(levels)
    )
    asks = tuple(
        OrderBookLevel(price=ask_price + step_d * i, size=size) for i in range(levels)
    )
    return OrderBook(market_id=market_id, outcome=outcome, bids=bids, asks=asks)


def make_snapshot(
    market_id: str = "m1",
    volume_usd: str = "150000",
    days_to_resolution: float = 14,
    best_bid: str = "0.39",
    best_ask: str = "0.41",
    category: str | None = "Politics",
    outcome: str = "YES",
    depth_size: str = "200",
    token_id: str | None = None,
) -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    return MarketSnapshot(
        market_id=market_id,
        slug=f"slug-{market_id}",
        question=f"Q: {market_id}?",
        category=category,
        outcome=outcome,
        volume_usd=Decimal(volume_usd),
        resolves_at=now + timedelta(days=days_to_resolution),
        orderbook=make_orderbook(
            market_id=market_id,
            outcome=outcome,
            best_bid=best_bid,
            best_ask=best_ask,
            depth_size=depth_size,
        ),
        fetched_at=now,
        token_id=token_id,
    )
