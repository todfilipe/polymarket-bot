"""Testes do ExposureGuard — DB in-memory.

Cobre as 3 verificações (10 posições, cash reserve, limite por evento) +
integração com o ExecutionPipeline (gate opcional).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from polymarket_bot.db.enums import PositionStatus, TradeSide, WalletTier
from polymarket_bot.db.models import Base, Position
from polymarket_bot.execution.pipeline import (
    ExecutionPipeline,
    PipelineContext,
    PipelineOutcome,
    SignalInput,
)
from polymarket_bot.market import WalletSignal
from polymarket_bot.portfolio.exposure_guard import ExposureGuard
from tests.fixtures.markets import make_snapshot


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


def _open_pos(
    *,
    market_id: str,
    size_usd: str,
    outcome: str = "YES",
) -> Position:
    return Position(
        market_id=market_id,
        outcome=outcome,
        side=TradeSide.BUY,
        status=PositionStatus.OPEN,
        is_paper=True,
        size_usd=Decimal(size_usd),
        avg_entry_price=Decimal("0.40"),
        entries_count=1,
        opened_at=datetime.now(timezone.utc),
        closed_at=None,
        realized_pnl_usd=None,
        followed_wallets=[],
    )


async def _add(sessionmaker, *positions: Position) -> None:
    async with sessionmaker() as s:
        for p in positions:
            s.add(p)
        await s.commit()


# ---- Direct ExposureGuard tests -----------------------------------------


@pytest.mark.asyncio
async def test_passes_when_no_open_positions(sessionmaker):
    guard = ExposureGuard(session_factory=sessionmaker)
    market = make_snapshot(market_id="evt_a")

    result = await guard.check(
        market=market, size_usd=Decimal("50"), bankroll_usd=Decimal("1000")
    )

    assert result.passes
    assert result.reason is None
    assert result.open_positions == 0


@pytest.mark.asyncio
async def test_rejects_when_10_open_positions(sessionmaker):
    positions = [
        _open_pos(market_id=f"evt_{i}", size_usd="20") for i in range(10)
    ]
    await _add(sessionmaker, *positions)

    guard = ExposureGuard(session_factory=sessionmaker)
    market = make_snapshot(market_id="evt_new")

    result = await guard.check(
        market=market, size_usd=Decimal("20"), bankroll_usd=Decimal("1000")
    )

    assert not result.passes
    assert "máx. 10 posições abertas" in (result.reason or "")
    assert result.open_positions == 10


@pytest.mark.asyncio
async def test_rejects_when_cash_reserve_below_30_pct(sessionmaker):
    # $720 deployed spread across multiple events → nova $60 ⇒ free = $220 < 30%.
    await _add(
        sessionmaker,
        _open_pos(market_id="evt_a", size_usd="120"),
        _open_pos(market_id="evt_b", size_usd="120"),
        _open_pos(market_id="evt_c", size_usd="120"),
        _open_pos(market_id="evt_d", size_usd="120"),
        _open_pos(market_id="evt_e", size_usd="120"),
        _open_pos(market_id="evt_f", size_usd="120"),
    )

    guard = ExposureGuard(session_factory=sessionmaker)
    market = make_snapshot(market_id="evt_new")

    result = await guard.check(
        market=market, size_usd=Decimal("60"), bankroll_usd=Decimal("1000")
    )

    assert not result.passes
    assert "cash reserve" in (result.reason or "")
    assert "22.0%" in (result.reason or "")


@pytest.mark.asyncio
async def test_passes_when_cash_reserve_at_or_above_30_pct(sessionmaker):
    # $600 deployed + nova $50 ⇒ free = $350 = 35%.
    await _add(
        sessionmaker,
        _open_pos(market_id="evt_a", size_usd="150"),
        _open_pos(market_id="evt_b", size_usd="150"),
        _open_pos(market_id="evt_c", size_usd="150"),
        _open_pos(market_id="evt_d", size_usd="150"),
    )

    guard = ExposureGuard(session_factory=sessionmaker)
    market = make_snapshot(market_id="evt_new")

    result = await guard.check(
        market=market, size_usd=Decimal("50"), bankroll_usd=Decimal("1000")
    )

    assert result.passes, result.reason


@pytest.mark.asyncio
async def test_rejects_when_event_exposure_exceeds_15_pct(sessionmaker):
    # $130 no mesmo evento + nova $30 = $160 = 16% > 15%.
    await _add(
        sessionmaker,
        _open_pos(market_id="evt_same", size_usd="130"),
    )

    guard = ExposureGuard(session_factory=sessionmaker)
    market = make_snapshot(market_id="evt_same")

    result = await guard.check(
        market=market, size_usd=Decimal("30"), bankroll_usd=Decimal("1000")
    )

    assert not result.passes
    assert "evento" in (result.reason or "")
    assert "16.0%" in (result.reason or "")


@pytest.mark.asyncio
async def test_passes_when_event_exposure_within_15_pct(sessionmaker):
    # $100 + nova $40 = $140 = 14%.
    await _add(
        sessionmaker,
        _open_pos(market_id="evt_same", size_usd="100"),
    )

    guard = ExposureGuard(session_factory=sessionmaker)
    market = make_snapshot(market_id="evt_same")

    result = await guard.check(
        market=market, size_usd=Decimal("40"), bankroll_usd=Decimal("1000")
    )

    assert result.passes, result.reason
    assert result.event_exposure_pct == pytest.approx(0.14, rel=1e-6)


@pytest.mark.asyncio
async def test_different_event_does_not_count_towards_limit(sessionmaker):
    # $140 num evento diferente → nova $140 num evento novo passa
    # (mesmo que somados ultrapassassem 15%).
    await _add(
        sessionmaker,
        _open_pos(market_id="evt_other", size_usd="140"),
    )

    guard = ExposureGuard(session_factory=sessionmaker)
    market = make_snapshot(market_id="evt_new")

    result = await guard.check(
        market=market, size_usd=Decimal("140"), bankroll_usd=Decimal("1000")
    )

    assert result.passes, result.reason
    assert result.event_exposure_pct == pytest.approx(0.14, rel=1e-6)


@pytest.mark.asyncio
async def test_event_key_groups_yes_and_no_via_42_char_prefix(sessionmaker):
    # condition_id comum (42+ chars) com sufixos "-YES"/"-NO" → mesmo evento.
    base = "0x" + "a" * 40  # 42 chars exactos
    await _add(
        sessionmaker,
        _open_pos(market_id=base + "-YES", size_usd="100"),
    )

    guard = ExposureGuard(session_factory=sessionmaker)
    market = make_snapshot(market_id=base + "-NO", outcome="NO")

    result = await guard.check(
        market=market, size_usd=Decimal("60"), bankroll_usd=Decimal("1000")
    )

    assert not result.passes
    assert "evento" in (result.reason or "")


# ---- Pipeline integration -----------------------------------------------


@dataclass
class FakeSubmissionResult:
    success: bool = True
    is_duplicate: bool = False
    is_paper: bool = True
    reason: str | None = None
    clob_order_id: str | None = "paper-1"
    executed_price: Decimal | None = Decimal("0.41")
    bot_trade_id: int | None = 1


@dataclass
class FakeOrderManager:
    result: FakeSubmissionResult = field(default_factory=FakeSubmissionResult)
    calls: list[dict] = field(default_factory=list)

    async def submit(self, **kwargs) -> FakeSubmissionResult:
        self.calls.append(kwargs)
        return self.result


def _signals_two_top() -> list[SignalInput]:
    return [
        SignalInput(
            signal=WalletSignal(
                wallet_address="0xA", tier=WalletTier.TOP, outcome="YES"
            ),
            win_rate=0.65,
        ),
        SignalInput(
            signal=WalletSignal(
                wallet_address="0xB", tier=WalletTier.TOP, outcome="YES"
            ),
            win_rate=0.70,
        ),
    ]


@pytest.mark.asyncio
async def test_pipeline_none_guard_passes_through(sessionmaker):
    """Se exposure_guard=None, o gate é ignorado e o submit acontece."""
    # Há 10 posições abertas — se o gate corresse, rejeitaria.
    await _add(
        sessionmaker,
        *[_open_pos(market_id=f"e{i}", size_usd="20") for i in range(10)],
    )

    om = FakeOrderManager()
    pipeline = ExecutionPipeline(order_manager=om, exposure_guard=None)
    ctx = PipelineContext(
        market=make_snapshot(depth_size="5000"),
        signals=_signals_two_top(),
        bankroll_usd=Decimal("1000"),
    )

    result = await pipeline.evaluate(ctx)

    assert result.outcome == PipelineOutcome.EXECUTED
    assert len(om.calls) == 1


@pytest.mark.asyncio
async def test_pipeline_skipped_exposure_when_guard_rejects(sessionmaker):
    await _add(
        sessionmaker,
        *[_open_pos(market_id=f"e{i}", size_usd="20") for i in range(10)],
    )
    guard = ExposureGuard(session_factory=sessionmaker)
    om = FakeOrderManager()
    pipeline = ExecutionPipeline(order_manager=om, exposure_guard=guard)

    ctx = PipelineContext(
        market=make_snapshot(depth_size="5000"),
        signals=_signals_two_top(),
        bankroll_usd=Decimal("1000"),
    )

    result = await pipeline.evaluate(ctx)

    assert result.outcome == PipelineOutcome.SKIPPED_EXPOSURE
    assert "10 posições" in (result.reason or "")
    assert om.calls == []
