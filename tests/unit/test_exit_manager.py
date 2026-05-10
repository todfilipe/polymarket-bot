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
    resolution: tuple[bool, str | None] = (False, None),
) -> ExitManager:
    market_builder = MagicMock()
    market_builder.build = AsyncMock(return_value=snapshot)
    market_builder.get_resolution_status = AsyncMock(return_value=resolution)

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
async def test_resolved_win_credits_correct_pnl(sessionmaker):
    """Mercado resolveu YES e a posição era YES → fecha com P&L positivo
    proporcional ao entry price. Em paper, $100 a 0.40 → resolve a $1 → +$150."""
    await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.40", outcome="YES"
    )
    # Snapshot não importa — o auto-settle dispara antes do fetch.
    manager = _make_exit_manager(
        sessionmaker, snapshot=None, resolution=(True, "YES")
    )

    [result] = await manager.check_all_positions()

    assert result.exit_reason == ExitReason.RESOLVED_WIN
    assert result.skipped is False
    # P&L = size × (1/entry - 1) = 100 × (1/0.40 - 1) = 100 × 1.5 = 150
    assert result.pnl_usd == Decimal("150")
    # Não deve registar como stop loss (não é panic exit)
    manager._circuit_breaker.record_stop_loss.assert_not_awaited()

    async with sessionmaker() as s:
        fresh = await s.get(Position, result.position_id)
        assert fresh.status == PositionStatus.CLOSED
        assert fresh.realized_pnl_usd == Decimal("150")
        assert fresh.notes == ExitReason.RESOLVED_WIN.value


@pytest.mark.asyncio
async def test_resolved_loss_records_full_size_loss(sessionmaker):
    """Mercado resolveu NO mas a posição era YES → posição vai a 0,
    P&L = -size_usd."""
    await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.40", outcome="YES"
    )
    manager = _make_exit_manager(
        sessionmaker, snapshot=None, resolution=(True, "NO")
    )

    [result] = await manager.check_all_positions()

    assert result.exit_reason == ExitReason.RESOLVED_LOSS
    assert result.pnl_usd == Decimal("-100")
    # Não deve disparar circuit breaker — resolução natural ≠ stop loss
    manager._circuit_breaker.record_stop_loss.assert_not_awaited()

    async with sessionmaker() as s:
        fresh = await s.get(Position, result.position_id)
        assert fresh.status == PositionStatus.CLOSED
        assert fresh.realized_pnl_usd == Decimal("-100")


@pytest.mark.asyncio
async def test_unresolved_market_falls_through_to_normal_checks(sessionmaker):
    """Mercado ainda activo → auto-settle salta, evaluate continua para
    stop loss / take profit / etc."""
    await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.50"
    )
    snap = _snapshot(mid_price="0.55")  # +10% gain, nada dispara
    manager = _make_exit_manager(
        sessionmaker, snapshot=snap, resolution=(False, None)
    )

    [result] = await manager.check_all_positions()

    assert result.skipped is True
    assert result.exit_reason is None


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
async def test_take_profit_partial_closes_half_on_50pct_gain(sessionmaker):
    """Trigger por LUCRO ≥ +50%, não por preço absoluto. Entry 0.40,
    current 0.80 → +100% gain → bem acima do trigger."""
    pos = await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.40"
    )
    snap = _snapshot(mid_price="0.80")  # +100% gain
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
async def test_high_entry_price_no_movement_does_not_trigger_partial(sessionmaker):
    """Bug fix: entrar a 0.77 e o preço continuar a 0.77 não deve disparar
    partial (lucro = 0%). Antes disparava porque o trigger era preço ≥ 0.75."""
    await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.77"
    )
    snap = _snapshot(mid_price="0.77")
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    [result] = await manager.check_all_positions()

    assert result.skipped is True
    assert result.exit_reason is None


@pytest.mark.asyncio
async def test_small_profit_does_not_trigger_partial(sessionmaker):
    """Lucro <50% (mesmo perto da resolução) deixa correr — temporal exit
    foi removido em favor de hold-to-resolution."""
    # entry=0.40, current=0.45 → +12.5% lucro (abaixo do trigger +50%)
    await _seed_open_position(
        sessionmaker, size_usd="100", avg_entry_price="0.40"
    )
    snap = _snapshot(mid_price="0.45", hours_to_resolution=3.0)
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    [result] = await manager.check_all_positions()

    assert result.skipped is True
    assert result.exit_reason is None


@pytest.mark.asyncio
async def test_in_loss_with_short_resolution_does_not_exit(sessionmaker):
    """Em perda perto da resolução já não é fechada (era TEMPORAL_EXIT)."""
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
async def test_wallet_exit_follows_regardless_of_timing_skill(
    sessionmaker, monkeypatch
):
    """No modo copytrade puro, qualquer wallet exit dispara o fecho —
    o threshold de timing_skill_ratio foi removido. Confiamos na wallet
    para sair tal como confiámos para entrar."""
    await _seed_open_position(
        sessionmaker,
        size_usd="100",
        avg_entry_price="0.40",
        followed_wallets=["0xabc"],
    )
    snap = _snapshot(mid_price="0.42", hours_to_resolution=48.0)
    manager = _make_exit_manager(sessionmaker, snapshot=snap)

    async def _detect(position):
        # Skill baixo (0.30) — antes seria ignorado; agora segue.
        return [WalletExitCandidate(wallet_address="0xabc", timing_skill_ratio=0.30)]

    monkeypatch.setattr(manager, "_detect_wallet_exits", _detect)

    [result] = await manager.check_all_positions()

    assert result.exit_reason == ExitReason.WALLET_EXIT


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
