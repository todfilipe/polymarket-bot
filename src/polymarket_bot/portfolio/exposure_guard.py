"""Gate de exposição aplicado antes do submit (CLAUDE.md §2, §8).

Três verificações sobre o estado actual das posições abertas:

    1. Máx. 10 posições abertas simultâneas.
    2. Cash reserve ≥ 30% da banca após abrir a nova posição.
    3. Máx. 15% da banca por evento (condition_id partilhado YES/NO).

Sem estado em memória — cada `check()` faz queries frescas à DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_bot.db.enums import PositionStatus
from polymarket_bot.db.models import Position
from polymarket_bot.market.models import MarketSnapshot

MAX_OPEN_POSITIONS = 10
CASH_RESERVE_RATIO = Decimal("0.30")
MAX_EVENT_EXPOSURE_RATIO = Decimal("0.15")
EVENT_KEY_LEN = 42


@dataclass(frozen=True)
class ExposureCheckResult:
    passes: bool
    reason: str | None
    open_positions: int
    capital_deployed_pct: float
    event_exposure_pct: float | None


def _event_key(market_id: str) -> str:
    return market_id[:EVENT_KEY_LEN] if len(market_id) >= EVENT_KEY_LEN else market_id


class ExposureGuard:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        initial_capital: Decimal = Decimal("1000"),
    ):
        self._sessionmaker = session_factory
        self._initial_capital = Decimal(initial_capital)

    async def check(
        self,
        market: MarketSnapshot,
        size_usd: Decimal,
        bankroll_usd: Decimal,
    ) -> ExposureCheckResult:
        async with self._sessionmaker() as session:
            open_positions = await self._open_positions(session)

        count = len(open_positions)
        total_deployed = sum(
            (p.size_usd for p in open_positions), start=Decimal("0")
        )
        deployed_pct = (
            float(total_deployed / bankroll_usd) if bankroll_usd > 0 else 0.0
        )

        target_event = _event_key(market.market_id)
        event_exposure = sum(
            (
                p.size_usd
                for p in open_positions
                if _event_key(p.market_id) == target_event
            ),
            start=Decimal("0"),
        )
        event_pct_current = (
            float(event_exposure / bankroll_usd) if bankroll_usd > 0 else None
        )

        if count >= MAX_OPEN_POSITIONS:
            return ExposureCheckResult(
                passes=False,
                reason="máx. 10 posições abertas atingido",
                open_positions=count,
                capital_deployed_pct=deployed_pct,
                event_exposure_pct=event_pct_current,
            )

        capital_livre_after = bankroll_usd - total_deployed - size_usd
        reserve_pct_after = (
            capital_livre_after / bankroll_usd
            if bankroll_usd > 0
            else Decimal("0")
        )
        if reserve_pct_after < CASH_RESERVE_RATIO:
            return ExposureCheckResult(
                passes=False,
                reason=f"cash reserve insuficiente ({float(reserve_pct_after):.1%} < 30%)",
                open_positions=count,
                capital_deployed_pct=deployed_pct,
                event_exposure_pct=event_pct_current,
            )

        event_pct_after = (
            (event_exposure + size_usd) / bankroll_usd
            if bankroll_usd > 0
            else Decimal("0")
        )
        if event_pct_after > MAX_EVENT_EXPOSURE_RATIO:
            return ExposureCheckResult(
                passes=False,
                reason=f"exposição no evento > 15% ({float(event_pct_after):.1%})",
                open_positions=count,
                capital_deployed_pct=deployed_pct,
                event_exposure_pct=float(event_pct_after),
            )

        return ExposureCheckResult(
            passes=True,
            reason=None,
            open_positions=count,
            capital_deployed_pct=deployed_pct,
            event_exposure_pct=float(event_pct_after),
        )

    @staticmethod
    async def _open_positions(session: AsyncSession) -> list[Position]:
        stmt = select(Position).where(Position.status == PositionStatus.OPEN)
        return list((await session.execute(stmt)).scalars().all())
