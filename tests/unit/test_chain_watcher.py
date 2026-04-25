"""Testes do ChainWatcher — parsing de OrderFilled, filtragem por wallet e
side/preço/token derivados dos asset IDs.

Foco em lógica determinística (parsing + decisões); o loop WS/HTTP fica
coberto por integration tests separados (não incluídos aqui).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_bot.db.enums import TradeSide, WalletTier
from polymarket_bot.monitoring.chain_watcher import (
    ORDER_FILLED_TOPIC,
    POLYMARKET_CTF_EXCHANGE,
    ChainWatcher,
    _decode_address_topic,
    _decode_uint256_array,
)
from polymarket_bot.monitoring.signal_reader import (
    DetectedSignal,
    FollowedWallet,
    SignalReader,
)


def _wallet(addr: str, tier: WalletTier = WalletTier.TOP) -> FollowedWallet:
    return FollowedWallet(address=addr.lower(), tier=tier, win_rate=0.7)


def _addr_topic(addr: str) -> str:
    """Cria um topic indexado para um endereço Ethereum."""
    s = addr.lower().removeprefix("0x")
    return "0x" + s.rjust(64, "0")


def _uint256(value: int) -> str:
    return hex(value)[2:].rjust(64, "0")


def _make_log(
    *,
    maker: str,
    taker: str,
    maker_asset_id: int,
    taker_asset_id: int,
    maker_amount: int,
    taker_amount: int,
    fee: int = 0,
    tx_hash: str = "0xabc123",
    log_index: int = 0,
    block_number: int = 1000,
    address: str = POLYMARKET_CTF_EXCHANGE,
    topic_override: str | None = None,
) -> dict:
    """Constrói um log RPC bruto compatível com `eth_getLogs` / `eth_subscribe`."""
    data_hex = "0x" + (
        _uint256(maker_asset_id)
        + _uint256(taker_asset_id)
        + _uint256(maker_amount)
        + _uint256(taker_amount)
        + _uint256(fee)
    )
    return {
        "address": address.lower(),
        "topics": [
            topic_override or ORDER_FILLED_TOPIC,
            "0x" + "1" * 64,  # orderHash dummy
            _addr_topic(maker),
            _addr_topic(taker),
        ],
        "data": data_hex,
        "transactionHash": tx_hash,
        "logIndex": hex(log_index),
        "blockNumber": hex(block_number),
    }


class _FakeSession:
    """Stub `aiohttp.ClientSession` — testes só usam o construtor, não a rede."""


async def _noop_resolver(_token_id: str):
    return ("market_x", "YES")


async def _noop_callback(_signal: DetectedSignal) -> None:
    return None


def _make_watcher(
    wallets: list[FollowedWallet],
    *,
    callback=_noop_callback,
    resolver=_noop_resolver,
) -> ChainWatcher:
    return ChainWatcher(
        rpc_url="http://localhost:8545",
        ws_url=None,
        followed_wallets=wallets,
        signal_callback=callback,
        market_resolver=resolver,
        http_session=_FakeSession(),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------- decoders


def test_decode_address_topic_extracts_last_20_bytes():
    padded = "0x000000000000000000000000abCDef0123456789abCDef0123456789abCDef01"
    assert _decode_address_topic(padded) == "0xabcdef0123456789abcdef0123456789abcdef01"


def test_decode_uint256_array_returns_tuple_of_ints():
    data = "0x" + _uint256(0) + _uint256(123) + _uint256(2**40)
    result = _decode_uint256_array(data, count=3)
    assert result == (0, 123, 2**40)


def test_decode_uint256_array_returns_none_when_too_short():
    data = "0x" + _uint256(1)  # só 1 valor
    assert _decode_uint256_array(data, count=3) is None


# --------------------------------------------------------------- parsing


def test_parse_log_extracts_all_fields():
    maker = "0x1111111111111111111111111111111111111111"
    taker = "0x2222222222222222222222222222222222222222"
    raw = _make_log(
        maker=maker, taker=taker,
        maker_asset_id=0, taker_asset_id=999,
        maker_amount=400, taker_amount=1000,
        tx_hash="0xdeadbeef", log_index=3, block_number=42,
    )
    parsed = ChainWatcher._parse_log(raw)
    assert parsed is not None
    assert parsed.maker == maker.lower()
    assert parsed.taker == taker.lower()
    assert parsed.maker_asset_id == 0
    assert parsed.taker_asset_id == 999
    assert parsed.maker_amount == 400
    assert parsed.taker_amount == 1000
    assert parsed.tx_hash == "0xdeadbeef"
    assert parsed.log_index == 3
    assert parsed.block_number == 42


def test_parse_log_returns_none_for_other_event_topic():
    raw = _make_log(
        maker="0xa", taker="0xb",
        maker_asset_id=0, taker_asset_id=1, maker_amount=1, taker_amount=1,
        topic_override="0x" + "0" * 64,
    )
    assert ChainWatcher._parse_log(raw) is None


def test_parse_log_returns_none_when_topics_missing():
    raw = {"topics": [ORDER_FILLED_TOPIC], "data": "0x"}
    assert ChainWatcher._parse_log(raw) is None


# --------------------------------------------------------------- filtering


@pytest.mark.asyncio
async def test_handle_log_skips_when_neither_party_followed():
    captured: list[DetectedSignal] = []

    async def cb(s):
        captured.append(s)

    watcher = _make_watcher(
        [_wallet("0xabcabcabcabcabcabcabcabcabcabcabcabcabca")],
        callback=cb,
    )
    log = _make_log(
        maker="0x1111111111111111111111111111111111111111",
        taker="0x2222222222222222222222222222222222222222",
        maker_asset_id=0, taker_asset_id=12345,
        maker_amount=100, taker_amount=200,
    )
    await watcher._handle_log(log)
    assert captured == []
    # Log não-relevante NÃO é marcado como visto (não polui o dedup set).
    assert watcher._seen_logs == set()


@pytest.mark.asyncio
async def test_handle_log_emits_signal_when_maker_followed():
    captured: list[DetectedSignal] = []

    async def cb(s):
        captured.append(s)

    followed = "0xaaaa000000000000000000000000000000000000"
    other = "0xbbbb000000000000000000000000000000000000"
    watcher = _make_watcher([_wallet(followed)], callback=cb)

    # Maker = followed, makerAsset=0 (USDC) → BUY de tokens
    log = _make_log(
        maker=followed, taker=other,
        maker_asset_id=0, taker_asset_id=999,
        maker_amount=400_000, taker_amount=1_000_000,
        tx_hash="0xfeedbeef",
    )
    await watcher._handle_log(log)
    assert len(captured) == 1
    sig = captured[0]
    assert sig.wallet.address == followed
    assert sig.side == TradeSide.BUY
    assert sig.market_id == "market_x"
    assert sig.outcome == "YES"
    # 400_000 USDC / 1_000_000 shares = 0.4
    assert sig.price == Decimal("400000") / Decimal("1000000")
    assert sig.tx_hash == "0xfeedbeef"


# --------------------------------------------------------------- side derivation


@pytest.mark.asyncio
async def test_maker_followed_with_token_makerasset_is_sell():
    captured: list[DetectedSignal] = []

    async def cb(s):
        captured.append(s)

    followed = "0xaaaa000000000000000000000000000000000000"
    watcher = _make_watcher([_wallet(followed)], callback=cb)

    # Maker = followed, makerAsset=token (não-zero) → maker está a vender
    log = _make_log(
        maker=followed,
        taker="0xbbbb000000000000000000000000000000000000",
        maker_asset_id=42, taker_asset_id=0,
        maker_amount=1_000_000, taker_amount=600_000,
    )
    await watcher._handle_log(log)
    assert captured[0].side == TradeSide.SELL
    # price = USDC / shares = 600_000 / 1_000_000 = 0.6
    assert captured[0].price == Decimal("600000") / Decimal("1000000")


@pytest.mark.asyncio
async def test_taker_followed_with_usdc_takerasset_is_buy():
    captured: list[DetectedSignal] = []

    async def cb(s):
        captured.append(s)

    followed = "0xaaaa000000000000000000000000000000000000"
    watcher = _make_watcher([_wallet(followed)], callback=cb)

    log = _make_log(
        maker="0xbbbb000000000000000000000000000000000000",
        taker=followed,
        maker_asset_id=42, taker_asset_id=0,
        maker_amount=1_000_000, taker_amount=600_000,
    )
    await watcher._handle_log(log)
    # Taker deu USDC (takerAsset=0) → taker está a comprar
    assert captured[0].side == TradeSide.BUY


@pytest.mark.asyncio
async def test_taker_followed_with_token_takerasset_is_sell():
    captured: list[DetectedSignal] = []

    async def cb(s):
        captured.append(s)

    followed = "0xaaaa000000000000000000000000000000000000"
    watcher = _make_watcher([_wallet(followed)], callback=cb)

    log = _make_log(
        maker="0xbbbb000000000000000000000000000000000000",
        taker=followed,
        maker_asset_id=0, taker_asset_id=42,
        maker_amount=400_000, taker_amount=1_000_000,
    )
    await watcher._handle_log(log)
    assert captured[0].side == TradeSide.SELL


# --------------------------------------------------------------- dedup


@pytest.mark.asyncio
async def test_duplicate_log_not_emitted_twice():
    captured: list[DetectedSignal] = []

    async def cb(s):
        captured.append(s)

    followed = "0xaaaa000000000000000000000000000000000000"
    watcher = _make_watcher([_wallet(followed)], callback=cb)

    log = _make_log(
        maker=followed,
        taker="0xbbbb000000000000000000000000000000000000",
        maker_asset_id=0, taker_asset_id=999,
        maker_amount=400, taker_amount=1000,
        tx_hash="0xdup", log_index=0,
    )
    await watcher._handle_log(log)
    await watcher._handle_log(log)
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_signal_dropped_when_resolver_returns_none():
    captured: list[DetectedSignal] = []

    async def cb(s):
        captured.append(s)

    async def fail_resolver(_t):
        return None

    followed = "0xaaaa000000000000000000000000000000000000"
    watcher = _make_watcher(
        [_wallet(followed)], callback=cb, resolver=fail_resolver
    )

    log = _make_log(
        maker=followed,
        taker="0xbbbb000000000000000000000000000000000000",
        maker_asset_id=0, taker_asset_id=999,
        maker_amount=400, taker_amount=1000,
    )
    await watcher._handle_log(log)
    assert captured == []


# --------------------------------------------------------------- update wallets


def test_update_followed_wallets_replaces_set():
    a = _wallet("0xaaaa000000000000000000000000000000000000")
    b = _wallet("0xbbbb000000000000000000000000000000000000")
    watcher = _make_watcher([a])
    assert watcher.followed_addresses == {a.address}

    watcher.update_followed_wallets([b])
    assert watcher.followed_addresses == {b.address}


# --------------------------------------------------------------- integration with SignalReader


class _StubDataClient:
    async def get_wallet_trades(self, *_a, **_kw):
        return []


@pytest.mark.asyncio
async def test_chain_signal_flows_through_signal_reader_buffer():
    """ChainWatcher.callback → SignalReader.inject_signal → poll_once devolve-o."""
    from datetime import datetime, timezone

    wallet = _wallet("0xaaaa000000000000000000000000000000000000")
    reader = SignalReader(
        wallets=[wallet],
        data_client=_StubDataClient(),  # type: ignore[arg-type]
        source="chain",
    )

    sig = DetectedSignal(
        wallet=wallet,
        market_id="m1",
        outcome="YES",
        price=Decimal("0.4"),
        detected_at=datetime.now(timezone.utc),
        tx_hash="0xinjected",
        side=TradeSide.BUY,
    )
    reader.inject_signal(sig)
    buys = await reader.poll_once()
    assert len(buys) == 1
    assert buys[0].tx_hash == "0xinjected"
    # 2ª poll: buffer drenado.
    assert await reader.poll_once() == []


@pytest.mark.asyncio
async def test_chain_mode_does_not_call_data_api():
    """`source="chain"` evita totalmente o polling à Data API."""
    calls: list[str] = []

    class TrackedClient:
        async def get_wallet_trades(self, wallet_address, since=None, limit=500):
            calls.append(wallet_address)
            return []

    wallet = _wallet("0xaaaa000000000000000000000000000000000000")
    reader = SignalReader(
        wallets=[wallet],
        data_client=TrackedClient(),  # type: ignore[arg-type]
        source="chain",
    )
    await reader.poll_once()
    await reader.poll_sells()
    assert calls == []


@pytest.mark.asyncio
async def test_inject_signal_dedupes_by_tx_hash():
    from datetime import datetime, timezone

    wallet = _wallet("0xaaaa000000000000000000000000000000000000")
    reader = SignalReader(
        wallets=[wallet],
        data_client=_StubDataClient(),  # type: ignore[arg-type]
        source="chain",
    )

    sig = DetectedSignal(
        wallet=wallet, market_id="m1", outcome="YES",
        price=Decimal("0.4"), detected_at=datetime.now(timezone.utc),
        tx_hash="0xsame", side=TradeSide.BUY,
    )
    reader.inject_signal(sig)
    reader.inject_signal(sig)  # duplicado
    buys = await reader.poll_once()
    assert len(buys) == 1


@pytest.mark.asyncio
async def test_inject_signal_for_unknown_wallet_is_dropped():
    """Sinais para wallets fora do universo seguido (e.g. após rebalanceamento)
    são silenciosamente descartados — segurança."""
    from datetime import datetime, timezone

    known = _wallet("0xaaaa000000000000000000000000000000000000")
    unknown = _wallet("0xffff000000000000000000000000000000000000")
    reader = SignalReader(
        wallets=[known],
        data_client=_StubDataClient(),  # type: ignore[arg-type]
        source="chain",
    )
    reader.inject_signal(
        DetectedSignal(
            wallet=unknown, market_id="m1", outcome="YES",
            price=Decimal("0.4"), detected_at=datetime.now(timezone.utc),
            tx_hash="0xstranger", side=TradeSide.BUY,
        )
    )
    assert await reader.poll_once() == []
