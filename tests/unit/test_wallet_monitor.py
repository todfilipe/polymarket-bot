"""Testes do WalletMonitor — foco em `_persist_skipped_trade`.

Regressão: o `_persist_skipped_trade` lia atributos errados de
`SizeDecision` (`size_usd` em vez de `final_size_usd`) e `EvResult`
(`expected_value` em vez de `margin`). Ambos lançavam AttributeError,
apanhado pelo `except Exception` no caller → skips eram notificados via
Telegram mas **nunca** persistidos em `bot_trades`.

Sintoma observado em produção: Telegram cheio de "skip: order book vazio
do lado pretendido" mas `COUNT(*) WHERE status='SKIPPED'` = 0.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from polymarket_bot.api.polymarket_data import PolymarketDataClient
from polymarket_bot.db.enums import BotTradeStatus, TradeSide, WalletTier
from polymarket_bot.db.models import Base, BotTrade
from polymarket_bot.execution.pipeline import (
    ExecutionPipeline,
    PipelineOutcome,
    PipelineResult,
)
from polymarket_bot.execution.sizer import SizeDecision
from polymarket_bot.market.consensus import ConsensusDecision, ConsensusStrength
from polymarket_bot.market.ev_calculator import EvResult
from polymarket_bot.market.models import MarketSnapshot, OrderBook, OrderBookLevel
from polymarket_bot.monitoring.signal_reader import DetectedSignal, FollowedWallet
from polymarket_bot.monitoring.wallet_monitor import WalletMonitor


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


def _make_market_snapshot() -> MarketSnapshot:
    book = OrderBook(
        market_id="0xabc",
        outcome="YES",
        bids=(OrderBookLevel(price=Decimal("0.40"), size=Decimal("100")),),
        asks=(),  # vazio → check_orderbook_depth devolve SKIPPED_DEPTH
    )
    return MarketSnapshot(
        market_id="0xabc",
        slug=None,
        question="test market",
        category=None,
        outcome="YES",
        volume_usd=Decimal("1000"),
        resolves_at=datetime.now(timezone.utc),
        orderbook=book,
        token_id="tok_y",
    )


def _make_signal() -> DetectedSignal:
    wallet = FollowedWallet(
        address="0x" + "a" * 40,
        tier=WalletTier.TOP,
        win_rate=0.65,
    )
    return DetectedSignal(
        wallet=wallet,
        market_id="0xabc",
        outcome="YES",
        price=Decimal("0.50"),
        detected_at=datetime.now(timezone.utc),
        tx_hash="0xtest" + "0" * 60,
        side=TradeSide.BUY,
        usd_value=Decimal("50"),
    )


def _make_skip_result_with_size_and_ev() -> PipelineResult:
    """Constrói um PipelineResult realista para um SKIPPED_DEPTH —
    `size` e `ev` ambos preenchidos (caso onde o bug rebentava)."""
    size = SizeDecision(
        bankroll_usd=Decimal("1000"),
        base_size_usd=Decimal("30"),
        tier_multiplier=1.4,
        signal_multiplier=0.5,
        recovery_multiplier=1.0,
        pre_cap_size_usd=Decimal("21"),
        final_size_usd=Decimal("21"),
        hit_cap=False,
        below_minimum=False,
        skip_reason=None,
    )
    ev = EvResult(
        stake_usd=Decimal("21"),
        entry_price=Decimal("0.50"),
        true_probability=Decimal("0.55"),
        potential_profit_usd=Decimal("21"),
        expected_value_usd=Decimal("2.1"),
        margin=0.10,
        passes_min_margin=True,
        reason=None,
    )
    consensus = ConsensusDecision(
        strength=ConsensusStrength.SOLO,
        direction_outcome="YES",
        n_wallets=1,
        n_top_tier=1,
        n_bottom_tier=0,
        tier_multiplier=1.4,
        signal_multiplier=0.5,
        reason=None,
    )
    return PipelineResult(
        outcome=PipelineOutcome.SKIPPED_DEPTH,
        reason="order book vazio do lado pretendido",
        consensus=consensus,
        size=size,
        ev=ev,
    )


def _make_monitor(sm) -> WalletMonitor:
    pipeline = MagicMock(spec=ExecutionPipeline)
    data_client = MagicMock(spec=PolymarketDataClient)
    return WalletMonitor(
        pipeline=pipeline,
        data_client=data_client,
        db_session_factory=sm,
    )


@pytest.mark.asyncio
async def test_persist_skipped_trade_inserts_bottrade_row(sessionmaker):
    """Regressão: skip com size+ev populados deve persistir em bot_trades.

    Antes do fix, `result.size.size_usd` lançava AttributeError → o except
    no caller silenciava e a row nunca chegava à DB.
    """
    monitor = _make_monitor(sessionmaker)
    market = _make_market_snapshot()
    signal = _make_signal()
    result = _make_skip_result_with_size_and_ev()

    await monitor._persist_skipped_trade(
        market=market, group=[signal], result=result
    )

    async with sessionmaker() as s:
        rows = list((await s.execute(select(BotTrade))).scalars().all())
        assert len(rows) == 1
        row = rows[0]
        assert row.status == BotTradeStatus.SKIPPED
        assert row.skip_reason == "order book vazio do lado pretendido"
        # Os 2 atributos que tinham bug:
        assert row.size_usd == Decimal("21")        # antes era 0 (AttributeError)
        assert row.expected_value == pytest.approx(0.10)  # antes era 0.0
        assert row.followed_wallet_address == signal.wallet.address.lower()
        assert row.market_id == "0xabc"
        assert row.side == TradeSide.BUY
        assert row.is_paper is True


@pytest.mark.asyncio
async def test_persist_skipped_trade_handles_missing_size_and_ev(sessionmaker):
    """Skips muito cedo (consenso/build) têm size=None e ev=None — não
    devem rebentar, devem persistir com 0."""
    monitor = _make_monitor(sessionmaker)
    market = _make_market_snapshot()
    signal = _make_signal()
    result = PipelineResult(
        outcome=PipelineOutcome.SKIPPED_NOISE,
        reason="filtro de ruído",
        size=None,
        ev=None,
    )

    await monitor._persist_skipped_trade(
        market=market, group=[signal], result=result
    )

    async with sessionmaker() as s:
        row = (await s.execute(select(BotTrade))).scalar_one()
        assert row.status == BotTradeStatus.SKIPPED
        assert row.size_usd == Decimal("0")
        assert row.expected_value == 0.0
        assert row.skip_reason == "filtro de ruído"
