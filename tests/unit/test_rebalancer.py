"""Testes do RebalancingScheduler — jobs isolados, colaboradores mockados.

DB in-memory para os testes de persistência; para o resto, `AsyncMock` em todos
os colaboradores externos (portfolio, notifier, circuit_breaker, monitor,
discovery).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from polymarket_bot.db.enums import CircuitBreakerStatus, WalletTier
from polymarket_bot.db.models import (
    Base,
    Wallet,
    WalletScore,
)
from polymarket_bot.notifications.telegram_notifier import WeeklyReport
from polymarket_bot.scheduler.rebalancer import RebalancingScheduler
from polymarket_bot.wallets.discovery import DiscoveryReport


# ----- helpers -----------------------------------------------------------


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


def _fake_scored(address: str, score: float = 75.0, eligible: bool = True):
    """Constrói um `ScoredWallet`-like minimal — duck-typed para o scheduler."""
    metrics = SimpleNamespace(
        profit_factor=2.0,
        consistency_pct=0.7,
        win_rate=0.65,
        max_drawdown=0.15,
        categories={"politics", "sports"},
        recency_score=0.8,
        total_trades=120,
        total_volume_usd=Decimal("5000"),
        luck_concentration=0.3,
    )
    eligibility = SimpleNamespace(is_eligible=eligible, reasons=[])
    return SimpleNamespace(
        wallet_address=address.lower(),
        total_score=score,
        metrics=metrics,
        eligibility=eligibility,
        is_selectable=eligible,
    )


def _make_scheduler(
    sessionmaker,
    *,
    portfolio=None,
    circuit_breaker=None,
    notifier=None,
    monitor=None,
    discovery_fn=None,
) -> RebalancingScheduler:
    portfolio = portfolio or MagicMock()
    circuit_breaker = circuit_breaker or MagicMock()
    notifier = notifier or MagicMock()
    monitor = monitor or MagicMock()

    # Método usado por `job_select_top7` — sempre AsyncMock.
    if not hasattr(monitor, "reload_followed_wallets") or not isinstance(
        monitor.reload_followed_wallets, AsyncMock
    ):
        monitor.reload_followed_wallets = AsyncMock()

    data_client = MagicMock()
    return RebalancingScheduler(
        monitor=monitor,
        portfolio=portfolio,
        circuit_breaker=circuit_breaker,
        notifier=notifier,
        data_client=data_client,
        session_factory=sessionmaker,
        discovery_fn=discovery_fn,
    )


# ----- tests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_weekly_report_calls_portfolio_and_notifier(sessionmaker):
    report = WeeklyReport(
        week_start=date(2026, 4, 20),
        pnl_usdc=Decimal("30"),
        pnl_pct=0.03,
        trades_won=3,
        trades_lost=1,
        trades_skipped=2,
    )
    portfolio = MagicMock()
    portfolio.get_weekly_report_data = AsyncMock(return_value=report)
    notifier = MagicMock()
    notifier.weekly_report = AsyncMock()

    scheduler = _make_scheduler(
        sessionmaker, portfolio=portfolio, notifier=notifier
    )
    await scheduler.job_weekly_report()

    portfolio.get_weekly_report_data.assert_awaited_once()
    notifier.weekly_report.assert_awaited_once_with(report)


@pytest.mark.asyncio
async def test_job_score_wallets_passes_weekly_pnl_to_circuit_breaker(sessionmaker):
    portfolio = MagicMock()
    portfolio.get_weekly_pnl_pct = AsyncMock(return_value=-0.04)
    cb = MagicMock()
    cb.resume_if_due = AsyncMock()
    cb.check_and_trigger = AsyncMock(return_value=CircuitBreakerStatus.NORMAL)

    discovery_result = DiscoveryReport(candidates=100, eligible=10, top_scored=[])
    discovery_fn = AsyncMock(return_value=discovery_result)

    scheduler = _make_scheduler(
        sessionmaker,
        portfolio=portfolio,
        circuit_breaker=cb,
        discovery_fn=discovery_fn,
    )
    await scheduler.job_score_wallets()

    cb.resume_if_due.assert_awaited_once()
    cb.check_and_trigger.assert_awaited_once_with(-0.04)
    discovery_fn.assert_awaited_once()
    assert scheduler._latest_scored == []


@pytest.mark.asyncio
async def test_job_select_top7_persists_wallets_and_reloads_monitor(sessionmaker):
    monitor = MagicMock()
    monitor.reload_followed_wallets = AsyncMock()
    scheduler = _make_scheduler(sessionmaker, monitor=monitor)

    addresses = [f"0x{i:040d}" for i in range(8)]
    scheduler._latest_scored = [_fake_scored(a, score=90 - i) for i, a in enumerate(addresses)]

    await scheduler.job_select_top7()

    monitor.reload_followed_wallets.assert_awaited_once()

    async with sessionmaker() as s:
        followed = (
            await s.execute(select(Wallet).where(Wallet.is_followed.is_(True)))
        ).scalars().all()
        assert len(followed) == 7
        tiers = [w.current_tier for w in followed]
        assert tiers.count(WalletTier.TOP) == 3
        assert tiers.count(WalletTier.BOTTOM) == 4

        scores = (await s.execute(select(WalletScore))).scalars().all()
        assert len(scores) == 7


@pytest.mark.asyncio
async def test_job_select_top7_marks_removed_wallets_as_unfollowed(sessionmaker):
    # Semeia uma wallet antiga que está a ser seguida mas NÃO aparece no novo top.
    async with sessionmaker() as s:
        s.add(
            Wallet(
                address="0xold".ljust(42, "0"),
                is_followed=True,
                current_tier=WalletTier.TOP,
            )
        )
        await s.commit()

    monitor = MagicMock()
    monitor.reload_followed_wallets = AsyncMock()
    scheduler = _make_scheduler(sessionmaker, monitor=monitor)

    new_addresses = [f"0x{i:040d}" for i in range(7)]
    scheduler._latest_scored = [_fake_scored(a, score=90 - i) for i, a in enumerate(new_addresses)]

    await scheduler.job_select_top7()

    async with sessionmaker() as s:
        old = await s.get(Wallet, "0xold".ljust(42, "0"))
        assert old is not None
        assert old.is_followed is False
        assert old.current_tier is None

    assert "0xold".ljust(42, "0") in scheduler._latest_removed_addresses


@pytest.mark.asyncio
async def test_safe_run_catches_exceptions_and_notifies(sessionmaker):
    notifier = MagicMock()
    notifier.critical_error = AsyncMock()
    scheduler = _make_scheduler(sessionmaker, notifier=notifier)

    async def boom() -> None:
        raise RuntimeError("job exploded")

    # Não deve propagar.
    await scheduler._safe_run("job_test", boom)

    notifier.critical_error.assert_awaited_once()
    args, kwargs = notifier.critical_error.call_args
    # O erro inclui a descrição e o módulo é "scheduler".
    assert kwargs.get("module") == "scheduler"
    assert "job_test" in kwargs.get("error", "")


@pytest.mark.asyncio
async def test_job_reset_weekly_state_only_resets_when_normal(sessionmaker):
    cb = MagicMock()
    cb.get_status = AsyncMock(return_value=CircuitBreakerStatus.NORMAL)
    cb.reset_consecutive_losses = AsyncMock()
    scheduler = _make_scheduler(sessionmaker, circuit_breaker=cb)

    await scheduler.job_reset_weekly_state()
    cb.reset_consecutive_losses.assert_awaited_once()

    # Mesma instância de scheduler: agora com status=HALTED → sem reset.
    cb.reset_consecutive_losses.reset_mock()
    cb.get_status = AsyncMock(return_value=CircuitBreakerStatus.HALTED)
    await scheduler.job_reset_weekly_state()
    cb.reset_consecutive_losses.assert_not_awaited()


@pytest.mark.asyncio
async def test_job_select_top7_skips_when_no_scored_results(sessionmaker):
    monitor = MagicMock()
    monitor.reload_followed_wallets = AsyncMock()
    scheduler = _make_scheduler(sessionmaker, monitor=monitor)

    # Sem scoring prévio.
    scheduler._latest_scored = []
    await scheduler.job_select_top7()

    monitor.reload_followed_wallets.assert_not_awaited()
