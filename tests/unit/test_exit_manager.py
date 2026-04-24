"""Testes do ExitManager — DB in-memory, MarketBuilder + Notifier mockados."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

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
    Position,
    Wallet,
)
from polymarket_bot.market.models import (
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
)
from polymarket_bot.monitoring import exit_manager as em_mod
from polymarket_bot.monitoring.exit_manager import (
    ExitManager,
    ExitReason,
    WalletExitCandidate,
)


# ----- fixtures / helpers ---------------------------------------------------


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


def _snapshot(
    *,
    mid_price: str,
    hours_to_resolution: float = 48.0,
    market_id: str = "m1",
    outcome: str = "YES",
) -> MarketSnapshot:
    price = Decimal(mid_price)
    half_spread = Decimal("0.005")
    bid = max(price - half_spread, Decimal("0.01"))
    ask = min(price + half_spread, Decimal("0.99"))
    book = OrderBook(
        market_id=market_id,
        outcome=outcome,
        bids=(OrderBookLevel(price=bid, size=Decimal("500")),),
        asks=(OrderBookLevel(price=ask, size=Decimal("500")),),
    )
    now = datetime.now(timezone.utc)
    return MarketSnapshot(
        market_id=market_id,
        slug=f"slug-{market_id}",
        question=f"Will {market_id} resolve YES?",
        category="politics",
        outcome=outcome,
        volume_usd=Decimal("200000"),
        resolves_at=now + timedelta(hours=hours_to_resolution),
        orderbook=book,
        fetched_at=now,
    )


async def _seed_open_position(
    sm,
    *,
    size_usd: str = "50",
    avg_entry_price: str = "0.40",
    entries_count: int = 1,
    market_id: str = "m1",
    outcome: str = "YES",
    followed_wallets: list[str] | None = None,
    notes: str | None = None,
    token_id: str | None = None,
) -> Position:
    now = datetime.now(timezone.utc)
    pos = Position(
        market_id=market_id,
        outcome=outcome,
        side=TradeSide.BUY,
        status=PositionStatus.OPEN,
        is_paper=True,
        size_usd=Decimal(size_usd),
        avg_entry_price=Decimal(avg_entry_price),
        entries_count=entries_count,
        opened_at=now - timedelta(hours=3),
        closed_at=None,
        realized_pnl_usd=None,
        followed_wallets=followed_wallets or [],
        notes=notes,
        token_id=token_id,
    )
    async with sm() as s:
        s.add(pos)
        await s.commit()
        await s.refresh(pos)
    return pos


def _make_exit_manager(
    sm,
    *,
    snapshot: MarketSnapshot | None = None,
    live_mode: bool = False,
    circuit_breaker=None,
    notifier=None,
) -> ExitManager:
    market_builder = MagicMock()
    market_builder.build = AsyncMock(return_value=snapshot)

    cb = circuit_breaker or MagicMock()
    cb.record_stop_loss = AsyncMock()
    cb.get_status = AsyncMock(return_value=CircuitBreakerStatus.NORMAL)

    notif = notifier or MagicMock()
    notif.stop_loss_triggered = AsyncMock()
    notif.critical_error = AsyncMock()
    notif.send = AsyncMock()

    clob_factory = MagicMock(return_value=MagicMock())

    return ExitManager(
        session_factory=sm,
        clob_client_factory=clob_factory,
        market_builder=market_builder,
        notifier=notif,
        circuit_breaker=cb,
        live_mode=live_mode,
    )


# ----- tests ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_open_positions_returns_empty_list(sessionmaker):
    manager = _make_exit_manager(sessionmaker, snapshot=_snapshot(mid_price="0.40"))
    results = await manager.check_all_positions()
    assert results == []


@pytest.mark.asyncio
async def test_hard_stop_loss_at_minus_41pct_closes_full(sessionmaker):
    # avg_entry=0.50, current=0.295 → razão ≈ -41%
    await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.50"
    )
    snap = _snapshot(mid_price="0.295")
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    [result] = await manager.check_all_positions()

    assert result.exit_reason == ExitReason.HARD_STOP_LOSS
    assert result.skipped is False
    assert result.pnl_usd < 0
    manager._circuit_breaker.record_stop_loss.assert_awaited_once()
    manager._notifier.stop_loss_triggered.assert_awaited_once()

    async with sessionmaker() as s:
        fresh = await s.get(Position, result.position_id)
        assert fresh.status == PositionStatus.CLOSED
        assert fresh.realized_pnl_usd is not None
        assert fresh.notes == ExitReason.HARD_STOP_LOSS.value


@pytest.mark.asyncio
async def test_pnl_at_minus_39pct_does_not_exit(sessionmaker):
    # avg_entry=0.50, current=0.305 → razão ≈ -39%
    await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.50"
    )
    snap = _snapshot(mid_price="0.305")
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    [result] = await manager.check_all_positions()

    assert result.skipped is True
    assert result.exit_reason is None
    manager._circuit_breaker.record_stop_loss.assert_not_awaited()


@pytest.mark.asyncio
async def test_take_profit_partial_closes_half_on_price_75(sessionmaker):
    pos = await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.40"
    )
    snap = _snapshot(mid_price="0.80")  # ≥ 0.75
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    [result] = await manager.check_all_positions()

    assert result.exit_reason == ExitReason.TAKE_PROFIT_PARTIAL
    assert result.pnl_usd > 0

    async with sessionmaker() as s:
        parent = await s.get(Position, pos.id)
        assert parent.size_usd == Decimal("50")   # 100 - 50%
        assert parent.entries_count == 2
        assert parent.status == PositionStatus.PARTIALLY_CLOSED
        assert parent.notes == "partial_taken"


@pytest.mark.asyncio
async def test_take_profit_does_not_repeat_when_partial_already_taken(sessionmaker):
    await _seed_open_position(
        sessionmaker,
        size_usd="50",
        avg_entry_price="0.40",
        notes="partial_taken",
    )
    snap = _snapshot(mid_price="0.82")
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    [result] = await manager.check_all_positions()

    assert result.skipped is True
    assert result.exit_reason is None


@pytest.mark.asyncio
async def test_temporal_exit_when_hours_under_6_and_in_profit(sessionmaker):
    # avg_entry=0.40, current=0.45 → em lucro.
    await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.40"
    )
    snap = _snapshot(mid_price="0.45", hours_to_resolution=3.0)
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    [result] = await manager.check_all_positions()

    assert result.exit_reason == ExitReason.TEMPORAL_EXIT
    assert result.pnl_usd > 0


@pytest.mark.asyncio
async def test_temporal_exit_skipped_when_in_loss(sessionmaker):
    # avg_entry=0.50, current=0.45 → em perda (mas < -40%, logo não é SL).
    await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.50"
    )
    snap = _snapshot(mid_price="0.45", hours_to_resolution=3.0)
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    [result] = await manager.check_all_positions()

    assert result.skipped is True
    assert result.exit_reason is None


@pytest.mark.asyncio
async def test_wallet_exit_when_timing_skill_above_threshold(sessionmaker, monkeypatch):
    await _seed_open_position(
        sessionmaker,
        size_usd="100",
        avg_entry_price="0.40",
        followed_wallets=["0xabc"],
    )
    snap = _snapshot(mid_price="0.42", hours_to_resolution=48.0)
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    async def _detect(position):
        return [WalletExitCandidate(wallet_address="0xabc", timing_skill_ratio=0.65)]

    monkeypatch.setattr(manager, "_detect_wallet_exits", _detect)

    [result] = await manager.check_all_positions()

    assert result.exit_reason == ExitReason.WALLET_EXIT


@pytest.mark.asyncio
async def test_wallet_exit_ignored_when_timing_skill_below_threshold(
    sessionmaker, monkeypatch
):
    await _seed_open_position(
        sessionmaker,
        size_usd="100",
        avg_entry_price="0.40",
        followed_wallets=["0xabc"],
    )
    snap = _snapshot(mid_price="0.42", hours_to_resolution=48.0)
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    async def _detect(position):
        return [WalletExitCandidate(wallet_address="0xabc", timing_skill_ratio=0.50)]

    monkeypatch.setattr(manager, "_detect_wallet_exits", _detect)

    [result] = await manager.check_all_positions()

    assert result.skipped is True
    assert result.exit_reason is None


@pytest.mark.asyncio
async def test_hard_stop_loss_takes_priority_over_take_profit(sessionmaker):
    """Impossível na prática (mid≥0.80 E pnl_ratio ≤ -40% requerem entry>1.33),
    mas verificamos a hierarquia forçando o cenário via avg_entry alto."""
    await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="1.5"  # entry acima do range
    )
    # current 0.80 vs entry 1.5 → ratio ≈ -47%.
    snap = _snapshot(mid_price="0.80")
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    [result] = await manager.check_all_positions()

    assert result.exit_reason == ExitReason.HARD_STOP_LOSS
    manager._circuit_breaker.record_stop_loss.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_all_positions_is_idempotent(sessionmaker):
    await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.50"
    )
    snap = _snapshot(mid_price="0.295")  # stop loss
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    first = await manager.check_all_positions()
    second = await manager.check_all_positions()

    assert len(first) == 1
    assert first[0].exit_reason == ExitReason.HARD_STOP_LOSS
    # Na segunda chamada a posição já não está OPEN — não aparece.
    assert second == []
    manager._circuit_breaker.record_stop_loss.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_mode_stub_marks_pending_manual(sessionmaker):
    pos = await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.50"
    )
    snap = _snapshot(mid_price="0.295")
    manager = _make_exit_manager(sessionmaker, snapshot=snap, live_mode=True)

    [result] = await manager.check_all_positions()

    assert result.skipped is True
    assert "live exit" in (result.skip_reason or "")
    manager._notifier.critical_error.assert_awaited_once()

    async with sessionmaker() as s:
        fresh = await s.get(Position, pos.id)
        # Posição NÃO foi fechada — fail-safe.
        assert fresh.status == PositionStatus.OPEN
        assert fresh.notes == "exit_pending_manual"


@pytest.mark.asyncio
async def test_wallet_timing_skill_helper_reads_from_wallet_table(sessionmaker):
    async with sessionmaker() as s:
        s.add(
            Wallet(
                address="0xdef".ljust(42, "0"),
                timing_skill_ratio=0.72,
            )
        )
        await s.commit()

    manager = _make_exit_manager(sessionmaker, snapshot=_snapshot(mid_price="0.40"))
    skill = await manager._wallet_timing_skill("0xdef".ljust(42, "0"))

    assert skill == pytest.approx(0.72)


# ---- Live SELL submission tests -------------------------------------------

@pytest.fixture
def fast_poll_exit(monkeypatch):
    """`asyncio.sleep` → no-op no módulo exit_manager."""
    async def _no_sleep(_secs):
        return None

    monkeypatch.setattr(em_mod.asyncio, "sleep", _no_sleep)


def _make_live_exit_manager(sm, *, snapshot, clob_client) -> ExitManager:
    """Construtor focado para testes live — factory devolve o clob mockado."""
    market_builder = MagicMock()
    market_builder.build = AsyncMock(return_value=snapshot)

    cb = MagicMock()
    cb.record_stop_loss = AsyncMock()
    cb.get_status = AsyncMock(return_value=CircuitBreakerStatus.NORMAL)

    notif = MagicMock()
    notif.stop_loss_triggered = AsyncMock()
    notif.critical_error = AsyncMock()
    notif.send = AsyncMock()

    return ExitManager(
        session_factory=sm,
        clob_client_factory=lambda: clob_client,
        market_builder=market_builder,
        notifier=notif,
        circuit_breaker=cb,
        live_mode=True,
    )


@pytest.mark.asyncio
async def test_live_sell_success_closes_position_with_pnl(
    sessionmaker, fast_poll_exit
):
    """Hard stop loss em live mode → SELL submetida e `FILLED` no primeiro poll."""
    pos = await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.50", token_id="tok-xyz"
    )
    snap = _snapshot(mid_price="0.295")  # razão ≈ -41% → hard SL

    clob = MagicMock()
    clob.create_order = MagicMock(return_value={"signed": True})
    clob.post_order = MagicMock(
        return_value={"success": True, "orderID": "0xSELL_OK"}
    )
    clob.get_order = MagicMock(return_value={"status": "FILLED"})
    clob.cancel = MagicMock()

    manager = _make_live_exit_manager(sessionmaker, snapshot=snap, clob_client=clob)
    [result] = await manager.check_all_positions()

    assert result.exit_reason == ExitReason.HARD_STOP_LOSS
    assert result.skipped is False
    assert result.pnl_usd < 0
    clob.create_order.assert_called_once()
    clob.post_order.assert_called_once()
    clob.cancel.assert_not_called()

    async with sessionmaker() as s:
        fresh = await s.get(Position, pos.id)
        assert fresh.status == PositionStatus.CLOSED
        assert fresh.realized_pnl_usd == result.pnl_usd
        assert fresh.notes == ExitReason.HARD_STOP_LOSS.value


@pytest.mark.asyncio
async def test_live_sell_timeout_marks_pending_manual_and_alerts(
    sessionmaker, fast_poll_exit
):
    """60s sem FILLED → cancel + `exit_pending_manual` + alerta Telegram."""
    pos = await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.50", token_id="tok-xyz"
    )
    snap = _snapshot(mid_price="0.295")

    clob = MagicMock()
    clob.create_order = MagicMock(return_value={"signed": True})
    clob.post_order = MagicMock(
        return_value={"success": True, "orderID": "0xSELL_TO"}
    )
    clob.get_order = MagicMock(return_value={"status": "OPEN"})
    clob.cancel = MagicMock()

    manager = _make_live_exit_manager(sessionmaker, snapshot=snap, clob_client=clob)
    [result] = await manager.check_all_positions()

    assert result.skipped is True
    assert "live exit" in (result.skip_reason or "")
    clob.cancel.assert_called_once_with("0xSELL_TO")
    manager._notifier.critical_error.assert_awaited_once()

    async with sessionmaker() as s:
        fresh = await s.get(Position, pos.id)
        # fail-safe: posição mantém-se OPEN, nota sinaliza intervenção manual
        assert fresh.status == PositionStatus.OPEN
        assert fresh.notes == "exit_pending_manual"
