"""Verifica se o RPC público está a limitar `eth_getLogs` (resultados truncados)."""
from __future__ import annotations

import asyncio

import aiohttp

from polymarket_bot.monitoring.chain_watcher import (
    ORDER_FILLED_TOPIC,
    POLYMARKET_CTF_EXCHANGE,
    POLYMARKET_NEG_RISK_EXCHANGE,
)


TX = "0x24f3a829d70e2e4336491f486c33aa45ef8b0b3eed899f9b09451861ea948b42"
RPC = "https://polygon-bor.publicnode.com"
TARGET = "0x033f0346c007323030eb420305ffede19a95618e"
EXCHANGES = [POLYMARKET_CTF_EXCHANGE.lower(), POLYMARKET_NEG_RISK_EXCHANGE.lower()]


async def get_logs(http, from_b: int, to_b: int):
    params = [
        {
            "fromBlock": hex(from_b),
            "toBlock": hex(to_b),
            "address": EXCHANGES,
            "topics": [ORDER_FILLED_TOPIC],
        }
    ]
    async with http.post(
        RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": params}
    ) as r:
        return await r.json()


async def main():
    async with aiohttp.ClientSession() as http:
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
        print(f"Tx alvo em bloco: {bn}")

        # 1. eth_getLogs APENAS no bloco da tx
        res = await get_logs(http, bn, bn)
        logs = res.get("result", [])
        print(f"\n1. Bloco único {bn}: {len(logs)} logs (keys={list(res.keys())})")
        if "error" in res:
            print(f"   ERROR: {res['error']}")
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) >= 4:
                maker = "0x" + topics[2][-40:].lower()
                if maker == TARGET:
                    tx_h = log.get("transactionHash", "")
                    print(f"   MATCH como maker em {tx_h[:14]}...")

        # 2. Chunk de 500 blocos centrado
        res2 = await get_logs(http, bn - 250, bn + 249)
        logs2 = res2.get("result", [])
        print(f"\n2. Chunk 500 blocos ({bn-250} → {bn+249}): {len(logs2)} logs")
        if "error" in res2:
            print(f"   ERROR: {res2['error']}")
        # Conta blocos únicos retornados
        unique_blocks = sorted({int(log.get("blockNumber", "0x0"), 16) for log in logs2})
        if unique_blocks:
            print(
                f"   blocos cobertos: min={unique_blocks[0]} max={unique_blocks[-1]}"
                f" ({len(unique_blocks)} blocos com logs)"
            )
        # Vê se há logs no nosso bloco
        in_target_block = [
            log for log in logs2
            if int(log.get("blockNumber", "0x0"), 16) == bn
        ]
        print(f"   logs no bloco alvo {bn}: {len(in_target_block)}")
        # Match maker
        matches = []
        for log in logs2:
            topics = log.get("topics", [])
            if len(topics) >= 4:
                maker = "0x" + topics[2][-40:].lower()
                if maker == TARGET:
                    matches.append(log)
        print(f"   matches da nossa wallet como maker no chunk: {len(matches)}")
        for m in matches:
            mb = int(m["blockNumber"], 16)
            print(f"     - bloco={mb} tx={m['transactionHash'][:14]}...")

        # 3. Chunk de 100 blocos
        res3 = await get_logs(http, bn - 50, bn + 49)
        logs3 = res3.get("result", [])
        print(f"\n3. Chunk 100 blocos: {len(logs3)} logs")
        if "error" in res3:
            print(f"   ERROR: {res3['error']}")


if __name__ == "__main__":
    asyncio.run(main())
