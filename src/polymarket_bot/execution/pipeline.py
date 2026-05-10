"""Pipeline de execução — orquestra a decisão de entrada num trade.

Ordem dos gates (falha em qualquer = skip com motivo registado):

    0. Filtro de ruído — descarta sinais com tamanho < 20% da mediana da wallet
    1. Sizing (tier × consenso × recuperação, cap CONST.MAX_SIZE_RATIO)
    2. Depth check do orderbook (para o size decidido)
    3. Exposure guard (cap/posição, evento, cash reserve, máx posições)
    4. Dedup (tx_hash) + submit via OrderManager (dry-run ou live)

Filtros de **alpha** (EV mínimo, volume, janela de probabilidade, tempo,
consenso) foram removidos — pure copytrade. Só ficam protecções de
capital físico (sizing cap, exposure, depth) + filtro de ruído baseado no
padrão histórico de cada wallet.

Cada skip é persistido em `BotTrade` com status=SKIPPED para análise semanal.
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
    from polymarket_bot.monitoring.noise_filter import NoiseFilter
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
    SKIPPED_NOISE = "SKIPPED_NOISE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class SignalInput:
    """Sinal de uma wallet + dados que acompanham (fornecidos pelo caller)."""

    signal: WalletSignal
    win_rate: float                       # win rate histórico da wallet (do scoring)
    tx_hash: str = ""                     # tx on-chain originária — alimenta o dedup
    trade_usd: Decimal = Decimal("0")     # tamanho do trade da wallet em USD (filtro de ruído)


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
        noise_filter: "NoiseFilter | None" = None,
    ):
        self._order_manager = order_manager
        self._exposure_guard = exposure_guard
        self._noise_filter = noise_filter

    async def evaluate(self, ctx: PipelineContext) -> PipelineResult:
        """Modo "copytrade puro": só fica entre o sinal e a execução o que é
        protecção de **capital** (sizing cap, exposure, dedup, depth físico)
        + filtro de **ruído** (trades muito abaixo da mediana da wallet).

        Removidos os filtros de **alpha** — EV mínimo, volume mínimo, janela
        de probabilidade, tempo até resolução, regra de consenso.
        Hipótese: as wallets seguidas são lucrativas precisamente pelo conjunto
        das suas trades; filtrar selectivamente cortava alpha sem garantia de
        melhoria. Risco controlado por size cap + exposure + stop loss.

        O filtro de ruído opera **por sinal individual** — se uma wallet faz
        um trade < 20% da mediana dela, esse sinal é descartado. Aplica a
        todas as entradas (1ª e adds) — caso contrário, depois de uma 1ª
        legítima, qualquer trade de $1 da mesma wallet passaria.
        """
        log = logger.bind(market_id=ctx.market.market_id)

        # ---- 0. Filtro de ruído por wallet (early gate) ----
        # Aplicado antes do sizing porque é computacionalmente barato e
        # o objectivo é descartar ruído cedo. Se houver múltiplos sinais
        # para o mesmo (market, outcome) e algum deles for ruído, esse
        # signal é removido — os restantes seguem.
        if self._noise_filter is not None and ctx.signals:
            kept: list[SignalInput] = []
            skipped_reasons: list[str] = []
            for s in ctx.signals:
                # trade_usd=0 significa "tamanho desconhecido" (legacy/test) —
                # deixa passar (não temos como decidir).
                if s.trade_usd <= 0:
                    kept.append(s)
                    continue
                decision = await self._noise_filter.evaluate(
                    wallet_address=s.signal.wallet_address,
                    trade_usd=s.trade_usd,
                )
                if decision.passes:
                    kept.append(s)
                else:
                    skipped_reasons.append(
                        f"{s.signal.wallet_address[:8]}: {decision.reason}"
                    )
            if not kept:
                reason = "todos os sinais classificados como ruído: " + " | ".join(
                    skipped_reasons
                )
                log.info("skip: noise — {}", reason)
                return PipelineResult(
                    outcome=PipelineOutcome.SKIPPED_NOISE,
                    reason=reason,
                )
            if skipped_reasons:
                log.debug(
                    "noise filter dropped {} signals: {}",
                    len(skipped_reasons),
                    skipped_reasons,
                )
            # Substituir signals por kept para os próximos passos
            ctx = PipelineContext(
                market=ctx.market,
                signals=kept,
                bankroll_usd=ctx.bankroll_usd,
                recovery_mode=ctx.recovery_mode,
            )

        # Consenso é executado apenas para extrair multiplicadores de sizing;
        # nunca aborta — sinais já vêm agrupados por (market, outcome) pelo
        # `wallet_monitor`, pelo que conflito YES/NO não pode ocorrer aqui.
        wallet_signals = [s.signal for s in ctx.signals]
        consensus = analyze_consensus(wallet_signals)

        # Probability estimate e EV — só para LOGS/audit, não rejeitam.
        market_price = ctx.market.implied_probability
        prob_estimate = (
            estimate_true_probability(
                market_price=market_price,
                win_rates=[s.win_rate for s in ctx.signals],
            )
            if market_price is not None
            else None
        )

        # ---- 1. Sizing (cap, tier weights, signal multiplier) ----
        size_decision = compute_size(ctx.bankroll_usd, consensus, ctx.recovery_mode)
        if not size_decision.should_execute:
            log.info("skip: size — {}", size_decision.skip_reason)
            return PipelineResult(
                outcome=PipelineOutcome.SKIPPED_SIZE,
                reason=size_decision.skip_reason,
                consensus=consensus,
                probability_estimate=prob_estimate,
                size=size_decision,
            )

        ev = (
            compute_ev(
                stake_usd=size_decision.final_size_usd,
                entry_price=market_price,
                true_probability=prob_estimate.estimated_probability,
            )
            if (market_price is not None and prob_estimate is not None)
            else None
        )

        # ---- 2. Orderbook depth (realidade física: fill com slippage razoável) ----
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
                probability_estimate=prob_estimate,
                ev=ev,
                size=size_decision,
                depth=depth,
            )

        # ---- 3. Exposure guard (cap/posição via CONST.MAX_SIZE_RATIO,
        #         15%/evento, cash 30%, máx CONST.MAX_OPEN_POSITIONS) ----
        if self._exposure_guard is not None:
            # Passa wallet_address para que o guard possa aplicar o cap por
            # posição (backstop final contra pyramiding além do cap).
            exposure = await self._exposure_guard.check(
                market=ctx.market,
                size_usd=size_decision.final_size_usd,
                bankroll_usd=ctx.bankroll_usd,
                followed_wallet=ctx.signals[0].signal.wallet_address
                if ctx.signals else None,
                side=TradeSide.BUY,
            )
            if not exposure.passes:
                log.info("skip: exposure — {}", exposure.reason)
                return PipelineResult(
                    outcome=PipelineOutcome.SKIPPED_EXPOSURE,
                    reason=exposure.reason,
                    consensus=consensus,
                    probability_estimate=prob_estimate,
                    ev=ev,
                    size=size_decision,
                    depth=depth,
                )

        # ---- 4. Dedup hash + submit ----
        # ``consensus.direction_outcome`` é sempre não-None: signals já vêm
        # agrupados por outcome e analyze_consensus só aborta com 0 sinais
        # ou outcomes opostos — nenhum dos quais ocorre por construção aqui.
        outcome_dir = consensus.direction_outcome or ctx.market.outcome
        followed_addr = ctx.signals[0].signal.wallet_address
        # Dedup baseado no tx_hash on-chain do primeiro sinal — garante
        # idempotência por fill sem bloquear momentum-adds da mesma wallet.
        tx_hash = ctx.signals[0].tx_hash or ""
        dedup = compute_dedup_hash(
            wallet_address=followed_addr,
            market_id=ctx.market.market_id,
            side=TradeSide.BUY,
            outcome=outcome_dir,
            tx_hash=tx_hash,
        )

        submission = await self._order_manager.submit(
            market=ctx.market,
            outcome=outcome_dir,
            side=TradeSide.BUY,
            size_usd=size_decision.final_size_usd,
            intended_price=depth.avg_fill_price or market_price or Decimal("0"),
            expected_value=float(ev.margin) if ev is not None else 0.0,
            followed_wallet=followed_addr,
            dedup_hash=dedup,
        )

        if submission.is_duplicate:
            return PipelineResult(
                outcome=PipelineOutcome.SKIPPED_DUPLICATE,
                reason="hash já existente na janela de dedup",
                consensus=consensus,
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
            probability_estimate=prob_estimate,
            ev=ev,
            size=size_decision,
            depth=depth,
            submission=submission,
            dedup_hash=dedup,
        )
