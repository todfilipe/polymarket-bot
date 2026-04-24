"""Domain types para mercados e order book.

Desacoplado do formato raw da CLOB — a camada API converte para estas
dataclasses antes de entrarem na lógica de filtros/EV/consensus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal


@dataclass(frozen=True)
class OrderBookLevel:
    price: Decimal
    size: Decimal  # tamanho disponível a este preço


@dataclass(frozen=True)
class OrderBook:
    """Book de um outcome (YES ou NO) isoladamente.

    Convenção: bids ordenados por preço desc, asks ordenados por preço asc.
    """

    market_id: str
    outcome: str
    bids: tuple[OrderBookLevel, ...] = field(default_factory=tuple)
    asks: tuple[OrderBookLevel, ...] = field(default_factory=tuple)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal(2)

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class MarketSnapshot:
    """Snapshot de um mercado no momento de avaliação para trade."""

    market_id: str
    slug: str | None
    question: str
    category: str | None
    outcome: str                       # YES | NO (lado em que avaliamos entrar)
    volume_usd: Decimal
    resolves_at: datetime
    orderbook: OrderBook
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    token_id: str | None = None       # clobTokenIds[YES=0, NO=1] — necessário para submissão CLOB

    @property
    def implied_probability(self) -> Decimal | None:
        """Probabilidade implícita ≈ mid price (convenção de prediction markets)."""
        return self.orderbook.mid_price

    @property
    def hours_to_resolution(self) -> float:
        delta = self.resolves_at - self.fetched_at
        return max(delta.total_seconds() / 3600.0, 0.0)

    @property
    def days_to_resolution(self) -> float:
        return self.hours_to_resolution / 24.0
