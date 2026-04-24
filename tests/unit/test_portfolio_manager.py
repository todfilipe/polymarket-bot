"""Testes do PortfolioManager — DB in-memory igual aos outros testes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from polymarket_bot.db.enums import (
    CircuitBreakerStatus,
    PositionStatus,
    TradeSide,
)
from polymarket_bot.db.models import (
    Base,
    CircuitBreakerState,
    Position,
)
from polymarket_bot.portfolio.portfolio_manager import (
    PortfolioManager,
    _this_week_start_utc,
)


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


def _closed_position(
    *,
    pnl: str,
    closed_at: datetime,
    size_usd: str = "50",
    market_id: str = "m1",
    outcome: str = "YES",
) -> Position:
    return Position(
        market_id=market_id,
        outcome=outcome,
        side=TradeSide.BUY,
        status=PositionStatus.CLOSED,
        is_paper=True,
        size_usd=Decimal(size_usd),
        avg_entry_price=Decimal("0.40"),
        entries_count=1,
        opened_at=closed_at - timedelta(hours=2),
        closed_at=closed_at,
        realized_pnl_usd=Decimal(pnl),
        followed_wallets=[],
    )


def _open_position(market_id: str = "m_open") -> Position:
    return Position(
        market_id=market_id,
        outcome="YES",
        side=TradeSide.BUY,
        status=PositionStatus.OPEN,
        is_paper=True,
        size_usd=Decimal("40"),
        avg_entry_price=Decimal("0.40"),
        entries_count=1,
        opened_at=datetime.now(timezone.utc),
        closed_at=None,
        realized_pnl_usd=None,
        followed_wallets=[],
    )


# ---- Tests --------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_bankroll_with_zero_positions_equals_initial(sessionmaker):
    pm = PortfolioManager(sessionmaker, initial_capital_usd=Decimal("1000"))

    bankroll = await pm.get_bankroll()

    assert bankroll == Decimal("1000")


@pytest.mark.asyncio
async def test_get_bankroll_sums_realized_pnl_of_closed_positions(sessionmaker):
    now = datetime.now(timezone.utc)
    async with sessionmaker() as s:
        s.add(_closed_position(pnl="25.00", closed_at=now - timedelta(days=2)))
        s.add(_closed_position(pnl="-10.00", closed_at=now - timedelta(days=1)))
        s.add(_closed_position(pnl="50.00", closed_at=now - timedelta(hours=3)))
        # Abertas não contam.
        s.add(_open_position())
        await s.commit()

    pm = PortfolioManager(sessionmaker, initial_capital_usd=Decimal("1000"))
    bankroll = await pm.get_bankroll()

    assert bankroll == Decimal("1065.00")  # 1000 + 25 - 10 + 50


@pytest.mark.asyncio
async def test_get_bankroll_clamps_to_zero_if_heavily_negative(sessionmaker):
    now = datetime.now(timezone.utc)
    async with sessionmaker() as s:
        s.add(_closed_position(pnl="-2000.00", closed_at=now - timedelta(days=1)))
        await s.commit()

    pm = PortfolioManager(sessionmaker, initial_capital_usd=Decimal("1000"))
    bankroll = await pm.get_bankroll()

    assert bankroll == Decimal("0")


@pytest.mark.asyncio
async def test_get_weekly_pnl_only_counts_since_monday(sessionmaker):
    week_start = _this_week_start_utc()
    async with sessionmaker() as s:
        # Antes da semana — não conta.
        s.add(_closed_position(pnl="100.00", closed_at=week_start - timedelta(days=1)))
        # Dentro da semana — conta.
        s.add(_closed_position(pnl="20.00", closed_at=week_start + timedelta(hours=1)))
        s.add(_closed_position(pnl="-5.00", closed_at=week_start + timedelta(days=1)))
        await s.commit()

    pm = PortfolioManager(sessionmaker, initial_capital_usd=Decimal("1000"))
    pnl = await pm.get_weekly_pnl()

    assert pnl == Decimal("15.00")


@pytest.mark.asyncio
async def test_get_weekly_pnl_pct_uses_bankroll_at_week_start(sessionmaker):
    week_start = _this_week_start_utc()
    async with sessionmaker() as s:
        # Base ao início da semana: 1000 + 100 = 1100.
        s.add(_closed_position(pnl="100.00", closed_at=week_start - timedelta(days=1)))
        # P&L da semana: 55.
        s.add(_closed_position(pnl="55.00", closed_at=week_start + timedelta(hours=5)))
        await s.commit()

    pm = PortfolioManager(sessionmaker, initial_capital_usd=Decimal("1000"))
    pct = await pm.get_weekly_pnl_pct()

    assert pct == pytest.approx(55.0 / 1100.0, rel=1e-6)


@pytest.mark.asyncio
async def test_is_recovery_mode_true_when_latest_state_is_recovery(sessionmaker):
    async with sessionmaker() as s:
        s.add(
            CircuitBreakerState(
                status=CircuitBreakerStatus.RECOVERY,
                reason="2 stops",
                triggered_at=datetime.now(timezone.utc),
                consecutive_negative_weeks=0,
                size_reduction_factor=0.6,
            )
        )
        await s.commit()

    pm = PortfolioManager(sessionmaker)

    assert await pm.is_recovery_mode() is True


@pytest.mark.asyncio
async def test_is_recovery_mode_false_when_no_state_or_normal(sessionmaker):
    pm = PortfolioManager(sessionmaker)
    assert await pm.is_recovery_mode() is False

    async with sessionmaker() as s:
        s.add(
            CircuitBreakerState(
                status=CircuitBreakerStatus.NORMAL,
                reason="ok",
                triggered_at=datetime.now(timezone.utc),
                consecutive_negative_weeks=0,
                size_reduction_factor=1.0,
            )
        )
        await s.commit()

    assert await pm.is_recovery_mode() is False


@pytest.mark.asyncio
async def test_count_open_positions_ignores_closed(sessionmaker):
    now = datetime.now(timezone.utc)
    async with sessionmaker() as s:
        s.add(_open_position(market_id="a"))
        s.add(_open_position(market_id="b"))
        s.add(_open_position(market_id="c"))
        s.add(_closed_position(pnl="10", closed_at=now - timedelta(hours=1)))
        await s.commit()

    pm = PortfolioManager(sessionmaker)

    assert await pm.count_open_positions() == 3


@pytest.mark.asyncio
async def test_get_weekly_report_data_aggregates(sessionmaker):
    week_start = _this_week_start_utc()
    async with sessionmaker() as s:
        s.add(_closed_position(pnl="30", closed_at=week_start + timedelta(hours=1)))
        s.add(_closed_position(pnl="-10", closed_at=week_start + timedelta(hours=5)))
        await s.commit()

    pm = PortfolioManager(sessionmaker, initial_capital_usd=Decimal("1000"))
    report = await pm.get_weekly_report_data()

    assert report.week_start == week_start.date()
    assert report.pnl_usdc == Decimal("20")
    assert report.trades_won == 1
    assert report.trades_lost == 1
    assert report.trades_skipped == 0
    assert report.capital_current == Decimal("1020")
    assert report.capital_initial == Decimal("1000")
    assert report.pnl_pct == pytest.approx(20.0 / 1000.0, rel=1e-6)
