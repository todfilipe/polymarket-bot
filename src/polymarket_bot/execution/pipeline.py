"""Pipeline de execução — orquestra a decisão de entrada num trade.

Ordem dos gates (falha em qualquer = skip com motivo registado):

    1. Consenso das wallets (direção, força, tier)
    2. Filtros de mercado (volume, tempo, probabilidade)
    3. Estimativa de true_probability + EV margin check
    4. Sizing (tier × consenso × recuperação, cap 8%, mín $20)
    5. Depth check do orderbook (para o size decidido)
    6. Submit via OrderManager (dry-run ou live)

Cada skip é persistido em `BotTrade` com status=SKIPPED para análise semanal
(§12 — "trades ignoradas com motivos desagregados").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from polymarket_bot.db.enums import TradeSide
from polymarket_bot.execution.dedup import compute_dedup_hash
from polymarket_bot.execution.sizer import SizeDecision, compute_size
from polymarket_bot.market import (
    ConsensusDecision,
    EvResult,
    MarketFilterResult,
    MarketSnapshot,
    OrderBookDepthResult,
    ProbabilityEstimate,
    WalletSignal,
    analyze_consensus,
    check_market_filters,
    check_orderbook_depth,
    compute_ev,
    estimate_true_probability,
)

if TYPE_CHECKING:
    from polymarket_bot.execution.order_manager import OrderManager, SubmissionResult
    from polymarket_bot.portfolio.exposure_guard import ExposureGuard


class PipelineOutcome(StrEnum):
    EXECUTED = "EXECUTED"
    SKIPPED_CONSENSUS = "SKIPPED_CONSENSUS"
    SKIPPED_MARKET_FILTERS = "SKIPPED_MARKET_FILTERS"
    SKIPPED_EV = "SKIPPED_EV"
    SKIPPED_SIZE = "SKIPPED_SIZE"
    SKIPPED_DEPTH = "SKIPPED_DEPTH"
    SKIPPED_EXPOSURE = "SKIPPED_EXPOSURE"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class SignalInput:
    """Sinal de uma wallet + dados que acompanham (fornecidos pelo caller)."""

    signal: WalletSignal
    win_rate: float             # win rate histórico da wallet (do scoring)


@dataclass(frozen=True)
class PipelineContext:
    """Tudo o que o pipeline precisa para decidir."""

    market: MarketSnapshot
    signals: Sequence[SignalInput]
    bankroll_usd: Decimal
    recovery_mode: bool = False


@dataclass(frozen=True)
class PipelineResult:
    outcome: PipelineOutcome
    reason: str | None

    # Artefactos intermédios — preenchidos consoante até onde chegou
    consensus: ConsensusDecision | None = None
    market_filters: MarketFilterResult | None = None
    probability_estimate: ProbabilityEstimate | None = None
    ev: EvResult | None = None
    size: SizeDecision | None = None
    depth: OrderBookDepthResult | None = None
    submission: "SubmissionResult | None" = None
    dedup_hash: str | None = None


class ExecutionPipeline:
    """Orquestrador puro — sem I/O directo (delega no `OrderManager`)."""

    def __init__(
        self,
        order_manager: "OrderManager",
        exposure_guard: "ExposureGuard | None" = None,
    ):
        self._order_manager = order_manager
        self._exposure_guard = exposure_guard

    async def evaluate(self, ctx: PipelineContext) -> PipelineResult:
        log = logger.bind(market_id=ctx.market.market_id)

        # ---- 1. Consenso ----
        wallet_signals = [s.signal for s in ctx.signals]
        consensus = analyze_consensus(wallet_signals)
        if not consensus.should_enter:
            log.info("skip: consensus — {}", consensus.reason)
            return PipelineResult(
                outcome=PipelineOutcome.SKIPPED_CONSENSUS,
                reason=consensus.reason,
                consensus=consensus,
            )

        # ---- 2. Filtros de mercado ----
        mf = check_market_filters(ctx.market)
        if not mf.passes:
            reason = "; ".join(mf.reasons)
            log.info("skip: market filters — {}", reason)
            return PipelineResult(
                outcome=PipelineOutcome.SKIPPED_MARKET_FILTERS,
                reason=reason,
                consensus=consensus,
                market_filters=mf,
            )

        # ---- 3. Probability + EV margin ----
        market_price = ctx.market.implied_probability
        assert market_price is not None  # garantido por check_market_filters

        prob_estimate = estimate_true_probability(
            market_price=market_price,
            win_rates=[s.win_rate for s in ctx.signals],
        )
        # EV margin é size-independent — usamos stake dummy $1 só para margin check
        ev_preview = compute_ev(
            stake_usd=Decimal("1"),
            entry_price=market_price,
            true_probability=prob_estimate.estimated_probability,
        )
        if not ev_preview.passes_min_margin:
            log.info("skip: EV — {}", ev_preview.reason)
            return PipelineResult(
                outcome=PipelineOutcome.SKIPPED_EV,
                reason=ev_preview.reason,
                consensus=consensus,
                market_filters=mf,
                probability_estimate=prob_estimate,
                ev=ev_preview,
            )

        # ---- 4. Sizing ----
        size_decision = compute_size(ctx.bankroll_usd, consensus, ctx.recovery_mode)
        if not size_decision.should_execute:
            log.info("skip: size — {}", size_decision.skip_reason)
            return PipelineResult(
                outcome=PipelineOutcome.SKIPPED_SIZE,
                reason=size_decision.skip_reason,
                consensus=consensus,
                market_filters=mf,
                probability_estimate=prob_estimate,
                ev=ev_preview,
                size=size_decision,
            )

        # EV recomputed com o stake real (valor em USD para logs)
        ev = compute_ev(
            stake_usd=size_decision.final_size_usd,
            entry_price=market_price,
            true_probability=prob_estimate.estimated_probability,
        )

        # ---- 5. Orderbook depth ----
        depth = check_orderbook_depth(
            ctx.market.orderbook,
            size_decision.final_size_usd,
            side="BUY",
        )
        if not depth.fillable:
            log.info("skip: depth — {}", depth.reason)
            return PipelineResult(
                outcome=PipelineOutcome.SKIPPED_DEPTH,
                reason=depth.reason,
                consensus=consensus,
                market_filters=mf,
                probability_estimate=prob_estimate,
                ev=ev,
                size=size_decision,
                depth=depth,
            )

        # ---- 5.5 Exposure guard (limites de portfolio) ----
        if self._exposure_guard is not None:
            exposure = await self._exposure_guard.check(
                market=ctx.market,
                size_usd=size_decision.final_size_usd,
                bankroll_usd=ctx.bankroll_usd,
            )
            if not exposure.passes:
                log.info("skip: exposure — {}", exposure.reason)
                return PipelineResult(
                    outcome=PipelineOutcome.SKIPPED_EXPOSURE,
                    reason=exposure.reason,
                    consensus=consensus,
                    market_filters=mf,
                    probability_estimate=prob_estimate,
                    ev=ev,
                    size=size_decision,
                    depth=depth,
                )

        # ---- 6. Dedup hash + submit ----
        assert consensus.direction_outcome is not None
        followed_addr = ctx.signals[0].signal.wallet_address
        dedup = compute_dedup_hash(
            wallet_address=followed_addr,
            market_id=ctx.market.market_id,
            side=TradeSide.BUY,
            outcome=consensus.direction_outcome,
        )

        submission = await self._order_manager.submit(
            market=ctx.market,
            outcome=consensus.direction_outcome,
            side=TradeSide.BUY,
            size_usd=size_decision.final_size_usd,
            intended_price=depth.avg_fill_price or market_price,
            expected_value=float(ev.margin),
            followed_wallet=followed_addr,
            dedup_hash=dedup,
        )

        if submission.is_duplicate:
            return PipelineResult(
                outcome=PipelineOutcome.SKIPPED_DUPLICATE,
                reason="hash já existente na janela de dedup",
                consensus=consensus,
                market_filters=mf,
                probability_estimate=prob_estimate,
                ev=ev,
                size=size_decision,
                depth=depth,
                submission=submission,
                dedup_hash=dedup,
            )

        if not submission.success:
            return PipelineResult(
                outcome=PipelineOutcome.FAILED,
                reason=submission.reason,
                consensus=consensus,
                market_filters=mf,
                probability_estimate=prob_estimate,
                ev=ev,
                size=size_decision,
                depth=depth,
                submission=submission,
                dedup_hash=dedup,
            )

        return PipelineResult(
            outcome=PipelineOutcome.EXECUTED,
            reason=None,
            consensus=consensus,
            market_filters=mf,
            probability_estimate=prob_estimate,
            ev=ev,
            size=size_decision,
            depth=depth,
            submission=submission,
            dedup_hash=dedup,
        )
