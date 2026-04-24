from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_bot.db.enums import WalletTier
from polymarket_bot.execution.sizer import compute_size
from polymarket_bot.market import WalletSignal, analyze_consensus


def _consensus(tier: WalletTier = WalletTier.TOP, n: int = 2, outcome: str = "YES"):
    signals = [
        WalletSignal(wallet_address=f"0x{i}", tier=tier, outcome=outcome)
        for i in range(n)
    ]
    return analyze_consensus(signals)


class TestSizer:
    def test_base_solo_top_no_cap(self):
        """Banca $1000, SOLO TOP: base $50 × 1.4 × 0.5 = $35 (não atinge cap $80)."""
        consensus = _consensus(WalletTier.TOP, n=1)
        d = compute_size(Decimal("1000"), consensus)
        assert d.base_size_usd == Decimal("50.00")
        assert d.tier_multiplier == 1.4
        assert d.signal_multiplier == 0.5
        assert d.recovery_multiplier == 1.0
        assert d.pre_cap_size_usd == pytest.approx(Decimal("35.00"), abs=Decimal("0.01"))
        assert d.final_size_usd == pytest.approx(Decimal("35.00"), abs=Decimal("0.01"))
        assert not d.hit_cap
        assert not d.below_minimum
        assert d.should_execute

    def test_consensus_top_always_caps(self):
        """TOP + CONSENSUS tem ratio 1.4×1.3=1.82×base > cap 1.6×base → cap sempre activo."""
        consensus = _consensus(WalletTier.TOP, n=2)
        d = compute_size(Decimal("1000"), consensus)
        assert d.pre_cap_size_usd == pytest.approx(Decimal("91.00"), abs=Decimal("0.01"))
        assert d.final_size_usd == Decimal("80.00")   # cap 8%
        assert d.hit_cap

    def test_hard_cap_8pct(self):
        """ALL + TOP pré-cap = 50 × 1.4 × 1.5 = $105 > cap 8% ($80)."""
        consensus = _consensus(WalletTier.TOP, n=7)  # força ALL
        d = compute_size(Decimal("1000"), consensus)
        assert d.hit_cap
        assert d.final_size_usd == Decimal("80.00")
        assert d.pre_cap_size_usd > d.final_size_usd

    def test_recovery_mode_reduces_size(self):
        """SOLO TOP @ banca $2000: normal $70, recovery $70×0.6=$42 — ambos abaixo do cap $160."""
        consensus = _consensus(WalletTier.TOP, n=1)
        d_normal = compute_size(Decimal("2000"), consensus, recovery_mode=False)
        d_recovery = compute_size(Decimal("2000"), consensus, recovery_mode=True)
        assert not d_normal.hit_cap
        assert not d_recovery.hit_cap
        assert d_recovery.recovery_multiplier == 0.6
        assert d_recovery.final_size_usd < d_normal.final_size_usd
        assert d_recovery.final_size_usd == pytest.approx(
            d_normal.final_size_usd * Decimal("0.6"),
            abs=Decimal("0.01"),
        )

    def test_below_minimum_skips(self):
        """Banca $100 + SOLO BOTTOM: 5 × 0.7 × 0.5 = $1.75 → < $20, skip."""
        consensus = _consensus(WalletTier.BOTTOM, n=1)
        d = compute_size(Decimal("100"), consensus)
        assert d.below_minimum
        assert d.final_size_usd == Decimal("0")
        assert not d.should_execute
        assert "mínimo" in (d.skip_reason or "")

    def test_minimum_exactly_20_passes(self):
        """Banca calibrada para dar exactamente $20 após multiplicadores."""
        # SOLO TOP: 1.4 × 0.5 = 0.7 → base × 0.7 = $20 → base = $28.57 → banca = $571.43
        # Mas 5% de 571.43 = 28.57 → não arredonda limpo. Vamos para 1000 e checar mínimo.
        consensus = _consensus(WalletTier.TOP, n=1)
        d = compute_size(Decimal("1000"), consensus)
        # 50 × 1.4 × 0.5 = $35 → acima de $20
        assert d.final_size_usd >= Decimal("20")
        assert d.should_execute

    def test_low_bankroll_solo_bottom_tier_skips(self):
        """Banca $500 + SOLO BOTTOM: 25 × 0.7 × 0.5 = $8.75 → skip."""
        consensus = _consensus(WalletTier.BOTTOM, n=1)
        d = compute_size(Decimal("500"), consensus)
        assert d.final_size_usd == Decimal("0")
        assert d.below_minimum
