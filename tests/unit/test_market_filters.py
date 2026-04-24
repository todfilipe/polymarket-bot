from __future__ import annotations

from decimal import Decimal

from polymarket_bot.market import (
    check_market_filters,
    check_orderbook_depth,
)
from tests.fixtures.markets import make_orderbook, make_snapshot


class TestMarketFilters:
    def test_ideal_market_passes_and_is_ideal(self):
        snap = make_snapshot(
            volume_usd="150000",
            days_to_resolution=14,
            best_bid="0.39",
            best_ask="0.41",
        )
        result = check_market_filters(snap)
        assert result.passes
        assert result.is_ideal_zone
        assert result.reasons == []

    def test_low_volume_fails(self):
        snap = make_snapshot(volume_usd="10000")
        result = check_market_filters(snap)
        assert not result.passes
        assert any("volume" in r for r in result.reasons)

    def test_too_soon_fails(self):
        # 10h até resolução
        snap = make_snapshot(days_to_resolution=10 / 24)
        result = check_market_filters(snap)
        assert not result.passes
        assert any("tempo" in r and "mín" in r for r in result.reasons)

    def test_too_far_fails(self):
        snap = make_snapshot(days_to_resolution=90)
        result = check_market_filters(snap)
        assert not result.passes
        assert any("máx" in r for r in result.reasons)

    def test_extreme_probability_fails(self):
        # mid ≈ 0.90 → falha (> 82%)
        snap = make_snapshot(best_bid="0.89", best_ask="0.91")
        result = check_market_filters(snap)
        assert not result.passes
        assert any("prob" in r for r in result.reasons)

    def test_low_probability_fails(self):
        snap = make_snapshot(best_bid="0.04", best_ask="0.06")
        result = check_market_filters(snap)
        assert not result.passes
        assert any("prob" in r for r in result.reasons)

    def test_passes_but_not_ideal_zone(self):
        # prob 75% — passa filtros mas fora da zona ideal (20-65%)
        snap = make_snapshot(best_bid="0.74", best_ask="0.76")
        result = check_market_filters(snap)
        assert result.passes
        assert not result.is_ideal_zone


class TestOrderBookDepth:
    def test_small_stake_fits_first_level(self):
        book = make_orderbook(best_bid="0.39", best_ask="0.41", depth_size="1000")
        result = check_orderbook_depth(book, Decimal("50"), side="BUY")
        assert result.fillable
        assert result.price_impact < 0.001

    def test_large_stake_crosses_levels_within_2pct(self):
        # 5 níveis × size 5000 × ~$0.41 ≈ $2000 por nível → capacidade ~$10k
        book = make_orderbook(
            best_bid="0.39", best_ask="0.41", depth_size="5000", levels=5, step="0.001"
        )
        result = check_orderbook_depth(book, Decimal("1500"), side="BUY")
        assert result.fillable
        # step 0.001 entre níveis → impacto médio ≪ 2%
        assert result.price_impact < 0.02

    def test_stake_too_large_fails(self):
        book = make_orderbook(
            best_bid="0.39", best_ask="0.41", depth_size="50", levels=3, step="0.01"
        )
        # capacidade total ≈ 50 × (0.41+0.42+0.43) = ~$63
        result = check_orderbook_depth(book, Decimal("500"), side="BUY")
        assert not result.fillable

    def test_impact_above_threshold_fails(self):
        # Book manual: 1º nível raso, 2º muito mais caro → fill preenche mas com impacto alto
        from polymarket_bot.market.models import OrderBook, OrderBookLevel

        book = OrderBook(
            market_id="m1",
            outcome="YES",
            bids=(OrderBookLevel(price=Decimal("0.39"), size=Decimal("100")),),
            asks=(
                OrderBookLevel(price=Decimal("0.41"), size=Decimal("10")),      # $4.10
                OrderBookLevel(price=Decimal("0.50"), size=Decimal("10000")),  # quase ilimitado
            ),
        )
        # Stake $50: $4.10 no nível 0, $45.90 no nível 0.50 → avg ≈ 0.493 → impacto ≈ 20%
        result = check_orderbook_depth(book, Decimal("50"), side="BUY")
        assert not result.fillable
        assert "impacto" in (result.reason or "")

    def test_empty_book_fails(self):
        from polymarket_bot.market.models import OrderBook

        book = OrderBook(market_id="m1", outcome="YES", bids=(), asks=())
        result = check_orderbook_depth(book, Decimal("100"), side="BUY")
        assert not result.fillable
        assert "vazio" in (result.reason or "")
