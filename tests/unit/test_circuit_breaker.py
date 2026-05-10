"""Testes do CircuitBreaker — DB in-memory + verificações das transições."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from polymarket_bot.db.enums import CircuitBreakerStatus
from polymarket_bot.db.models import (
    Base,
    CircuitBreakerState,
)
from polymarket_bot.risk.circuit_breaker import (
    CircuitBreaker,
    _next_sunday_2330_utc,
)


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


async def _all_states(sm) -> list[CircuitBreakerState]:
    async with sm() as session:
        return list(
            (
                await session.execute(
                    select(CircuitBreakerState).order_by(
                        CircuitBreakerState.triggered_at
                    )
                )
            ).scalars()
        )


async def _seed_state(
    sm,
    status: CircuitBreakerStatus,
    *,
    triggered_at: datetime | None = None,
    resumes_at: datetime | None = None,
    consecutive_negative_weeks: int = 0,
) -> None:
    async with sm() as session:
        session.add(
            CircuitBreakerState(
                status=status,
                reason="seed",
                triggered_at=triggered_at or datetime.now(timezone.utc),
                resumes_at=resumes_at,
                consecutive_negative_weeks=consecutive_negative_weeks,
                size_reduction_factor=(
                    0.6 if status == CircuitBreakerStatus.RECOVERY else 1.0
                ),
            )
        )
        await session.commit()


# ---- Tests --------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_pnl_stays_normal(sessionmaker):
    """NORMAL → NORMAL é idempotente: sem histórico permanece sem histórico."""
    cb = CircuitBreaker(sessionmaker)
    status = await cb.check_and_trigger(weekly_pnl_pct=0.03)

    assert status == CircuitBreakerStatus.NORMAL
    # get_status também deve devolver NORMAL (default).
    assert await cb.get_status() == CircuitBreakerStatus.NORMAL


@pytest.mark.asyncio
async def test_weekly_loss_over_threshold_triggers_paused(sessionmaker):
    cb = CircuitBreaker(sessionmaker)
    status = await cb.check_and_trigger(weekly_pnl_pct=-0.20)

    assert status == CircuitBreakerStatus.PAUSED
    rows = await _all_states(sessionmaker)
    assert rows[-1].status == CircuitBreakerStatus.PAUSED
    # `resumes_at` deve apontar para próximo domingo ~23:30.
    assert rows[-1].resumes_at is not None
    resumes = rows[-1].resumes_at.replace(tzinfo=timezone.utc)
    assert resumes > datetime.now(timezone.utc)
    target = _next_sunday_2330_utc()
    delta = abs((resumes - target).total_seconds())
    assert delta < 60


@pytest.mark.asyncio
async def test_weekly_loss_exactly_at_threshold_triggers_paused(sessionmaker):
    cb = CircuitBreaker(sessionmaker)
    status = await cb.check_and_trigger(weekly_pnl_pct=-0.15)

    assert status == CircuitBreakerStatus.PAUSED


@pytest.mark.asyncio
async def test_three_negative_weeks_trigger_halted(sessionmaker):
    cb = CircuitBreaker(sessionmaker)

    s1 = await cb.check_and_trigger(weekly_pnl_pct=-0.05)
    s2 = await cb.check_and_trigger(weekly_pnl_pct=-0.04)
    s3 = await cb.check_and_trigger(weekly_pnl_pct=-0.01)

    assert s1 != CircuitBreakerStatus.HALTED
    assert s2 != CircuitBreakerStatus.HALTED
    assert s3 == CircuitBreakerStatus.HALTED


@pytest.mark.asyncio
async def test_halted_is_idempotent(sessionmaker):
    """Se já HALTED, nova chamada não cria nova linha."""
    cb = CircuitBreaker(sessionmaker)

    await cb.check_and_trigger(weekly_pnl_pct=-0.05)
    await cb.check_and_trigger(weekly_pnl_pct=-0.04)
    await cb.check_and_trigger(weekly_pnl_pct=-0.01)  # HALTED aqui

    n_before = len(await _all_states(sessionmaker))

    # Chamadas adicionais com qualquer P&L — não mudam.
    await cb.check_and_trigger(weekly_pnl_pct=+0.10)
    status = await cb.check_and_trigger(weekly_pnl_pct=-0.20)

    assert status == CircuitBreakerStatus.HALTED
    rows = await _all_states(sessionmaker)
    assert len(rows) == n_before


@pytest.mark.asyncio
async def test_resume_if_due_does_nothing_when_resumes_in_future(sessionmaker):
    future = datetime.now(timezone.utc) + timedelta(days=2)
    await _seed_state(
        sessionmaker, CircuitBreakerStatus.PAUSED, resumes_at=future
    )
    cb = CircuitBreaker(sessionmaker)

    await cb.resume_if_due()
    rows = await _all_states(sessionmaker)
    assert rows[-1].status == CircuitBreakerStatus.PAUSED


@pytest.mark.asyncio
async def test_resume_if_due_transitions_to_normal_when_expired(sessionmaker):
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    await _seed_state(
        sessionmaker, CircuitBreakerStatus.PAUSED, resumes_at=past
    )
    cb = CircuitBreaker(sessionmaker)

    await cb.resume_if_due()
    rows = await _all_states(sessionmaker)
    assert rows[-1].status == CircuitBreakerStatus.NORMAL


@pytest.mark.asyncio
async def test_resume_if_due_noop_on_normal(sessionmaker):
    await _seed_state(sessionmaker, CircuitBreakerStatus.NORMAL)
    cb = CircuitBreaker(sessionmaker)

    before = len(await _all_states(sessionmaker))
    await cb.resume_if_due()
    after = len(await _all_states(sessionmaker))

    assert before == after


@pytest.mark.asyncio
async def test_get_status_returns_current(sessionmaker):
    cb = CircuitBreaker(sessionmaker)

    assert await cb.get_status() == CircuitBreakerStatus.NORMAL

    await _seed_state(sessionmaker, CircuitBreakerStatus.RECOVERY)
    assert await cb.get_status() == CircuitBreakerStatus.RECOVERY


@pytest.mark.asyncio
async def test_get_status_treats_expired_pause_as_normal(sessionmaker):
    """Pausa expirada → status efectivo NORMAL mesmo sem resume_if_due()."""
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    await _seed_state(
        sessionmaker, CircuitBreakerStatus.PAUSED, resumes_at=past
    )
    cb = CircuitBreaker(sessionmaker)

    assert await cb.get_status() == CircuitBreakerStatus.NORMAL


@pytest.mark.asyncio
async def test_positive_week_resets_negative_weeks_counter(sessionmaker):
    cb = CircuitBreaker(sessionmaker)

    await cb.check_and_trigger(weekly_pnl_pct=-0.03)
    await cb.check_and_trigger(weekly_pnl_pct=-0.02)
    # Semana positiva → contador zera.
    await cb.check_and_trigger(weekly_pnl_pct=+0.05)
    # Mais uma negativa → contador deve ser 1, não 3 ⇒ não deve HALT.
    status = await cb.check_and_trigger(weekly_pnl_pct=-0.02)

    assert status != CircuitBreakerStatus.HALTED
