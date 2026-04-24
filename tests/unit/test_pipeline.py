from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from polymarket_bot.db.enums import TradeSide, WalletTier
from polymarket_bot.execution.pipeline import (
    ExecutionPipeline,
    PipelineContext,
    PipelineOutcome,
    SignalInput,
)
from polymarket_bot.market import WalletSignal
from tests.fixtures.markets import make_snapshot


# ---- Fake OrderManager ---------------------------------------------------

@dataclass
class FakeSubmissionResult:
    success: bool = True
    is_duplicate: bool = False
    is_paper: bool = True
    reason: str | None = None
    clob_order_id: str | None = "paper-1"
    executed_price: Decimal | None = Decimal("0.41")
    bot_trade_id: int | None = 1


@dataclass
class FakeOrderManager:
    """Captura chamadas a submit() e devolve um resultado configurável."""

    result: FakeSubmissionResult = field(default_factory=FakeSubmissionResult)
    calls: list[dict] = field(default_factory=list)

    async def submit(self, **kwargs) -> FakeSubmissionResult:
        self.calls.append(kwargs)
        return self.result


# ---- Helpers -------------------------------------------------------------

def _signal_input(
    addr: str = "0x1",
    tier: WalletTier = WalletTier.TOP,
    outcome: str = "YES",
    win_rate: float = 0.65,
) -> SignalInput:
    return SignalInput(
        signal=WalletSignal(wallet_address=addr, tier=tier, outcome=outcome),
        win_rate=win_rate,
    )


def _ctx(
    signals,
    snapshot=None,
    bankroll=Decimal("1000"),
    recovery_mode: bool = False,
) -> PipelineContext:
    return PipelineContext(
        market=snapshot or make_snapshot(depth_size="5000"),
        signals=signals,
        bankroll_usd=bankroll,
        recovery_mode=recovery_mode,
    )


# ---- Tests ---------------------------------------------------------------


class TestPipelineHappyPath:
    @pytest.mark.asyncio
    async def test_full_execution_with_2_top_wallets(self):
        """Banca $1000, 2 wallets TOP win rate 60% e 70% → EXECUTED."""
        om = FakeOrderManager()
        pipeline = ExecutionPipeline(order_manager=om)
        ctx = _ctx(
            [
                _signal_input("0xA", WalletTier.TOP, "YES", 0.60),
                _signal_input("0xB", WalletTier.TOP, "YES", 0.70),
            ]
        )

        result = await pipeline.evaluate(ctx)

        assert result.outcome == PipelineOutcome.EXECUTED
        assert result.submission is not None
        assert result.submission.success

        # A chamada ao order manager deve ser coerente com as decisões
        assert len(om.calls) == 1
        call = om.calls[0]
        assert call["outcome"] == "YES"
        assert call["side"] == TradeSide.BUY
        # pre_cap = 50 × 1.4 × 1.3 = $91 → excede cap 8% ($80), logo capped
        assert call["size_usd"] == Decimal("80.00")
        assert result.size is not None
        assert result.size.hit_cap
        assert call["followed_wallet"] == "0xA"
        assert call["dedup_hash"] == result.dedup_hash
        assert call["dedup_hash"] is not None


