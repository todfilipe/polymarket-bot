from __future__ import annotations

import pytest

from polymarket_bot.db.enums import WalletTier
from polymarket_bot.market import (
    ConsensusStrength,
    WalletSignal,
    analyze_consensus,
)


def _sig(addr: str, tier: WalletTier = WalletTier.TOP, outcome: str = "YES") -> WalletSignal:
    return WalletSignal(wallet_address=addr, tier=tier, outcome=outcome)


class TestConsensus:
    def test_empty_signals_aborts(self):
        decision = analyze_consensus([])
        assert decision.strength == ConsensusStrength.ABORT
        assert not decision.should_enter

    def test_solo_wallet_top_tier(self):
        decision = analyze_consensus([_sig("0x1", WalletTier.TOP)])
        assert decision.strength == ConsensusStrength.SOLO
        assert decision.tier_multiplier == 1.4
        assert decision.signal_multiplier == 0.5
        # 1.4 × 0.5 = 0.7
        assert decision.combined_sizing_multiplier == 0.7

    def test_solo_wallet_bottom_tier(self):
        decision = analyze_consensus([_sig("0x1", WalletTier.BOTTOM)])
        assert decision.tier_multiplier == 0.7
        assert decision.signal_multiplier == 0.5
        # 0.7 × 0.5 = 0.35
        assert decision.combined_sizing_multiplier == 0.35

    def test_two_wallets_same_direction_consensus(self):
        decision = analyze_consensus(
            [_sig("0x1", WalletTier.TOP), _sig("0x2", WalletTier.TOP)]
        )
        assert decision.strength == ConsensusStrength.CONSENSUS
        # Matches §4.2 example: 1.4 × 1.3 = 1.82
        assert decision.combined_sizing_multiplier == pytest.approx(1.82)

    def test_opposite_directions_abort(self):
        decision = analyze_consensus(
            [
                _sig("0x1", WalletTier.TOP, outcome="YES"),
                _sig("0x2", WalletTier.TOP, outcome="NO"),
            ]
        )
        assert decision.strength == ConsensusStrength.ABORT
        assert "opostas" in (decision.reason or "")

    def test_all_wallets_same_direction(self):
        signals = [_sig(f"0x{i}", WalletTier.TOP) for i in range(7)]
        decision = analyze_consensus(signals)
        assert decision.strength == ConsensusStrength.ALL
        assert decision.signal_multiplier == 1.5

    def test_mixed_tier_uses_top_multiplier(self):
        # 1 top + 1 bottom em consenso → usa tier TOP (1.4)
        decision = analyze_consensus(
            [_sig("0x1", WalletTier.TOP), _sig("0x2", WalletTier.BOTTOM)]
        )
        assert decision.tier_multiplier == 1.4
        assert decision.strength == ConsensusStrength.CONSENSUS

    def test_all_bottom_uses_bottom_multiplier(self):
        decision = analyze_consensus(
            [_sig("0x1", WalletTier.BOTTOM), _sig("0x2", WalletTier.BOTTOM)]
        )
        assert decision.tier_multiplier == 0.7
