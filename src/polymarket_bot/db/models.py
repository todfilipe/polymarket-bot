"""Entidades SQLAlchemy async.

Responsabilidades:
- `Wallet` + `WalletScore`: universo monitorizado e scores históricos.
- `WalletTrade`: histórico raw de trades extraído do Data API (usado pelo scoring).
- `Market`: metadata de mercados.
- `BotTrade`: ordens submetidas pelo bot (paper ou live).
- `Position`: posições abertas do bot.
- `DedupHash`: hashes para prevenir execução duplicada (§9.2).
- `CircuitBreakerState`: estado de pausa/halt semanal.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from polymarket_bot.db.enums import (
    BotTradeStatus,
    CircuitBreakerStatus,
    PositionStatus,
    TradeSide,
    WalletTier,
)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Wallet(Base, TimestampMixin):
    __tablename__ = "wallets"

    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    is_followed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    current_tier: Mapped[WalletTier | None] = mapped_column(Enum(WalletTier), nullable=True)
    timing_skill_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    scores: Mapped[list["WalletScore"]] = relationship(back_populates="wallet")
    trades: Mapped[list["WalletTrade"]] = relationship(back_populates="wallet")


class WalletScore(Base, TimestampMixin):
    __tablename__ = "wallet_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(
        ForeignKey("wallets.address"), index=True, nullable=False
    )
    scored_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    window_weeks: Mapped[int] = mapped_column(Integer, nullable=False)

    # Score composto (0-100)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)

    # Métricas individuais — úteis para debugging e auditoria
    profit_factor: Mapped[float] = mapped_column(Float, nullable=False)
    consistency_pct: Mapped[float] = mapped_column(Float, nullable=False)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False)
    max_drawdown: Mapped[float] = mapped_column(Float, nullable=False)
    categories_count: Mapped[int] = mapped_column(Integer, nullable=False)
    recency_score: Mapped[float] = mapped_column(Float, nullable=False)

    # Contexto
    total_trades: Mapped[int] = mapped_column(Integer, nullable=False)
    total_volume_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    luck_concentration: Mapped[float] = mapped_column(Float, nullable=False)
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    exclusion_reasons: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    wallet: Mapped[Wallet] = relationship(back_populates="scores")


class WalletTrade(Base):
    """Trades raw da Polymarket (input para scoring)."""

    __tablename__ = "wallet_trades"
    __table_args__ = (UniqueConstraint("wallet_address", "tx_hash", name="uq_wallet_tx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(
        ForeignKey("wallets.address"), index=True, nullable=False
    )
    tx_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    market_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)

    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)   # YES / NO
    price: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    size: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    usd_value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    executed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    # P&L realizado por trade — preenchido após resolução/fecho
    realized_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)

    wallet: Mapped[Wallet] = relationship(back_populates="trades")


class Market(Base, TimestampMixin):
    __tablename__ = "markets"

    market_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), index=True)
    resolves_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    volume_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)


class BotTrade(Base, TimestampMixin):
    """Trade do bot — paper ou live."""

    __tablename__ = "bot_trades"
    __table_args__ = (
        UniqueConstraint("dedup_hash", name="uq_bot_trade_dedup"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedup_hash: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)

    followed_wallet_address: Mapped[str] = mapped_column(String(64), nullable=False)
    market_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)

    intended_price: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    executed_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    size_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)

    expected_value: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[BotTradeStatus] = mapped_column(Enum(BotTradeStatus), nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    clob_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Position(Base, TimestampMixin):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide), nullable=False)
    status: Mapped[PositionStatus] = mapped_column(
        Enum(PositionStatus), nullable=False, index=True
    )
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False)

    size_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    entries_count: Mapped[int] = mapped_column(Integer, default=1)

    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    realized_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)

    followed_wallets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # clobTokenIds[index] do outcome — necessário para submeter SELLs live
    # ao CLOB. `None` em posições criadas em modo paper antes da migração live.
    token_id: Mapped[str | None] = mapped_column(String(120), nullable=True)


class DedupHash(Base):
    """Hash único por trade para prevenir duplicados (§9.2)."""

    __tablename__ = "dedup_hashes"

    hash_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class CircuitBreakerState(Base, TimestampMixin):
    __tablename__ = "circuit_breaker_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[CircuitBreakerStatus] = mapped_column(
        Enum(CircuitBreakerStatus), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resumes_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_negative_weeks: Mapped[int] = mapped_column(Integer, default=0)
    size_reduction_factor: Mapped[float] = mapped_column(Float, default=1.0)
