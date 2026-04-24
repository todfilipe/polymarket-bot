"""Testes do SignalReader — fake `PolymarketDataClient`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polymarket_bot.api.polymarket_data import RawTrade
from polymarket_bot.db.enums import TradeSide, WalletTier
from polymarket_bot.monitoring.signal_reader import (
    FollowedWallet,
    SignalReader,
)


class FakeDataClient:
    """Fake que devolve as trades preparadas por wallet, filtrando por `since`."""

    def __init__(self, trades_by_wallet: dict[str, list[RawTrade]] | None = None):
        self._trades = trades_by_wallet or {}
        self.calls: list[tuple[str, datetime | None]] = []

    def set_trades(self, wallet: str, trades: list[RawTrade]) -> None:
        self._trades[wallet.lower()] = trades

    async def get_wallet_trades(
        self,
        wallet_address: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[RawTrade]:
        self.calls.append((wallet_address.lower(), since))
        trades = self._trades.get(wallet_address.lower(), [])
        if since is None:
            return list(trades)
        return [t for t in trades if t.executed_at >= since]


def _make_trade(
    wallet: str,
    tx_hash: str,
    market_id: str = "market_1",
    side: TradeSide = TradeSide.BUY,
    outcome: str = "YES",
    price: str = "0.40",
    minutes_ago: int = 1,
) -> RawTrade:
    executed_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    price_d = Decimal(price)
    return RawTrade(
        wallet_address=wallet.lower(),
        tx_hash=tx_hash,
        market_id=market_id,
        category="Politics",
        side=side,
        outcome=outcome,
        price=price_d,
        size=Decimal("100"),
        usd_value=price_d * Decimal("100"),
        executed_at=executed_at,
        realized_pnl_usd=None,
    )


def _wallet(addr: str = "0xaaa", tier: WalletTier = WalletTier.TOP) -> FollowedWallet:
    return FollowedWallet(address=addr.lower(), tier=tier, win_rate=0.65)


# ---------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_poll_with_no_trades_returns_empty():
    client = FakeDataClient()
    reader = SignalReader(wallets=[_wallet("0xaaa")], data_client=client)  # type: ignore[arg-type]

    signals = await reader.poll_once()

    assert signals == []
    assert len(client.calls) == 1  # chamou a data API uma vez


@pytest.mark.asyncio
async def test_new_buy_trade_emits_detected_signal():
    wallet = _wallet("0xAAA")
    client = FakeDataClient()
    reader = SignalReader(wallets=[wallet], data_client=client)  # type: ignore[arg-type]

    # Cursor inicial = now(); nova trade tem de ser >= esse instante.
    # Usamos minutes_ago negativo — no futuro em relação ao cursor.
    trade = _make_trade("0xaaa", tx_hash="0xtx1", minutes_ago=-1)
    client.set_trades("0xaaa", [trade])

    signals = await reader.poll_once()

    assert len(signals) == 1
    sig = signals[0]
    assert sig.wallet.address == "0xaaa"
    assert sig.wallet.tier == WalletTier.TOP
    assert sig.market_id == "market_1"
    assert sig.outcome == "YES"
    assert sig.price == Decimal("0.40")
    assert sig.tx_hash == "0xtx1"


@pytest.mark.asyncio
async def test_sell_trade_is_ignored():
    client = FakeDataClient()
    reader = SignalReader(wallets=[_wallet("0xaaa")], data_client=client)  # type: ignore[arg-type]

    client.set_trades(
        "0xaaa",
        [_make_trade("0xaaa", "tx-sell", side=TradeSide.SELL, minutes_ago=-1)],
    )

    signals = await reader.poll_once()

    assert signals == []


@pytest.mark.asyncio
async def test_duplicate_tx_hash_is_not_emitted_twice():
    client = FakeDataClient()
    reader = SignalReader(wallets=[_wallet("0xaaa")], data_client=client)  # type: ignore[arg-type]

    trade = _make_trade("0xaaa", "tx-dup", minutes_ago=-1)
    client.set_trades("0xaaa", [trade])

    first = await reader.poll_once()
    assert len(first) == 1

    # Segunda poll: o mock continua a devolver a mesma trade porque `since`
    # avançou para `trade.executed_at` (inclusive). O reader tem de deduplicar.
    second = await reader.poll_once()
    assert second == []


@pytest.mark.asyncio
async def test_cursor_advances_so_second_poll_only_returns_newer():
    client = FakeDataClient()
    reader = SignalReader(wallets=[_wallet("0xaaa")], data_client=client)  # type: ignore[arg-type]

    old = _make_trade("0xaaa", "tx-old", minutes_ago=-1)
    client.set_trades("0xaaa", [old])

    first = await reader.poll_once()
    assert [s.tx_hash for s in first] == ["tx-old"]

    # Nova trade mais recente → única emitida na 2ª poll.
    newer = _make_trade(
        "0xaaa",
        "tx-new",
        market_id="market_2",
        minutes_ago=-10,  # bem mais no futuro
    )
    client.set_trades("0xaaa", [old, newer])

    second = await reader.poll_once()
    tx_hashes = [s.tx_hash for s in second]
    assert "tx-new" in tx_hashes
    assert "tx-old" not in tx_hashes


@pytest.mark.asyncio
async def test_multiple_wallets_emit_signals_for_all():
    client = FakeDataClient()
    reader = SignalReader(
        wallets=[
            _wallet("0xaaa", tier=WalletTier.TOP),
            _wallet("0xbbb", tier=WalletTier.BOTTOM),
        ],
        data_client=client,  # type: ignore[arg-type]
    )

    client.set_trades("0xaaa", [_make_trade("0xaaa", "tx-a", minutes_ago=-1)])
    client.set_trades("0xbbb", [_make_trade("0xbbb", "tx-b", minutes_ago=-1)])

    signals = await reader.poll_once()

    by_wallet = {s.wallet.address: s for s in signals}
    assert set(by_wallet.keys()) == {"0xaaa", "0xbbb"}
    assert by_wallet["0xaaa"].tx_hash == "tx-a"
    assert by_wallet["0xbbb"].tx_hash == "tx-b"
    assert by_wallet["0xaaa"].wallet.tier == WalletTier.TOP
    assert by_wallet["0xbbb"].wallet.tier == WalletTier.BOTTOM


@pytest.mark.asyncio
async def test_replace_wallets_drops_removed_and_keeps_existing_cursors():
    client = FakeDataClient()
    reader = SignalReader(wallets=[_wallet("0xaaa")], data_client=client)  # type: ignore[arg-type]

    cursor_before = reader.get_cursor("0xaaa")
    assert cursor_before is not None

    reader.replace_wallets([_wallet("0xaaa"), _wallet("0xbbb")])
    # Cursor da wallet que já existia foi preservado.
    assert reader.get_cursor("0xaaa") == cursor_before
    # Nova wallet tem cursor inicializado.
    assert reader.get_cursor("0xbbb") is not None

    reader.replace_wallets([_wallet("0xbbb")])
    # Wallet removida → sem cursor.
    assert reader.get_cursor("0xaaa") is None


@pytest.mark.asyncio
async def test_poll_sells_returns_only_sell_trades():
    client = FakeDataClient()
    reader = SignalReader(wallets=[_wallet("0xaaa")], data_client=client)  # type: ignore[arg-type]

    buy = _make_trade("0xaaa", "tx-buy", side=TradeSide.BUY, minutes_ago=-1)
    sell = _make_trade(
        "0xaaa", "tx-sell", side=TradeSide.SELL, minutes_ago=-1
    )
    client.set_trades("0xaaa", [buy, sell])

    sells = await reader.poll_sells()

    assert len(sells) == 1
    assert sells[0].tx_hash == "tx-sell"
    assert sells[0].side == TradeSide.SELL


@pytest.mark.asyncio
async def test_poll_once_and_poll_sells_share_dedup():
    """tx_hash emitido por uma das duas funções não reaparece na outra."""
    client = FakeDataClient()
    reader = SignalReader(wallets=[_wallet("0xaaa")], data_client=client)  # type: ignore[arg-type]

    buy = _make_trade("0xaaa", "tx-b", side=TradeSide.BUY, minutes_ago=-1)
    sell = _make_trade(
        "0xaaa", "tx-s", side=TradeSide.SELL, minutes_ago=-1
    )
    client.set_trades("0xaaa", [buy, sell])

    # 1ª poll: BUYs → emite tx-b, regista no dedup.
    buys_first = await reader.poll_once()
    assert [s.tx_hash for s in buys_first] == ["tx-b"]

    # 2ª poll (sells): tx-b está no dedup. tx-s ainda não. Só tx-s emitido.
    sells = await reader.poll_sells()
    assert [s.tx_hash for s in sells] == ["tx-s"]

    # Reverso: nova poll de BUYs — tx-b já está no dedup, nem mesmo se repetido.
    client.set_trades("0xaaa", [buy, sell])
    buys_second = await reader.poll_once()
    assert buys_second == []

    # E poll_sells também não re-emite tx-s.
    sells_second = await reader.poll_sells()
    assert sells_second == []


@pytest.mark.asyncio
async def test_data_client_failure_is_swallowed_per_wallet():
    class FailingClient(FakeDataClient):
        async def get_wallet_trades(
            self, wallet_address: str, since=None, limit: int = 500
        ):
            if wallet_address == "0xbad":
                raise RuntimeError("network down")
            return await super().get_wallet_trades(wallet_address, since, limit)

    client = FailingClient()
    reader = SignalReader(
        wallets=[_wallet("0xbad"), _wallet("0xok")],
        data_client=client,  # type: ignore[arg-type]
    )
    client.set_trades("0xok", [_make_trade("0xok", "tx-ok", minutes_ago=-1)])

    signals = await reader.poll_once()

    # Wallet má falha silenciosamente; wallet boa ainda emite.
    assert [s.tx_hash for s in signals] == ["tx-ok"]
