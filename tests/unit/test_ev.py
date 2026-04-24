from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_bot.market import compute_ev, estimate_true_probability


class TestEvMath:
    def test_docx_example_zero_ev(self):
        """§3.4 exemplo: stake $50, preço 0.40, prob 0.40 → EV = $0."""
        result = compute_ev(
            stake_usd=Decimal("50"),
            entry_price=Decimal("0.40"),
            true_probability=Decimal("0.40"),
        )
        assert result.expected_value_usd == pytest.approx(Decimal("0"), abs=Decimal("0.01"))
        assert not result.passes_min_margin

    def test_positive_ev_above_margin(self):
        """prob 0.50 no preço 0.40 → edge claro de 25%."""
        result = compute_ev(
            stake_usd=Decimal("100"),
            entry_price=Decimal("0.40"),
            true_probability=Decimal("0.50"),
        )
        # potential_profit = 100 * (2.5 - 1) = 150
        # EV = 0.5*150 - 0.5*100 = 75 - 50 = 25
        assert result.expected_value_usd == pytest.approx(Decimal("25"))
        assert result.margin == pytest.approx(0.25, rel=0.01)
        assert result.passes_min_margin

    def test_margin_just_above_threshold(self):
        """Margin 11% passa, 10% não passa (estrita)."""
        # Resolver: EV/stake = 0.11 → 0.11 = prob * (1/price - 1) - (1 - prob)
        # Escolhemos prob = 0.60, price = 0.50 → EV/stake = 0.2
        r1 = compute_ev(Decimal("100"), Decimal("0.50"), Decimal("0.60"))
        assert r1.margin == pytest.approx(0.20, rel=0.01)
        assert r1.passes_min_margin

    def test_invalid_price_rejected(self):
        result = compute_ev(Decimal("100"), Decimal("0"), Decimal("0.5"))
        assert not result.passes_min_margin
        assert "entry_price" in (result.reason or "")

    def test_invalid_probability_rejected(self):
        result = compute_ev(Decimal("100"), Decimal("0.5"), Decimal("1.5"))
        assert not result.passes_min_margin
        assert "true_probability" in (result.reason or "")


class TestEstimateTrueProbability:
    def test_zero_wallets_returns_market_price(self):
        est = estimate_true_probability(Decimal("0.40"), [])
        assert est.estimated_probability == Decimal("0.40")
        assert est.n_wallets == 0
        assert est.edge_applied == 0.0

    def test_example_from_chat(self):
        """Wallets com win rate médio 65%, 2 em consenso, preço 0.40.

        edge_base = (0.65 − 0.50) × 0.5 = 0.075
        edge_applied = 0.075 × 1.5 = 0.1125
        prob estimada = 0.40 + 0.1125 = 0.5125
        """
        est = estimate_true_probability(Decimal("0.40"), [0.60, 0.70])
        assert est.avg_win_rate == pytest.approx(0.65)
        assert est.consensus_factor == 1.5
        assert est.edge_applied == pytest.approx(0.1125)
        assert est.estimated_probability == pytest.approx(Decimal("0.5125"))

    def test_wallet_at_50pct_adds_no_edge(self):
        """Win rate exatamente no limite inferior → sem edge."""
        est = estimate_true_probability(Decimal("0.40"), [0.50, 0.50, 0.50])
        assert est.edge_applied == 0.0
        assert est.estimated_probability == Decimal("0.40")

    def test_three_wallets_uses_factor_2(self):
        est = estimate_true_probability(Decimal("0.40"), [0.60, 0.65, 0.70])
        assert est.consensus_factor == 2.0

    def test_elite_wallets_give_strong_edge(self):
        """Top 3 com win rate 75% → edge substancial."""
        est = estimate_true_probability(Decimal("0.40"), [0.75, 0.75, 0.75])
        # edge_base = 0.25 * 0.5 = 0.125; factor=2.0 → 0.25
        assert est.edge_applied == pytest.approx(0.25)
        assert est.estimated_probability == pytest.approx(Decimal("0.65"))

    def test_below_50pct_wallets_floor_at_market_price(self):
        """Win rate < 50% não deve descontar abaixo do preço de mercado."""
        est = estimate_true_probability(Decimal("0.40"), [0.40])
        assert est.estimated_probability == Decimal("0.40")

    def test_caps_at_0_99(self):
        est = estimate_true_probability(Decimal("0.80"), [0.95, 0.95, 0.95])
        assert est.estimated_probability <= Decimal("0.99")
