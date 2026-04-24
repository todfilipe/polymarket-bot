"""Loop principal de monitorização — liga SignalReader → MarketBuilder → Pipeline.

Cada iteração:
    1. Verifica `CircuitBreakerState` — se HALTED/PAUSED, não faz trades.
    2. `reader.poll_once()` para obter novos sinais BUY.
    3. Agrupa por `(market_id, outcome)` — várias wallets podem disparar o
       mesmo mercado na mesma poll (consenso natural).
    4. Para cada grupo: `MarketBuilder.build()` → `PipelineContext` → pipeline.
    5. Regista `PipelineResult` em logs estruturados.

Robustez:
- Falha a construir um mercado específico → skip com log, loop continua.
- Erro de rede num mercado → mesmo tratamento.
- Exceção inesperada no ciclo → log CRITICAL + re-raise para o process manager
  reiniciar (§10.1).

A reconfiguração semanal chama `reload_followed_wallets()` após o rebalanceamento
de domingo (§11).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_bot.api.polymarket_data import PolymarketDataClient
from polymarket_bot.config.constants import CONST
from polymarket_bot.db.enums import CircuitBreakerStatus, WalletTier
from polymarket_bot.db.models import (
    CircuitBreakerState,
    Position,
    Wallet,
    WalletScore,
)
from polymarket_bot.execution.pipeline import (
    ExecutionPipeline,
    PipelineContext,
    SignalInput,
)
from polymarket_bot.market import WalletSignal
from polymarket_bot.monitoring.exit_manager import ExitManager
from polymarket_bot.monitoring.market_builder import MarketBuilder, MarketBuildError
from polymarket_bot.monitoring.signal_reader import (
    DetectedSignal,
    FollowedWallet,
    SignalReader,
)


@dataclass(frozen=True)
class _RuntimeState:
    bankroll_usd: Decimal
    recovery_mode: bool
    is_halted: bool
    is_paused: bool


BankrollProvider = Callable[[async_sessionmaker[AsyncSession]], Awaitable[Decimal]]


class WalletMonitor:
    """Orquestrador long-running do copytrade."""

    # Capital inicial — §2 do CLAUDE.md. Usado como baseline de bankroll quando
    # não há provider custom.
    DEFAULT_INITIAL_BANKROLL_USD: Decimal = Decimal("1000")

    def __init__(
        self,
        pipeline: ExecutionPipeline,
        data_client: PolymarketDataClient,
        db_session_factory: async_sessionmaker[AsyncSession],
        poll_interval_seconds: int = 30,
        market_builder: MarketBuilder | None = None,
        signal_reader: SignalReader | None = None,
        bankroll_provider: BankrollProvider | None = None,
        exit_manager: ExitManager | None = None,
    ):
        self._pipeline = pipeline
        self._data_client = data_client
        self._sessionmaker = db_session_factory
        self._poll_interval = poll_interval_seconds
        self._market_builder = market_builder or MarketBuilder()
        self._reader = signal_reader or SignalReader(
            wallets=[], data_client=data_client
        )
        self._bankroll_provider = bankroll_provider or self._default_bankroll
        self._exit_manager = exit_manager
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------ public
    async def run_forever(self) -> None:
        """Loop principal. Termina quando `stop()` é chamado."""
        if not self._reader.followed_addresses:
            await self.reload_followed_wallets()

        logger.info(
            "wallet_monitor: iniciado (poll={}s, wallets={})",
            self._poll_interval,
            len(self._reader.followed_addresses),
        )

        while not self._stop_event.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.critical(
                    "wallet_monitor: exceção não tratada no tick — a re-raise",
                    exc_info=True,
                )
                raise

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                continue

        logger.info("wallet_monitor: parado")

    def stop(self) -> None:
        self._stop_event.set()

    async def reload_followed_wallets(self) -> None:
        """Carrega as top-7 wallets seguidas a partir da DB (scoring mais recente)."""
        wallets = await self._load_followed_wallets()
        self._reader.replace_wallets(wallets)
        logger.info(
            "wallet_monitor: {} wallets seguidas recarregadas", len(wallets)
        )

    # ------------------------------------------------------------------ tick
    async def _tick(self) -> None:
        state = await self._read_runtime_state()
        if state.is_halted:
            logger.warning("wallet_monitor: HALTED — a saltar ciclo")
            return
        if state.is_paused:
            logger.info("wallet_monitor: PAUSED — a saltar ciclo")
            return

        # Gestão de saídas corre antes do poll de novos sinais: liberta
        # capital para possíveis entradas no mesmo tick.
        if self._exit_manager is not None:
            try:
                exit_results = await self._exit_manager.check_all_positions()
            except Exception as exc:  # noqa: BLE001 — exits isolados do entry loop
                logger.warning(
                    "wallet_monitor: exit_manager falhou — {}", exc
                )
            else:
                for r in exit_results:
                    if not r.skipped and r.exit_reason is not None:
                        logger.info(
                            "exit: {} | {} | pnl={:.2f}",
                            r.exit_reason.value,
                            r.market_id,
                            float(r.pnl_usd),
                        )

        signals = await self._reader.poll_once()
        if not signals:
            return

        groups = self._group_by_market(signals)
        for (market_id, outcome), group in groups.items():
            await self._process_group(
                market_id=market_id,
                outcome=outcome,
                group=group,
                state=state,
            )

    async def _process_group(
        self,
        *,
        market_id: str,
        outcome: str,
        group: list[DetectedSignal],
        state: _RuntimeState,
    ) -> None:
        log = logger.bind(market_id=market_id, outcome=outcome, n_signals=len(group))
        try:
            market = await self._market_builder.build(market_id, outcome)
        except MarketBuildError as exc:
            log.warning("wallet_monitor: skip market — {}", exc)
            return
        except Exception as exc:  # noqa: BLE001 — loop continues on transient errors
            log.warning(
                "wallet_monitor: falha inesperada a construir mercado — {}", exc
            )
            return

        signal_inputs = [
            SignalInput(
                signal=WalletSignal(
                    wallet_address=s.wallet.address,
                    tier=s.wallet.tier,
                    outcome=s.outcome,
                ),
                win_rate=s.wallet.win_rate,
            )
            for s in group
        ]
        ctx = PipelineContext(
            market=market,
            signals=signal_inputs,
            bankroll_usd=state.bankroll_usd,
            recovery_mode=state.recovery_mode,
        )

        try:
            result = await self._pipeline.evaluate(ctx)
        except Exception as exc:  # noqa: BLE001
            log.error("wallet_monitor: pipeline levantou — {}", exc)
            return

        log.info(
            "pipeline: outcome={} reason={}",
            result.outcome.value,
            result.reason or "-",
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _group_by_market(
        signals: list[DetectedSignal],
    ) -> dict[tuple[str, str], list[DetectedSignal]]:
        groups: dict[tuple[str, str], list[DetectedSignal]] = {}
        for s in signals:
            groups.setdefault((s.market_id, s.outcome), []).append(s)
        return groups

    async def _read_runtime_state(self) -> _RuntimeState:
        bankroll = await self._bankroll_provider(self._sessionmaker)
        async with self._sessionmaker() as session:
            status = await self._current_breaker_status(session)
        return _RuntimeState(
            bankroll_usd=bankroll,
            recovery_mode=status == CircuitBreakerStatus.RECOVERY,
            is_halted=status == CircuitBreakerStatus.HALTED,
            is_paused=status == CircuitBreakerStatus.PAUSED,
        )

    @staticmethod
    async def _current_breaker_status(
        session: AsyncSession,
    ) -> CircuitBreakerStatus:
        stmt = (
            select(CircuitBreakerState)
            .order_by(CircuitBreakerState.triggered_at.desc())
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return CircuitBreakerStatus.NORMAL
        resumes_at = row.resumes_at
        if resumes_at is not None and resumes_at <= datetime.now(timezone.utc):
            # Janela de pausa expirou → trata como NORMAL até novo evento.
            return CircuitBreakerStatus.NORMAL
        return row.status

    async def _load_followed_wallets(self) -> list[FollowedWallet]:
        """Top-7 wallets seguidas — último `WalletScore` por wallet."""
        async with self._sessionmaker() as session:
            stmt = select(Wallet).where(Wallet.is_followed.is_(True))
            wallets = (await session.execute(stmt)).scalars().all()

            followed: list[FollowedWallet] = []
            for w in wallets:
                tier = w.current_tier or WalletTier.BOTTOM
                score_stmt = (
                    select(WalletScore)
                    .where(WalletScore.wallet_address == w.address)
                    .order_by(WalletScore.scored_at.desc())
                    .limit(1)
                )
                latest = (await session.execute(score_stmt)).scalar_one_or_none()
                win_rate = latest.win_rate if latest is not None else 0.5
                followed.append(
                    FollowedWallet(
                        address=w.address,
                        tier=tier,
                        win_rate=win_rate,
                    )
                )
            return followed

    @classmethod
    async def _default_bankroll(
        cls, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> Decimal:
        """Estimativa: capital inicial + P&L realizado de posições fechadas.

        Não subtrai capital alocado em posições abertas — o sizing do pipeline
        aplica cap de 8%/trade e o `portfolio` (futuro) é que cruza com
        posições abertas. Melhor que over-engineer aqui.
        """
        async with sessionmaker() as session:
            stmt = select(Position.realized_pnl_usd).where(
                Position.realized_pnl_usd.is_not(None)
            )
            pnls = (await session.execute(stmt)).scalars().all()
        realized = sum((p for p in pnls if p is not None), Decimal("0"))
        bankroll = cls.DEFAULT_INITIAL_BANKROLL_USD + realized
        # Stop-loss semanal inegociável — nunca deixa descer abaixo de um patamar
        # absurdo; mas o controlo real está no circuit breaker, aqui só
        # garantimos não-negativo.
        if bankroll < Decimal("0"):
            return Decimal("0")
        return bankroll

    # ----- pequenas conveniências que facilitam testes unitários -----
    @property
    def poll_interval_seconds(self) -> int:
        return self._poll_interval

    @property
    def reader(self) -> SignalReader:
        return self._reader

    @property
    def market_builder(self) -> MarketBuilder:
        return self._market_builder


_ = CONST  # noqa — re-exposto para import-time checks
