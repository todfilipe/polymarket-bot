"""Testes do MarketBuilder — mocks HTTP via patch em `_get`."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from polymarket_bot.monitoring.market_builder import (
    MarketBuilder,
    MarketBuildError,
)


def _make_gamma_payload(
    market_id: str = "m1",
    clob_token_ids: str | list[str] | None = None,
    end_date: str = "2026-05-10T00:00:00Z",
    volume: str = "150000",
    category: str = "Politics",
) -> dict:
    if clob_token_ids is None:
        clob_token_ids = '["tok-yes-111", "tok-no-222"]'
    return {
        "id": market_id,
        "slug": f"slug-{market_id}",
        "question": f"Will {market_id} happen?",
        "category": category,
        "endDate": end_date,
        "volume": volume,
        "clobTokenIds": clob_token_ids,
    }


def _make_book_payload(
    best_bid: str = "0.40",
    best_ask: str = "0.42",
    depth: str = "200",
) -> dict:
    return {
        "bids": [
            {"price": best_bid, "size": depth},
            {"price": str(Decimal(best_bid) - Decimal("0.01")), "size": depth},
        ],
        "asks": [
            {"price": best_ask, "size": depth},
            {"price": str(Decimal(best_ask) + Decimal("0.01")), "size": depth},
        ],
    }


def _install_get_mock(builder: MarketBuilder, side_effect) -> AsyncMock:
    mock = AsyncMock(side_effect=side_effect)
    builder._get = mock  # type: ignore[assignment]
    return mock


@pytest.mark.asyncio
async def test_build_success_returns_full_snapshot():
    gamma = _make_gamma_payload()
    book = _make_book_payload()

    async def fake_get(url: str, params=None):
        if url.endswith("/markets/m1"):
            return gamma
        if url.endswith("/book"):
            assert params == {"token_id": "tok-yes-111"}
            return book
        raise AssertionError(f"URL inesperada: {url}")

    builder = MarketBuilder(gamma_url="http://g", clob_url="http://c")
    _install_get_mock(builder, fake_get)

    snapshot = await builder.build("m1", "YES")

    assert snapshot.market_id == "m1"
    assert snapshot.outcome == "YES"
    assert snapshot.slug == "slug-m1"
    assert snapshot.question == "Will m1 happen?"
    assert snapshot.category == "Politics"
    assert snapshot.volume_usd == Decimal("150000")
    assert snapshot.orderbook.best_bid == Decimal("0.40")
    assert snapshot.orderbook.best_ask == Decimal("0.42")
    # Ordenação canónica: bids desc, asks asc.
    assert snapshot.orderbook.bids[0].price > snapshot.orderbook.bids[-1].price
    assert snapshot.orderbook.asks[0].price < snapshot.orderbook.asks[-1].price


@pytest.mark.asyncio
async def test_build_selects_no_token_for_no_outcome():
    gamma = _make_gamma_payload()
    book = _make_book_payload(best_bid="0.58", best_ask="0.60")

    captured_params: list[dict] = []

    async def fake_get(url: str, params=None):
        if url.endswith("/markets/m1"):
            return gamma
        if url.endswith("/book"):
            captured_params.append(params or {})
            return book
        raise AssertionError(url)

    builder = MarketBuilder(gamma_url="http://g", clob_url="http://c")
    _install_get_mock(builder, fake_get)

    snapshot = await builder.build("m1", "NO")

    assert snapshot.outcome == "NO"
    assert captured_params == [{"token_id": "tok-no-222"}]


@pytest.mark.asyncio
async def test_cache_hit_does_not_refetch_within_ttl():
    calls: list[str] = []

    async def fake_get(url: str, params=None):
        calls.append(url)
        if "/markets/" in url:
            return _make_gamma_payload()
        return _make_book_payload()

    builder = MarketBuilder(gamma_url="http://g", clob_url="http://c")
    mock = _install_get_mock(builder, fake_get)

    snap_a = await builder.build("m1", "YES")
    assert mock.call_count == 2  # 1 gamma + 1 book

    snap_b = await builder.build("m1", "YES")
    # Ainda 2 chamadas totais — segunda build saiu da cache.
    assert mock.call_count == 2
    assert snap_a is snap_b


@pytest.mark.asyncio
async def test_cache_miss_after_ttl_refetches():
    async def fake_get(url: str, params=None):
        if "/markets/" in url:
            return _make_gamma_payload()
        return _make_book_payload()

    builder = MarketBuilder(gamma_url="http://g", clob_url="http://c")
    mock = _install_get_mock(builder, fake_get)

    await builder.build("m1", "YES")
    assert mock.call_count == 2

    # Forçar expiração: esvaziar cache manualmente simula TTL > 30s.
    builder._cache.clear()

    await builder.build("m1", "YES")
    assert mock.call_count == 4  # novo fetch


@pytest.mark.asyncio
async def test_market_not_found_raises_market_build_error():
    async def fake_get(url: str, params=None):
        if "/markets/" in url:
            raise MarketBuildError("404 em /markets/m1")
        return _make_book_payload()

    builder = MarketBuilder(gamma_url="http://g", clob_url="http://c")
    _install_get_mock(builder, fake_get)

    with pytest.raises(MarketBuildError):
        await builder.build("m1", "YES")


@pytest.mark.asyncio
async def test_empty_orderbook_raises_market_build_error():
    async def fake_get(url: str, params=None):
        if "/markets/" in url:
            return _make_gamma_payload()
        return {"bids": [], "asks": []}

    builder = MarketBuilder(gamma_url="http://g", clob_url="http://c")
    _install_get_mock(builder, fake_get)

    with pytest.raises(MarketBuildError, match="vazio"):
        await builder.build("m1", "YES")


@pytest.mark.asyncio
async def test_clob_token_ids_as_json_string_is_parsed():
    gamma = _make_gamma_payload(clob_token_ids='["yy", "nn"]')

    async def fake_get(url: str, params=None):
        if "/markets/" in url:
            return gamma
        assert params == {"token_id": "yy"}
        return _make_book_payload()

    builder = MarketBuilder(gamma_url="http://g", clob_url="http://c")
    _install_get_mock(builder, fake_get)

    snapshot = await builder.build("m1", "YES")
    assert snapshot.market_id == "m1"


@pytest.mark.asyncio
async def test_invalid_outcome_raises():
    builder = MarketBuilder(gamma_url="http://g", clob_url="http://c")
    with pytest.raises(MarketBuildError, match="outcome"):
        await builder.build("m1", "MAYBE")