class TestPipelineSkips:
    @pytest.mark.asyncio
    async def test_skipped_consensus_on_empty_signals(self):
        om = FakeOrderManager()
        pipeline = ExecutionPipeline(order_manager=om)

        result = await pipeline.evaluate(_ctx([]))

        assert result.outcome == PipelineOutcome.SKIPPED_CONSENSUS
        assert result.consensus is not None
        assert not result.consensus.should_enter
        assert om.calls == []

    @pytest.mark.asyncio
    async def test_skipped_consensus_on_opposite_directions(self):
        om = FakeOrderManager()
        pipeline = ExecutionPipeline(order_manager=om)
        ctx = _ctx(
            [
                _signal_input("0xA", WalletTier.TOP, "YES", 0.70),
                _signal_input("0xB", WalletTier.TOP, "NO", 0.70),
            ]
        )

        result = await pipeline.evaluate(ctx)

        assert result.outcome == PipelineOutcome.SKIPPED_CONSENSUS
        assert "opostas" in (result.reason or "")
        assert om.calls == []

    @pytest.mark.asyncio
    async def test_skipped_market_filters_low_volume(self):
        om = FakeOrderManager()
        pipeline = ExecutionPipeline(order_manager=om)
        snapshot = make_snapshot(volume_usd="10000", depth_size="5000")  # < $50k
        ctx = _ctx(
            [_signal_input("0xA", WalletTier.TOP, "YES", 0.65)],
            snapshot=snapshot,
        )

        result = await pipeline.evaluate(ctx)

        assert result.outcome == PipelineOutcome.SKIPPED_MARKET_FILTERS
        assert "volume" in (result.reason or "")
        assert result.market_filters is not None
        assert not result.market_filters.passes
        assert om.calls == []

    @pytest.mark.asyncio
    async def test_skipped_ev_when_edge_too_small(self):
        """Wallet solo com win rate 55% @ preço 0.40 → margin < 10%."""
        om = FakeOrderManager()
        pipeline = ExecutionPipeline(order_manager=om)
        ctx = _ctx([_signal_input("0xA", WalletTier.TOP, "YES", 0.55)])

        result = await pipeline.evaluate(ctx)

        assert result.outcome == PipelineOutcome.SKIPPED_EV
        assert result.ev is not None
        assert not result.ev.passes_min_margin
        assert result.probability_estimate is not None
        assert om.calls == []

    @pytest.mark.asyncio
    async def test_skipped_size_low_bankroll_solo_bottom(self):
        """Banca $100 + SOLO BOTTOM + win rate alto → falha no sizing."""
        om = FakeOrderManager()
        pipeline = ExecutionPipeline(order_manager=om)
        ctx = _ctx(
            [_signal_input("0xA", WalletTier.BOTTOM, "YES", 0.80)],
            bankroll=Decimal("100"),
        )

        result = await pipeline.evaluate(ctx)

        assert result.outcome == PipelineOutcome.SKIPPED_SIZE
        assert result.size is not None
        assert result.size.below_minimum
        assert om.calls == []

    @pytest.mark.asyncio
    async def test_skipped_depth_when_book_too_thin(self):
        """Book com apenas 1 share → stake $91 não preenche."""
        om = FakeOrderManager()
        pipeline = ExecutionPipeline(order_manager=om)
        snapshot = make_snapshot(depth_size="1")  # book diminuto
        ctx = _ctx(
            [
                _signal_input("0xA", WalletTier.TOP, "YES", 0.70),
                _signal_input("0xB", WalletTier.TOP, "YES", 0.70),
            ],
            snapshot=snapshot,
        )

        result = await pipeline.evaluate(ctx)

        assert result.outcome == PipelineOutcome.SKIPPED_DEPTH
        assert result.depth is not None
        assert not result.depth.fillable
        assert om.calls == []

    @pytest.mark.asyncio
    async def test_skipped_duplicate_from_order_manager(self):
        """OrderManager devolve is_duplicate=True → SKIPPED_DUPLICATE."""
        om = FakeOrderManager(
            result=FakeSubmissionResult(
                success=False,
                is_duplicate=True,
                reason="dedup hit",
                executed_price=None,
            )
        )
        pipeline = ExecutionPipeline(order_manager=om)
        ctx = _ctx(
            [
                _signal_input("0xA", WalletTier.TOP, "YES", 0.65),
                _signal_input("0xB", WalletTier.TOP, "YES", 0.70),
            ]
        )

        result = await pipeline.evaluate(ctx)

        assert result.outcome == PipelineOutcome.SKIPPED_DUPLICATE
        assert result.submission is not None
        assert result.submission.is_duplicate
        assert result.dedup_hash is not None

    @pytest.mark.asyncio
    async def test_failed_submission_propagates(self):
        om = FakeOrderManager(
            result=FakeSubmissionResult(
                success=False,
                is_duplicate=False,
                reason="CLOB rejected",
                executed_price=None,
            )
        )
        pipeline = ExecutionPipeline(order_manager=om)
        ctx = _ctx(
            [
                _signal_input("0xA", WalletTier.TOP, "YES", 0.65),
                _signal_input("0xB", WalletTier.TOP, "YES", 0.70),
            ]
        )

        result = await pipeline.evaluate(ctx)

        assert result.outcome == PipelineOutcome.FAILED
        assert result.reason == "CLOB rejected"
