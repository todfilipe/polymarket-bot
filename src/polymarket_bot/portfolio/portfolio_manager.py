"""Cálculos de portfolio a partir do estado persistido.

Fornece a `WalletMonitor` o `bankroll` actual, e ao `Scheduler` os dados do
relatório semanal. Não mantém estado em memória — tudo é query directa à DB.

Regras (CLAUDE.md §2, §11, §12):
- Capital = capital_inicial + Σ(realized_pnl_usd) de posições fechadas.
- Semana = ISO (segunda-feira 00:00 UTC até segunda-feira seguinte).
- `is_recovery_mode` reflecte apenas o status actual da `CircuitBreakerState`.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_bot.db.enums import (
    BotTradeStatus,
    CircuitBreakerStatus,
    PositionStatus,
)
from polymarket_bot.db.models import (
    BotTrade,
    CircuitBreakerState,
    Position,
)
from polymarket_bot.notifications.telegram_notifier import WeeklyReport


class PortfolioManager:
    """API read-only sobre o estado do portfolio."""

    DEFAULT_INITIAL_CAPITAL: Decimal = Decimal("1000")

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        initial_capital_usd: Decimal = DEFAULT_INITIAL_CAPITAL,
    ):
        self._sessionmaker = session_factory
        self._initial_capital = Decimal(initial_capital_usd)

    # ------------------------------------------------------------------ bankroll
    async def get_bankroll(self) -> Decimal:
        """Capital inicial + Σ(realized_pnl_usd) de todas as posições fechadas."""
        async with self._sessionmaker() as session:
            realized = await self._sum_realized_pnl(session, closed_only=True)
        bankroll = self._initial_capital + realized
        if bankroll < Decimal("0"):
            logger.warning(
                "portfolio: bankroll negativo ({}) — a clampar a 0", bankroll
            )
            return Decimal("0")
        return bankroll

    async def get_weekly_pnl(self) -> Decimal:
        """P&L realizado desde segunda-feira 00:00 UTC (ISO week start)."""
        week_start_dt = _this_week_start_utc()
        async with self._sessionmaker() as session:
            return await self._sum_realized_pnl(
                session, closed_only=True, closed_since=week_start_dt
            )

    async def get_weekly_pnl_pct(self) -> float:
        """P&L semanal / bankroll no início da semana.

        Se ainda não há histórico, usa `initial_capital` como denominador para
        que a primeira semana tenha uma percentagem bem-definida.
        """
        week_start_dt = _this_week_start_utc()
        async with self._sessionmaker() as session:
            pnl_week = await self._sum_realized_pnl(
                session, closed_only=True, closed_since=week_start_dt
            )
            # Bankroll à hora zero da semana = initial + realized de tudo
            # fechado ANTES da semana em curso.
            realized_before_week = await self._sum_realized_pnl(
                session, closed_only=True, closed_before=week_start_dt
            )
        base = self._initial_capital + realized_before_week
        if base <= Decimal("0"):
            return 0.0
        return float(pnl_week / base)

    # ------------------------------------------------------------------ status
    async def is_recovery_mode(self) -> bool:
        """True sse o último `CircuitBreakerState` tiver status=RECOVERY."""
        async with self._sessionmaker() as session:
            latest = await self._latest_breaker(session)
        return latest is not None and latest.status == CircuitBreakerStatus.RECOVERY

    async def count_open_positions(self) -> int:
        """Posições com status=OPEN (cap §8 = 10 simultâneas)."""
        async with self._sessionmaker() as session:
            stmt = select(func.count()).select_from(Position).where(
                Position.status == PositionStatus.OPEN
            )
            result = await session.execute(stmt)
            return int(result.scalar_one())

    # ------------------------------------------------------------------ report
    async def get_weekly_report_data(self) -> WeeklyReport:
        """Agrega tudo o que o `TelegramNotifier.weekly_report` precisa."""
        week_start_dt = _this_week_start_utc()
        week_start = week_start_dt.date()

        async with self._sessionmaker() as session:
            pnl_week = await self._sum_realized_pnl(
                session, closed_only=True, closed_since=week_start_dt
            )
            realized_before_week = await self._sum_realized_pnl(
                session, closed_only=True, closed_before=week_start_dt
            )
            trades_won, trades_lost = await self._weekly_win_loss(
                session, since=week_start_dt
            )
            trades_skipped = await self._count_bot_trades_by_status(
                session, status=BotTradeStatus.SKIPPED, since=week_start_dt
            )
            top_wallets = await self._top_wallets_by_weekly_pnl(
                session, since=week_start_dt, limit=3
            )

        base = self._initial_capital + realized_before_week
        pnl_pct = float(pnl_week / base) if base > 0 else 0.0
        capital_current = self._initial_capital + realized_before_week + pnl_week
        if capital_current < Decimal("0"):
            capital_current = Decimal("0")

        return WeeklyReport(
            week_start=week_start,
            pnl_usdc=pnl_week,
            pnl_pct=pnl_pct,
            trades_won=trades_won,
            trades_lost=trades_lost,
            trades_skipped=trades_skipped,
            top_wallets=top_wallets,
            next_wallets=[],   # preenchido pelo scheduler após scoring de domingo
            capital_current=capital_current,
            capital_initial=self._initial_capital,
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    async def _sum_realized_pnl(
        session: AsyncSession,
        *,
        closed_only: bool,
        closed_since: datetime | None = None,
        closed_before: datetime | None = None,
    ) -> Decimal:
        stmt = select(func.coalesce(func.sum(Position.realized_pnl_usd), 0))
        if closed_only:
            stmt = stmt.where(Position.status == PositionStatus.CLOSED)
            stmt = stmt.where(Position.realized_pnl_usd.is_not(None))
        if closed_since is not None:
            stmt = stmt.where(Position.closed_at >= closed_since)
        if closed_before is not None:
            stmt = stmt.where(Position.closed_at < closed_before)
        result = await session.execute(stmt)
        raw = result.scalar_one()
        return Decimal(str(raw)) if raw is not None else Decimal("0")

    @staticmethod
    async def _latest_breaker(
        session: AsyncSession,
    ) -> CircuitBreakerState | None:
        stmt = (
            select(CircuitBreakerState)
            .order_by(CircuitBreakerState.triggered_at.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def _weekly_win_loss(
        session: AsyncSession, since: datetime
    ) -> tuple[int, int]:
        """Contagem de posições fechadas desde `since` — W/L por sinal do P&L."""
        stmt = select(Position.realized_pnl_usd).where(
            Position.status == PositionStatus.CLOSED,
            Position.closed_at >= since,
            Position.realized_pnl_usd.is_not(None),
        )
        pnls = (await session.execute(stmt)).scalars().all()
        wins = sum(1 for p in pnls if p is not None and p > 0)
        losses = sum(1 for p in pnls if p is not None and p <= 0)
        return wins, losses

    @staticmethod
    async def _count_bot_trades_by_status(
        session: AsyncSession,
        *,
        status: BotTradeStatus,
        since: datetime,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(BotTrade)
            .where(BotTrade.status == status, BotTrade.created_at >= since)
        )
        return int((await session.execute(stmt)).scalar_one())

    @staticmethod
    async def _top_wallets_by_weekly_pnl(
        session: AsyncSession, *, since: datetime, limit: int
    ) -> list[str]:
        """Top-N wallets seguidas por Σ(realized_pnl) das trades do bot desde `since`.

        Aproximação razoável enquanto não há link directo Position→followed_wallet:
        usa-se `BotTrade.followed_wallet_address` e soma-se o P&L das posições
        pelo `market_id + outcome` (best-effort — se não bater, a wallet simplesmente
        não aparece no top).
        """
        # Pares (followed_wallet, market_id) das ordens executadas esta semana
        stmt = (
            select(
                BotTrade.followed_wallet_address,
                func.sum(Position.realized_pnl_usd).label("pnl"),
            )
            .join(
                Position,
                (Position.market_id == BotTrade.market_id)
                & (Position.outcome == BotTrade.outcome),
            )
            .where(
                Position.status == PositionStatus.CLOSED,
                Position.closed_at >= since,
                Position.realized_pnl_usd.is_not(None),
            )
            .group_by(BotTrade.followed_wallet_address)
            .order_by(func.sum(Position.realized_pnl_usd).desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [str(r[0]) for r in rows if r[0]]


# ---------------------------------------------------------------- helpers
def _this_week_start_utc(now: datetime | None = None) -> datetime:
    """Segunda-feira 00:00 UTC da semana corrente."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    days_since_monday = now.weekday()
    monday_date: date = (now - timedelta(days=days_since_monday)).date()
    return datetime.combine(monday_date, time.min, tzinfo=timezone.utc)
