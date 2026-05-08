"""Verifica que para uma wallet conhecida, eth_getLogs no chunk certo retorna
um OrderFilled em que ela é maker."""
from __future__ import annotations

import asyncio

import aiohttp

from polymarket_bot.monitoring.chain_watcher import (
    ORDER_FILLED_TOPIC,
    POLYMARKET_CTF_EXCHANGE,
    POLYMARKET_NEG_RISK_EXCHANGE,
)


WALLET = "0x033f0346c007323030eb420305ffede19a95618e"
TX = "0x24f3a829d70e2e4336491f486c33aa45ef8b0b3eed899f9b09451861ea948b42"
RPC = "https://polygon-bor.publicnode.com"


async def main():
    async with aiohttp.ClientSession() as http:
        # bloco da tx
        async with http.post(
            RPC,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "eth_getTransactionReceipt",
                "params": [TX],
            },
        ) as r:
            rec = (await r.json())["result"]
        bn = int(rec["blockNumber"], 16)
        print(f"Bloco da tx: {bn}")

        # 100 blocos centrados (igual ao chunk size do chain_watcher agora)
        from_b, to_b = bn - 50, bn + 49
        params = [
            {
                "fromBlock": hex(from_b), "toBlock": hex(to_b),
                "address": [
                    POLYMARKET_CTF_EXCHANGE.lower(),
                    POLYMARKET_NEG_RISK_EXCHANGE.lower(),
                ],
                "topics": [ORDER_FILLED_TOPIC],
            }
        ]
        print(f"\nA pedir eth_getLogs {from_b} → {to_b} (100 blocos)...")
        async with http.post(
            RPC,
            json={"jsonrpc": "2.0", "id": 2, "method": "eth_getLogs", "params": params},
        ) as r:
            res = await r.json()

        logs = res.get("result", [])
        print(f"  total logs: {len(logs)}")
        if "error" in res:
            print(f"  ERROR: {res['error']}")
            return

        # encontrou 0x033f0346 como maker?
        matches = []
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) >= 4:
                maker = "0x" + topics[2][-40:].lower()
                if maker == WALLET.lower():
                    matches.append(log)

        print(f"  logs com 0x033f0346 como maker: {len(matches)}")
        for m in matches[:5]:
            mb = int(m["blockNumber"], 16)
            tx_h = m.get("transactionHash", "")
            print(f"    bloco={mb}  tx={tx_h[:16]}...")


if __name__ == "__main__":
    asyncio.run(main())
