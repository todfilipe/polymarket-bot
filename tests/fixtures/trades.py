"""Geradores de trades sintéticos para testes do scoring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_bot.api.polymarket_data import RawTrade
from polymarket_bot.db.enums import TradeSide


def make_trade(
    wallet: str = "0xwallet",
    market_id: str = "market_1",
    category: str | None = "Politics",
    side: TradeSide = TradeSide.BUY,
    outcome: str = "YES",
    price: str = "0.50",
    size: str = "100",
    days_ago: int = 0,
    pnl: str | None = None,
) -> RawTrade:
    now = datetime.now(timezone.utc)
    price_d = Decimal(price)
    size_d = Decimal(size)
    return RawTrade(
        wallet_address=wallet,
        tx_hash=f"0xtx{wallet}{market_id}{days_ago}",
        market_id=market_id,
        category=category,
        side=side,
        outcome=outcome,
        price=price_d,
        size=size_d,
        usd_value=price_d * size_d,
        executed_at=now - timedelta(days=days_ago),
        realized_pnl_usd=Decimal(pnl) if pnl is not None else None,
    )


def elite_wallet_trades(wallet: str = "0xelite") -> list[RawTrade]:
    """50+ trades distribuídos por 4 semanas, 65% win rate, PF ~2.5, 3 categorias."""
    trades: list[RawTrade] = []
    # 30 wins de $10 lucro, 20 losses de $6 perda → PF = 300/120 = 2.5
    categories = ["Politics", "Sports", "Crypto"]
    for i in range(30):
        trades.append(
            make_trade(
                wallet=wallet,
                market_id=f"m_w_{i}",
                category=categories[i % 3],
                days_ago=(i % 28),
                price="0.40",
                size="25",
                pnl="10.00",
            )
        )
    for i in range(20):
        trades.append(
            make_trade(
                wallet=wallet,
                market_id=f"m_l_{i}",
                category=categories[i % 3],
                days_ago=(i % 28),
                price="0.55",
                size="25",
                pnl="-6.00",
            )
        )
    return trades


def lucky_wallet_trades(wallet: str = "0xlucky") -> list[RawTrade]:
    """50 trades, 1 único trade gigante concentra 90% do lucro → excluída."""
    trades: list[RawTrade] = []
    for i in range(50):
        pnl = "900.00" if i == 0 else "1.00" if i % 2 == 0 else "-0.50"
        trades.append(
            make_trade(
                wallet=wallet,
                market_id=f"m_{i}",
                category="Politics" if i % 2 == 0 else "Sports",
                days_ago=(i % 28),
                pnl=pnl,
            )
        )
    return trades


def low_volume_wallet_trades(wallet: str = "0xlow") -> list[RawTrade]:
    """10 trades → falha MIN_TRADES_HISTORY."""
    return [
        make_trade(wallet=wallet, market_id=f"m_{i}", days_ago=i, pnl="5.00")
        for i in range(10)
    ]


def one_category_wallet_trades(wallet: str = "0xmono") -> list[RawTrade]:
    """50 trades todos na mesma categoria → falha MIN_CATEGORIES."""
    return [
        make_trade(
            wallet=wallet,
            market_id=f"m_{i}",
            category="Politics",
            days_ago=(i % 28),
            pnl="10.00" if i % 3 != 0 else "-4.00",
        )
        for i in range(50)
    ]
