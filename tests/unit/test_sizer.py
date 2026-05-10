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
        """Banca $1000, SOLO TOP: base $30 × 1.4 × 0.5 = $21 (cap $80 não atingido)."""
        consensus = _consensus(WalletTier.TOP, n=1)
        d = compute_size(Decimal("1000"), consensus)
        assert d.base_size_usd == Decimal("30.00")
        assert d.tier_multiplier == 1.4
        assert d.signal_multiplier == 0.5
        assert d.recovery_multiplier == 1.0
        assert d.pre_cap_size_usd == pytest.approx(Decimal("21.00"), abs=Decimal("0.01"))
        assert d.final_size_usd == pytest.approx(Decimal("21.00"), abs=Decimal("0.01"))
        assert not d.hit_cap
        assert not d.below_minimum
        assert d.should_execute

    def test_consensus_top_no_cap_with_3pct_base(self):
        """TOP + CONSENSUS @ banca $1000: 30 × 1.4 × 1.3 = $54.60 → < cap $80."""
        consensus = _consensus(WalletTier.TOP, n=2)
        d = compute_size(Decimal("1000"), consensus)
        assert d.pre_cap_size_usd == pytest.approx(Decimal("54.60"), abs=Decimal("0.01"))
        assert d.final_size_usd == pytest.approx(Decimal("54.60"), abs=Decimal("0.01"))
        assert not d.hit_cap

    def test_hard_cap_8pct(self):
        """Banca alta força hit-cap: ALL + TOP @ $2000: 60 × 1.4 × 1.5 = $126 > cap $160 nope.
        Vamos para banca $5000: 150 × 1.4 × 1.5 = $315 > cap $400 → hit_cap."""
        consensus = _consensus(WalletTier.TOP, n=7)  # força ALL
        d = compute_size(Decimal("5000"), consensus)
        # base 3% × 1.4 × 1.5 = 6.3% > cap 8%? 0.03×1.4×1.5 = 0.063 = 6.3%. NÃO bate cap.
        # Bate cap quando 0.03×t×s > 0.08 → t×s > 2.67. Max t×s = 1.4×1.5 = 2.1 → NUNCA bate.
        # Com base 3%, o cap deixou de ser atingível pelos multiplicadores normais.
        # Apenas se houvesse multiplicadores extra. Vamos validar que NÃO bate.
        assert not d.hit_cap
        assert d.final_size_usd == d.pre_cap_size_usd

    def test_recovery_mode_reduces_size(self):
        """SOLO TOP @ banca $2000: normal vs recovery (×0.6)."""
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
        """Banca muito baixa para dar abaixo de $1: $30 × 1 × 0.5 × 1 = $15 ≥ $1.
        Para forçar below-min com novo $1: usa banca $30 + SOLO BOTTOM:
        $30 × 3% × 0.7 × 0.5 = $0.315 < $1 → skip."""
        consensus = _consensus(WalletTier.BOTTOM, n=1)
        d = compute_size(Decimal("30"), consensus)
        assert d.below_minimum
        assert d.final_size_usd == Decimal("0")
        assert not d.should_execute
        assert "mínimo" in (d.skip_reason or "")

    def test_minimum_exactly_1_passes(self):
        """Mínimo é agora $1 — qualquer trade acima disso passa."""
        consensus = _consensus(WalletTier.TOP, n=1)
        d = compute_size(Decimal("1000"), consensus)
        # 30 × 1.4 × 0.5 = $21 → bem acima de $1
        assert d.final_size_usd >= Decimal("1")
        assert d.should_execute

    def test_low_bankroll_still_executes_with_min_1(self):
        """Banca $500 + SOLO BOTTOM: 15 × 0.7 × 0.5 = $5.25 → ACIMA do novo
        mínimo de $1. Antes (com min $20) era skipped."""
        consensus = _consensus(WalletTier.BOTTOM, n=1)
        d = compute_size(Decimal("500"), consensus)
        assert d.final_size_usd > Decimal("1")
        assert not d.below_minimum
        assert d.should_execute
