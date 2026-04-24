from __future__ import annotations

import pytest

from polymarket_bot.wallets import score_wallet
from polymarket_bot.wallets.scoring import rank_wallets
from tests.fixtures.trades import (
    elite_wallet_trades,
    low_volume_wallet_trades,
    lucky_wallet_trades,
    one_category_wallet_trades,
)


class TestEligibility:
    def test_elite_wallet_passes_all_filters(self):
        scored = score_wallet("0xelite", elite_wallet_trades())
        assert scored.eligibility.is_eligible, scored.eligibility.reasons
        assert scored.total_score > 0

    def test_lucky_wallet_excluded_by_concentration(self):
        scored = score_wallet("0xlucky", lucky_wallet_trades())
        assert not scored.eligibility.is_eligible
        assert any("sorte" in r for r in scored.eligibility.reasons)
        assert scored.total_score == 0.0

    def test_low_volume_excluded(self):
        scored = score_wallet("0xlow", low_volume_wallet_trades())
        assert not scored.eligibility.is_eligible
        assert any("trades insuficientes" in r for r in scored.eligibility.reasons)

    def test_one_category_excluded(self):
        scored = score_wallet("0xmono", one_category_wallet_trades())
        assert not scored.eligibility.is_eligible
        assert any("categorias" in r for r in scored.eligibility.reasons)


class TestScoringComponents:
    def test_elite_metrics_sane(self):
        scored = score_wallet("0xelite", elite_wallet_trades())
        m = scored.metrics
        assert m.total_trades == 50
        assert m.profit_factor == pytest.approx(2.5, rel=0.01)
        assert m.win_rate == pytest.approx(0.60, rel=0.01)
        assert len(m.categories) == 3

    def test_score_range_is_0_to_100(self):
        scored = score_wallet("0xelite", elite_wallet_trades())
        assert 0 <= scored.total_score <= 100

    def test_weights_sum_to_one(self):
        from polymarket_bot.config.constants import CONST

        total = (
            CONST.SCORE_WEIGHT_PROFIT_FACTOR
            + CONST.SCORE_WEIGHT_CONSISTENCY
            + CONST.SCORE_WEIGHT_WIN_RATE
            + CONST.SCORE_WEIGHT_DRAWDOWN
            + CONST.SCORE_WEIGHT_DIVERSIFICATION
            + CONST.SCORE_WEIGHT_RECENCY
        )
        assert total == pytest.approx(1.0)


class TestRanking:
    def test_rank_excludes_ineligible(self):
        scored = [
            score_wallet("0xelite", elite_wallet_trades()),
            score_wallet("0xlucky", lucky_wallet_trades()),
            score_wallet("0xlow", low_volume_wallet_trades()),
        ]
        ranked = rank_wallets(scored)
        assert len(ranked) == 1
        assert ranked[0].wallet_address == "0xelite"

    def test_rank_orders_by_score_desc(self):
        scored_a = score_wallet("0xelite", elite_wallet_trades())
        scored_b = score_wallet("0xelite2", elite_wallet_trades())
        ranked = rank_wallets([scored_a, scored_b])
        scores = [s.total_score for s in ranked]
        assert scores == sorted(scores, reverse=True)


class TestNormalization:
    def test_profit_factor_below_min_is_zero(self):
        from polymarket_bot.wallets.scoring import _norm_profit_factor

        assert _norm_profit_factor(1.0) == 0.0
        assert _norm_profit_factor(1.5) == 0.0

    def test_profit_factor_saturates(self):
        from polymarket_bot.wallets.scoring import _norm_profit_factor

        assert _norm_profit_factor(10.0) == 1.0

    def test_drawdown_penalty_is_monotonic(self):
        from polymarket_bot.wallets.scoring import _norm_drawdown

        assert _norm_drawdown(0.0) == 1.0
        assert _norm_drawdown(0.35) == 0.0
        assert _norm_drawdown(0.10) > _norm_drawdown(0.30)
