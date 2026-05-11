"""Rebalanceamento semanal — jobs APScheduler de domingo (CLAUDE.md §11).

Sequência UTC:

    22:30 dom   → `job_close_removed_wallets`— marca posições órfãs para review
    22:55 dom   → `job_paper_reset`          — paper only: fecha tudo + snapshot
    23:00 dom   → `job_score_wallets`        — check circuit breaker + scoring
    23:30 dom   → `job_select_top7`          — persiste novas 7 + recarrega monitor
    00:00 seg   → `job_reset_weekly_state`   — nova semana sem carry-over
    00:30 seg   → `job_weekly_report`        — relatório Telegram (depois de tudo)

O relatório corre **por último**, garantindo que mostra dados consistentes:
performance da semana terminada + as 7 wallets já seleccionadas para a
semana nova. Antes corria às 22:00 dom e a secção "Próximas 7" aparecia
vazia porque o select_top7 só corria às 23:30.

Cada job é independente: falha de um não cancela os outros. Erros são
notificados via Telegram e relogados, mas NÃO propagam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
    CircuitBreakerState,
    Position,
    Wallet,
    WalletScore,
    WeeklySnapshot,
)
from polymarket_bot.monitoring.market_builder import MarketBuilder
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

    # Capital inicial usado em paper mode — referência para o reset semanal.
    PAPER_INITIAL_CAPITAL_USD: Decimal = Decimal("1000")

    def __init__(
        self,
        monitor: WalletMonitor,
        portfolio: PortfolioManager,
        circuit_breaker: CircuitBreaker,
        notifier: TelegramNotifier,
        data_client: PolymarketDataClient,
        session_factory: async_sessionmaker[AsyncSession],
        discovery_fn: DiscoveryFn | None = None,
        live_mode: bool = False,
        market_builder: MarketBuilder | None = None,
    ):
        self._monitor = monitor
        self._portfolio = portfolio
        self._circuit_breaker = circuit_breaker
        self._notifier = notifier
        self._data_client = data_client
        self._sessionmaker = session_factory
        self._discovery_fn = discovery_fn or discover_and_score
        self._live_mode = live_mode
        self._market_builder = market_builder

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
            logger.info("scheduler: iniciado (6 jobs, UTC)")

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler: parado")

    # ------------------------------------------------------------------ registration
    def _register_jobs(self) -> None:
        # Relatório corre POR ÚLTIMO (segunda 00:30 UTC) — depois de scoring
        # e select_top7 já terem persistido as novas wallets. Antes corria às
        # 22:00 dom, antes da selecção, e mostrava "Próximas 7: (nenhuma)".
        self._scheduler.add_job(
            self._safe_run,
            CronTrigger(day_of_week="mon", hour=0, minute=30, timezone=timezone.utc),
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
        # Reset paper-mode aos domingos às 22:55 UTC — fecha posições abertas
        # mark-to-market, regista snapshot semanal, e reseta circuit breaker.
        # No-op em LIVE_MODE (não toca em ordens reais).
        self._scheduler.add_job(
            self._safe_run,
            CronTrigger(day_of_week="sun", hour=22, minute=55, timezone=timezone.utc),
            args=["job_paper_reset", self.job_paper_reset],
            id="job_paper_reset",
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

    async def job_paper_reset(self) -> None:
        """Paper-mode only: hard reset semanal. Fecha posições abertas e cria
        snapshot.

        Cada cohort de 7 wallets é avaliada **isoladamente**: o capital de
        início é sempre ``PAPER_INITIAL_CAPITAL_USD`` ($1000), independente
        do P&L acumulado das semanas anteriores. Isto permite ver
        claramente "esta cohort rendeu X%" sem influência do histórico
        — útil para testes de estratégia e benchmarking de cohorts.

        Em LIVE mode é no-op — não tocamos em ordens reais.

        Os dados históricos ficam intactos: ``Position`` continua na DB
        com ``status=CLOSED``, e o snapshot é só uma agregação que serve
        para o dashboard saber qual é o "capital inicial" desta semana.
        """
        if self._live_mode:
            logger.info("scheduler: paper_reset — skip (LIVE mode)")
            return

        now = datetime.now(timezone.utc)
        iso_cal = now.isocalendar()
        week_iso = f"{iso_cal.year}-W{iso_cal.week:02d}"
        # Início da semana = segunda 00:00 UTC desta semana ISO.
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        async with self._sessionmaker() as session:
            # Idempotência: se já existe snapshot para esta semana, saltamos.
            existing = (
                await session.execute(
                    select(WeeklySnapshot).where(WeeklySnapshot.week_iso == week_iso)
                )
            ).scalar_one_or_none()
            if existing is not None:
                logger.info(
                    "scheduler: paper_reset — snapshot {} já existe, skip", week_iso
                )
                return

            # HARD RESET: capital de início é sempre $1000, independente
            # do P&L cumulativo das semanas anteriores.
            capital_start = self.PAPER_INITIAL_CAPITAL_USD

            # Posições fechadas DURANTE esta semana (antes do reset)
            pnl_week_stmt = select(Position.realized_pnl_usd).where(
                Position.status == PositionStatus.CLOSED,
                Position.realized_pnl_usd.is_not(None),
                Position.closed_at >= week_start,
            )
            pnl_realized_week = sum(
                (r for r in (await session.execute(pnl_week_stmt)).scalars().all() if r),
                start=Decimal("0"),
            )

            # Posições ainda abertas — fechar mark-to-market
            open_stmt = select(Position).where(
                Position.status == PositionStatus.OPEN
            )
            open_positions = list((await session.execute(open_stmt)).scalars().all())

        # Buscar preços actuais via market_builder (pode falhar — toleramos)
        forced_pnl_total = Decimal("0")
        n_force_closed = 0
        n_wins = 0
        n_losses = 0
        if open_positions and self._market_builder is not None:
            for pos in open_positions:
                try:
                    snap = await self._market_builder.build(
                        pos.market_id, pos.outcome
                    )
                    current_price = snap.implied_probability or pos.avg_entry_price
                except Exception as exc:  # noqa: BLE001 — fallback a entry price
                    logger.warning(
                        "scheduler: paper_reset — falha a obter preço para {} ({}); "
                        "uso avg_entry como fallback — {}",
                        pos.market_id, pos.outcome, exc,
                    )
                    current_price = pos.avg_entry_price

                if pos.avg_entry_price and pos.avg_entry_price > 0:
                    pnl_ratio = (current_price / pos.avg_entry_price) - Decimal("1")
                    pnl_usd = pos.size_usd * pnl_ratio
                else:
                    pnl_usd = Decimal("0")
                forced_pnl_total += pnl_usd
                if pnl_usd > Decimal("0.01"):
                    n_wins += 1
                elif pnl_usd < Decimal("-0.01"):
                    n_losses += 1

                async with self._sessionmaker() as session:
                    fresh = await session.get(Position, pos.id)
                    if fresh is None or fresh.status != PositionStatus.OPEN:
                        continue
                    fresh.status = PositionStatus.CLOSED
                    fresh.closed_at = now
                    fresh.realized_pnl_usd = pnl_usd
                    fresh.notes = "paper_reset"
                    await session.commit()
                n_force_closed += 1

        # Conta wins/losses das posições normalmente fechadas durante a semana.
        # Excluímos por `notes != 'paper_reset'` em vez de `closed_at < now`
        # — mais robusto a positions com timestamps "no futuro" (testes ou
        # casos edge de clock skew).
        async with self._sessionmaker() as session:
            week_closed_stmt = select(Position).where(
                Position.status == PositionStatus.CLOSED,
                Position.closed_at >= week_start,
                (Position.notes != "paper_reset") | Position.notes.is_(None),
                Position.realized_pnl_usd.is_not(None),
            )
            week_closed = list(
                (await session.execute(week_closed_stmt)).scalars().all()
            )
        for p in week_closed:
            pnl = p.realized_pnl_usd or Decimal("0")
            if pnl > Decimal("0.01"):
                n_wins += 1
            elif pnl < Decimal("-0.01"):
                n_losses += 1

        capital_end = capital_start + pnl_realized_week + forced_pnl_total

        # Wallets seguidas durante a semana (snapshot do estado actual)
        async with self._sessionmaker() as session:
            wallets_stmt = select(Wallet.address).where(Wallet.is_followed == True)  # noqa: E712
            wallets_followed = [
                a for a in (await session.execute(wallets_stmt)).scalars().all()
            ]

            # Insert snapshot
            snap_row = WeeklySnapshot(
                week_iso=week_iso,
                is_paper=True,
                started_at=week_start,
                ended_at=now,
                capital_start_usd=capital_start,
                capital_end_usd=capital_end,
                pnl_realized_usd=pnl_realized_week,
                pnl_unrealized_at_close_usd=forced_pnl_total,
                n_positions_opened=len(week_closed) + n_force_closed,
                n_positions_closed=len(week_closed),
                n_wins=n_wins,
                n_losses=n_losses,
                n_force_closed=n_force_closed,
                wallets_followed=wallets_followed,
            )
            session.add(snap_row)

            # Reset circuit breaker para NORMAL — começa semana limpa
            cb = CircuitBreakerState(
                status=CircuitBreakerStatus.NORMAL,
                reason=f"paper_reset {week_iso}",
                consecutive_negative_weeks=0,
                size_reduction_factor=1.0,
            )
            session.add(cb)
            await session.commit()

        logger.info(
            "scheduler: paper_reset {} concluído — capital ${} → ${}, "
            "{} fechadas (forçado {}), {}W/{}L",
            week_iso,
            float(capital_start),
            float(capital_end),
            len(week_closed) + n_force_closed,
            n_force_closed,
            n_wins,
            n_losses,
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
