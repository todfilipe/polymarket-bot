"""Diagnóstico: compara endereços em DB vs proxyWallet em /trades vs OrderFilled on-chain.

Para cada wallet seguida:
  1. Imprime endereço completo da DB.
  2. Pede /trades à Data API e mostra `proxyWallet` da última trade.
  3. Confirma se bate com o endereço da DB.
  4. Pega no `transactionHash` mais recente e tenta encontrar o `OrderFilled`
     correspondente via eth_getLogs no Polygon RPC, mostrando os tópicos
     (maker/taker decoded) — para validar que o que está em DB é mesmo o
     endereço que aparece on-chain.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from sqlalchemy import select

from polymarket_bot.config import get_settings
from polymarket_bot.db.models import Wallet
from polymarket_bot.db.session import get_sessionmaker
from polymarket_bot.monitoring.chain_watcher import (
    ORDER_FILLED_TOPIC,
    POLYMARKET_CTF_EXCHANGE,
    POLYMARKET_NEG_RISK_EXCHANGE,
)


def short(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr else "<empty>"


def decode_topic_addr(topic: str) -> str:
    s = topic.lower().removeprefix("0x")
    return "0x" + s[-40:]


async def fetch_recent_trade(
    session: aiohttp.ClientSession, base_url: str, address: str
) -> dict[str, Any] | None:
    url = f"{base_url}/trades"
    params = {"user": address.lower(), "limit": 5}
    async with session.get(url, params=params) as resp:
        if resp.status >= 400:
            print(f"    /trades HTTP {resp.status}")
            return None
        data = await resp.json()
    return data[0] if data else None


async def fetch_logs_for_tx(
    session: aiohttp.ClientSession, rpc_url: str, tx_hash: str
) -> list[dict[str, Any]]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
    }
    async with session.post(rpc_url, json=payload) as resp:
        resp.raise_for_status()
        data = await resp.json()
    receipt = data.get("result") or {}
    logs = receipt.get("logs") or []
    return logs


async def main() -> None:
    settings = get_settings()
    sm = get_sessionmaker()

    async with sm() as s:
        rows = (
            await s.execute(
                select(Wallet.address, Wallet.current_tier).where(
                    Wallet.is_followed.is_(True)
                )
            )
        ).all()

    if not rows:
        print("DB vazia: corre force_rebalance.py primeiro.")
        return

    print(f"Wallets seguidas em DB: {len(rows)}")
    for addr, tier in rows:
        print(f"  - {addr}  [{tier.value if tier else '?'}]")
    print()

    exchanges = {
        POLYMARKET_CTF_EXCHANGE.lower(): "CTF",
        POLYMARKET_NEG_RISK_EXCHANGE.lower(): "NegRisk",
    }

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"Accept": "application/json"}
    ) as http:
        for db_addr, _ in rows:
            print(f"=== {db_addr} ===")
            trade = await fetch_recent_trade(
                http, settings.polymarket_data_api_url, db_addr
            )
            if not trade:
                print("  Sem trades recentes na Data API.")
                print()
                continue

            proxy = (trade.get("proxyWallet") or "").lower()
            tx = (trade.get("transactionHash") or "").lower()
            ts = trade.get("timestamp")
            side = trade.get("side")
            print(f"  /trades último: side={side} ts={ts}")
            print(f"    proxyWallet  = {proxy}")
            print(f"    DB address   = {db_addr.lower()}")
            print(
                f"    MATCH        = {proxy == db_addr.lower()}"
                f"  (tx={short(tx)})"
            )

            if not tx:
                print()
                continue

            try:
                logs = await fetch_logs_for_tx(http, settings.polygon_rpc_url, tx)
            except Exception as exc:  # noqa: BLE001
                print(f"  RPC falhou: {exc}")
                print()
                continue

            order_logs = [
                log
                for log in logs
                if (log.get("address") or "").lower() in exchanges
                and (log.get("topics") or [None])[0] == ORDER_FILLED_TOPIC
            ]
            print(f"  OrderFilled logs no recibo: {len(order_logs)}")
            for log in order_logs:
                exch_name = exchanges.get(
                    (log.get("address") or "").lower(), "?"
                )
                topics = log.get("topics") or []
                if len(topics) >= 4:
                    maker = decode_topic_addr(topics[2])
                    taker = decode_topic_addr(topics[3])
                    in_log = (
                        db_addr.lower() == maker
                        or db_addr.lower() == taker
                    )
                    print(
                        f"    [{exch_name}] maker={maker} taker={taker} "
                        f"db_in_log={in_log}"
                    )
                else:
                    print(f"    [{exch_name}] log mal-formado (topics={len(topics)})")
            print()


if __name__ == "__main__":
    asyncio.run(main())
