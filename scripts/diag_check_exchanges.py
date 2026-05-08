"""Para cada wallet seguida, pega na última trade via Data API e verifica que
contrato Polygon emitiu o OrderFilled. Detecta se há mais que um exchange v2."""
from __future__ import annotations

import asyncio
from collections import Counter

import aiohttp
from sqlalchemy import select

from polymarket_bot.config import get_settings
from polymarket_bot.db.models import Wallet
from polymarket_bot.db.session import get_sessionmaker


async def main() -> None:
    s = get_settings()
    sm = get_sessionmaker()
    rpc = "https://polygon-bor.publicnode.com"

    async with sm() as db:
        rows = (
            await db.execute(
                select(Wallet.address).where(Wallet.is_followed.is_(True))
            )
        ).all()

    addresses_per_wallet: list[tuple[str, list[str]]] = []
    contract_counter: Counter[str] = Counter()

    async with aiohttp.ClientSession() as http:
        for (addr,) in rows:
            params = {"user": addr.lower(), "limit": 5}
            async with http.get(
                f"{s.polymarket_data_api_url}/trades", params=params
            ) as r:
                trades = await r.json()
            if not trades:
                addresses_per_wallet.append((addr, []))
                continue

            tx_hashes = list(
                {(t.get("transactionHash") or "").lower() for t in trades if t.get("transactionHash")}
            )
            seen_contracts: set[str] = set()
            for tx in tx_hashes:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_getTransactionReceipt",
                    "params": [tx],
                }
                async with http.post(rpc, json=payload) as r:
                    data = await r.json()
                rec = data.get("result") or {}
                logs = rec.get("logs") or []
                for log in logs:
                    topics = log.get("topics") or []
                    if len(topics) < 4:
                        continue
                    log_addr = (log.get("address") or "").lower()
                    log_topic0 = topics[0]
                    # any 4-topic event whose address looks like a contract handling this wallet
                    seen_contracts.add(f"{log_addr}::{log_topic0}")

            addresses_per_wallet.append((addr, sorted(seen_contracts)))
            for c in seen_contracts:
                contract_counter[c] += 1

    print("Resumo por wallet (contrato::topic0):")
    for addr, contracts in addresses_per_wallet:
        print(f"\n  {addr}:")
        for c in contracts:
            print(f"    {c}")

    print("\n\nContagem global (contrato::topic0 → wallets que tocaram):")
    for c, n in contract_counter.most_common():
        print(f"  {n}× {c}")


if __name__ == "__main__":
    asyncio.run(main())
