"""End-to-end: pega num OrderFilled real on-chain das wallets seguidas, parsa com
o novo `ChainWatcher._parse_log`, e compara com o que a Data API reporta para o
mesmo `tx_hash`. Confirma side, price e tokenId."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import aiohttp
from sqlalchemy import select

from polymarket_bot.config import get_settings
from polymarket_bot.db.enums import TradeSide
from polymarket_bot.db.models import Wallet
from polymarket_bot.db.session import get_sessionmaker
from polymarket_bot.monitoring.chain_watcher import (
    ORDER_FILLED_TOPIC,
    POLYMARKET_CTF_EXCHANGE,
    POLYMARKET_NEG_RISK_EXCHANGE,
    ChainWatcher,
)


async def main() -> None:
    s = get_settings()
    sm = get_sessionmaker()
    rpc = "https://polygon-bor.publicnode.com"

    async with sm() as db:
        rows = (
            await db.execute(select(Wallet.address).where(Wallet.is_followed.is_(True)))
        ).all()

    targets = {addr.lower() for (addr,) in rows}
    print(f"Wallets seguidas: {len(targets)}")
    print()

    async with aiohttp.ClientSession() as http:
        # 1. Pega o último trade duma wallet via Data API e o respectivo tx_hash.
        first_addr = next(iter(targets))
        async with http.get(
            f"{s.polymarket_data_api_url}/trades",
            params={"user": first_addr, "limit": 1},
        ) as r:
            trades = await r.json()
        trade = trades[0]
        tx = trade["transactionHash"].lower()
        print(f"Trade Data API:")
        print(f"  wallet     = {first_addr}")
        print(f"  tx_hash    = {tx}")
        print(f"  side       = {trade.get('side')}")
        print(f"  outcome    = {trade.get('outcome')}")
        print(f"  price      = {trade.get('price')}")
        print(f"  size       = {trade.get('size')}")
        print(f"  asset      = {trade.get('asset')}")
        print()

        # 2. Pega o receipt e filtra OrderFilled das exchanges V2.
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getTransactionReceipt",
            "params": [tx],
        }
        async with http.post(rpc, json=payload) as r:
            rec = (await r.json())["result"]
        logs = rec.get("logs") or []
        valid_addrs = {
            POLYMARKET_CTF_EXCHANGE.lower(),
            POLYMARKET_NEG_RISK_EXCHANGE.lower(),
        }
        order_logs = [
            log
            for log in logs
            if (log.get("address") or "").lower() in valid_addrs
            and (log.get("topics") or [""])[0] == ORDER_FILLED_TOPIC
        ]
        print(f"OrderFilled logs nas V2 exchanges: {len(order_logs)}")
        print()

        # 3. Parsa cada um com o novo parser e mostra side/price.
        for i, log in enumerate(order_logs):
            parsed = ChainWatcher._parse_log(log)
            if parsed is None:
                print(f"  [{i}] FALHOU a parsar")
                continue

            maker_match = parsed.maker.lower() in targets
            taker_match = parsed.taker.lower() in targets
            in_log = "MAKER" if maker_match else ("TAKER" if taker_match else "—")

            side_str = "BUY" if parsed.side == 0 else "SELL"
            # side da wallet seguida (flip se taker)
            wallet_side: TradeSide | None = None
            if maker_match:
                wallet_side = TradeSide.BUY if parsed.side == 0 else TradeSide.SELL
            elif taker_match:
                wallet_side = TradeSide.SELL if parsed.side == 0 else TradeSide.BUY

            price = ChainWatcher._compute_price(parsed)

            print(f"  [{i}] addr={log['address']}")
            print(f"      maker      = {parsed.maker}")
            print(f"      taker      = {parsed.taker}")
            print(f"      side(maker)= {parsed.side} ({side_str})")
            print(f"      tokenId    = {parsed.token_id}")
            print(f"      maker_amt  = {parsed.maker_amount}")
            print(f"      taker_amt  = {parsed.taker_amount}")
            print(f"      price      = {price}")
            print(f"      our wallet = {in_log}")
            if wallet_side is not None:
                api_side = (trade.get("side") or "").upper()
                match = "OK" if wallet_side.value == api_side else "MISMATCH"
                print(
                    f"      our side   = {wallet_side.value}  vs API={api_side}  [{match}]"
                )
                try:
                    api_price = Decimal(str(trade.get("price", "0")))
                    pm = "OK" if abs(price - api_price) < Decimal("0.001") else "MISMATCH"
                    print(f"      price match: {price}  vs API={api_price}  [{pm}]")
                except Exception:
                    pass
            print()


if __name__ == "__main__":
    asyncio.run(main())
