"""Dump completo dos logs de uma tx real para descobrir o event signature."""
from __future__ import annotations

import asyncio

import aiohttp

from polymarket_bot.config import get_settings


async def main() -> None:
    s = get_settings()
    rpc = "https://polygon-bor.publicnode.com"
    async with aiohttp.ClientSession() as http:
        params = {"user": "0xe8dd7741ccb12350957ec71e9ee332e0d1e6ec86", "limit": 1}
        async with http.get(f"{s.polymarket_data_api_url}/trades", params=params) as r:
            trade = (await r.json())[0]

        tx = trade["transactionHash"]
        print(f"tx: {tx}")
        print(f"proxyWallet: {trade.get('proxyWallet')}")
        print(f"side: {trade.get('side')}  outcome: {trade.get('outcome')}")
        print(f"price: {trade.get('price')}  size: {trade.get('size')}")
        print()

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getTransactionReceipt",
            "params": [tx],
        }
        async with http.post(rpc, json=payload) as r:
            rec = (await r.json())["result"]

        logs = rec.get("logs", [])
        print(f"Total logs: {len(logs)}")
        for i, log in enumerate(logs):
            addr = (log.get("address") or "").lower()
            topics = log.get("topics") or []
            data = log.get("data") or "0x"
            print(f"  [{i}] addr={addr} topics={len(topics)} data_len={len(data)}")
            for j, t in enumerate(topics):
                print(f"      topic[{j}] = {t}")
            if data and data != "0x":
                snippet = data[:200] + ("..." if len(data) > 200 else "")
                print(f"      data = {snippet}")


if __name__ == "__main__":
    asyncio.run(main())
