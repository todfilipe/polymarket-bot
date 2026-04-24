"""Rebalanceamento semanal — jobs APScheduler de domingo (CLAUDE.md §11).

Sequência UTC:

    22:00 dom   → `job_weekly_report`        — envia relatório via Telegram
    22:30 dom   → `job_close_removed_wallets`— marca posições órfãs para review
    23:00 dom   → `job_score_wallets`        — check circuit breaker + scoring
    23:30 dom   → `job_select_top7`          — persiste novas 7 + recarrega monitor
    00:00 seg   → `job_reset_weekly_state`   — nova semana sem carry-over

Cada job é independente: falha de um não cancela os outros. Erros são
notificados via Telegram e relogados, mas NÃO propagam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_bot.api.polymarket_data import PolymarketDataClient
from polymarket_bot.config.constants import CONST
from polymarket_bot.db.enums import (
    CircuitBreakerStatus,
    PositionStatus,
    WalletTier,
)
from polymarket_bot.db.models import (
    Position,
    Wallet,
    WalletScore,
)
from polymarket_bot.monitoring.wallet_monitor import WalletMonitor
from polymarket_bot.notifications.telegram_notifier import TelegramNotifier
from polymarket_bot.portfolio.portfolio_manager import PortfolioManager
from polymarket_bot.risk.circuit_breaker import CircuitBreaker
from polymarket_bot.wallets.discovery import DiscoveryReport, discover_and_score
from polymarket_bot.wallets.scoring import ScoredWallet


# Injetável para simplificar testes — default é `discover_and_score` real.
DiscoveryFn = Callable[[], Awaitable[DiscoveryReport]]


@dataclass(frozen=True)
class _TopSelection:
    top3: list[ScoredWallet]
    bottom4: list[ScoredWallet]

    @property
    def all(self) -> list[ScoredWallet]:
        return [*self.top3, *self.bottom4]

    @property
    def addresses(self) -> set[str]:
        return {s.wallet_address.lower() for s in self.all}


class RebalancingScheduler:
    """Orquestra os jobs de rebalanceamento semanal com `AsyncIOScheduler`."""

    def __init__(
        self,
        monitor: WalletMonitor,
        portfolio: PortfolioManager,
        circuit_breaker: CircuitBreaker,
        notifier: TelegramNotifier,
        data_client: PolymarketDataClient,
        session_factory: async_sessionmaker[AsyncSession],
        discovery_fn: DiscoveryFn | None = None,
    ):
        self._monitor = monitor
        self._portfolio = portfolio
        self._circuit_breaker = circuit_breaker
        self._notifier = notifier
        self._data_client = data_client
        self._sessionmaker = session_factory
        self._discovery_fn = discovery_fn or discover_and_score

        self._scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self._latest_scored: list[ScoredWallet] = []
        self._latest_removed_addresses: list[str] = []
        self._registered = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Regista jobs (se ainda não) e arranca o scheduler em modo não-bloqueante."""
        if not self._registered:
            self._register_jobs()
            self._registered = True
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("scheduler: iniciado (5 jobs, UTC)")

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler: parado")

    # ------------------------------------------------------------------ registration
    def _register_jobs(self) -> None:
        self._scheduler.add_job(
            self._safe_run,
            CronTrigger(day_of_week="sun", hour=22, minute=0, timezone=timezone.utc),
            args=["job_weekly_report", self.job_weekly_report],
            id="job_weekly_report",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._safe_run,
            CronTrigger(day_of_week="sun", hour=22, minute=30, timezone=timezone.utc),
            args=["job_close_removed_wallets", self.job_close_removed_wallets],
            id="job_close_removed_wallets",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._safe_run,
            CronTrigger(day_of_week="sun", hour=23, minute=0, timezone=timezone.utc),
            args=["job_score_wallets", self.job_score_wallets],
            id="job_score_wallets",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._safe_run,
            CronTrigger(day_of_week="sun", hour=23, minute=30, timezone=timezone.utc),
            args=["job_select_top7", self.job_select_top7],
            id="job_select_top7",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._safe_run,
            CronTrigger(day_of_week="mon", hour=0, minute=0, timezone=timezone.utc),
            args=["job_reset_weekly_state", self.job_reset_weekly_state],
            id="job_reset_weekly_state",
            replace_existing=True,
        )

    async def _safe_run(
        self, name: str, coro_fn: Callable[[], Awaitable[None]]
    ) -> None:
        """Wrapper que contém excepções — jobs são independentes uns dos outros."""
        try:
            await coro_fn()
        except Exception as exc:  # noqa: BLE001 — jobs nunca devem partir o scheduler
            logger.opt(exception=True).error("scheduler: job {} falhou — {}", name, exc)
            try:
                await self._notifier.critical_error(
                    error=f"job {name} falhou: {exc}",
                    module="scheduler",
                )
            except Exception as notify_exc:  # noqa: BLE001
                logger.warning(
                    "scheduler: falha a notificar erro de {} — {}", name, notify_exc
                )

    # ------------------------------------------------------------------ jobs
    async def job_weekly_report(self) -> None:
        report = await self._portfolio.get_weekly_report_data()
        await self._notifier.weekly_report(report)
        logger.bind(
            pnl_usdc=str(report.pnl_usdc),
            pnl_pct=report.pnl_pct,
            trades_won=report.trades_won,
            trades_lost=report.trades_lost,
            trades_skipped=report.trades_skipped,
        ).info("scheduler: relatório semanal enviado")

    async def job_close_removed_wallets(self) -> None:
        """Sinaliza posições abertas cujas wallets seguidas saíram do top 7.

        §6 do CLAUDE.md: só seguir exit da wallet se ela tiver >60% de exits
        precisos. Como o scoring ainda vai correr às 23:00, aqui só sinalizamos
        posições cujo `followed_wallets` esteja totalmente fora do conjunto
        actualmente seguido (best-effort com estado em memória).
        """
        async with self._sessionmaker() as session:
            followed_now = await self._load_followed_addresses(session)

            stmt = select(Position).where(Position.status == PositionStatus.OPEN)
            open_positions = (await session.execute(stmt)).scalars().all()

        orphans: list[int] = []
        for pos in open_positions:
            tracked = {w.lower() for w in (pos.followed_wallets or []) if w}
            if not tracked:
                continue
            if tracked.isdisjoint(followed_now):
                orphans.append(pos.id)
                logger.warning(
                    "scheduler: posição {} ficou sem wallets seguidas "
                    "— avaliar fecho manual",
                    pos.id,
                )

        logger.info(
            "scheduler: close_removed_wallets inspeccionou {} posições, {} órfãs",
            len(open_positions),
            len(orphans),
        )

    async def job_score_wallets(self) -> None:
        await self._circuit_breaker.resume_if_due()

        weekly_pnl_pct = await self._portfolio.get_weekly_pnl_pct()
        new_status = await self._circuit_breaker.check_and_trigger(weekly_pnl_pct)
        logger.info(
            "scheduler: circuit breaker check — weekly_pnl={:.2%} → {}",
            weekly_pnl_pct,
            new_status.value,
        )

        report = await self._discovery_fn()
        self._latest_scored = list(report.top_scored)
        logger.info(
            "scheduler: scoring concluído — {} elegíveis / {} candidatas",
            report.eligible,
            report.candidates,
        )

    async def job_select_top7(self) -> None:
        if not self._latest_scored:
            logger.warning(
                "scheduler: sem resultados de scoring — skip selecção de top 7"
            )
            return

        selection = self._pick_top7(self._latest_scored)
        if not selection.all:
            logger.warning("scheduler: scoring devolveu 0 elegíveis — skip")
            return

        added, removed = await self._persist_selection(selection)
        self._latest_removed_addresses = list(removed)

        await self._monitor.reload_followed_wallets()

        logger.info(
            "scheduler: rebalanceamento concluído: {} novas, {} removidas",
            len(added),
            len(removed),
        )

    async def job_reset_weekly_state(self) -> None:
        status = await self._circuit_breaker.get_status()
        if status != CircuitBreakerStatus.NORMAL:
            logger.info(
                "scheduler: nova semana — status {} preservado (sem reset)",
                status.value,
            )
            return
        await self._circuit_breaker.reset_consecutive_losses()
        logger.info("scheduler: nova semana iniciada")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _pick_top7(ranked: list[ScoredWallet]) -> _TopSelection:
        """Top 3 por score → tier=TOP; próximas 4 → tier=BOTTOM."""
        eligible = [s for s in ranked if s.is_selectable]
        top3 = eligible[:3]
        bottom4 = eligible[3:7]
        return _TopSelection(top3=top3, bottom4=bottom4)

    async def _persist_selection(
        self, selection: _TopSelection
    ) -> tuple[list[str], list[str]]:
        """Persiste a selecção. Devolve (added_addresses, removed_addresses)."""
        now = datetime.now(timezone.utc)
        new_addresses = selection.addresses

        async with self._sessionmaker() as session:
            current_followed = await self._load_followed_addresses(session)
            removed = sorted(current_followed - new_addresses)
            added = sorted(new_addresses - current_followed)

            # Remover `is_followed` das que saíram.
            if current_followed:
                stmt = select(Wallet).where(
                    Wallet.address.in_(list(current_followed))
                )
                to_update = (await session.execute(stmt)).scalars().all()
                for w in to_update:
                    if w.address.lower() not in new_addresses:
                        w.is_followed = False
                        w.current_tier = None

            # Upsert das 7 seguidas + WalletScore associado.
            for tier, scored_list in (
                (WalletTier.TOP, selection.top3),
                (WalletTier.BOTTOM, selection.bottom4),
            ):
                for scored in scored_list:
                    address = scored.wallet_address.lower()
                    wallet = await session.get(Wallet, address)
                    if wallet is None:
                        wallet = Wallet(address=address)
                        session.add(wallet)
                    wallet.is_followed = True
                    wallet.current_tier = tier

                    session.add(
                        WalletScore(
                            wallet_address=address,
                            scored_at=now,
                            window_weeks=CONST.SCORING_WINDOW_WEEKS,
                            total_score=scored.total_score,
                            profit_factor=scored.metrics.profit_factor,
                            consistency_pct=scored.metrics.consistency_pct,
                            win_rate=scored.metrics.win_rate,
                            max_drawdown=scored.metrics.max_drawdown,
                            categories_count=len(scored.metrics.categories),
                            recency_score=scored.metrics.recency_score,
                            total_trades=scored.metrics.total_trades,
                            total_volume_usd=scored.metrics.total_volume_usd,
                            luck_concentration=scored.metrics.luck_concentration,
                            eligible=scored.eligibility.is_eligible,
                            exclusion_reasons=list(scored.eligibility.reasons) or None,
                        )
                    )

            await session.commit()

        return added, removed

    @staticmethod
    async def _load_followed_addresses(session: AsyncSession) -> set[str]:
        stmt = select(Wallet.address).where(Wallet.is_followed.is_(True))
        rows = (await session.execute(stmt)).scalars().all()
        return {addr.lower() for addr in rows}
