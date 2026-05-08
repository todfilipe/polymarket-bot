"""Integração completa: corre o `ChainWatcher` real (com `_scan_range` sobre
blocos recentes) e cruza com a Data API. Verifica que cada trade que a Data API
reporta para uma wallet seguida produz um sinal correspondente do ChainWatcher.

Saída esperada:
    [OK] Chain detectou X sinais; Y trades na Data API estão na janela; Z bateram.
    Se Z == Y → 100% de cobertura on-chain.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiohttp
from sqlalchemy import select

from polymarket_bot.config import get_settings
from polymarket_bot.db.models import Wallet
from polymarket_bot.db.session import get_sessionmaker
from polymarket_bot.monitoring.chain_watcher import (
    ChainWatcher,
    MarketIdResolver,
)
from polymarket_bot.monitoring.signal_reader import (
    DetectedSignal,
    FollowedWallet,
)


# Janela de blocos a varrer. Polygon faz ~2s/block → 1800 blocos ≈ 1 hora.
SCAN_BLOCKS = 1800
RPC_URL = "https://polygon-bor.publicnode.com"


async def main() -> None:
    settings = get_settings()
    sm = get_sessionmaker()

    # 1. Carrega as 7 wallets seguidas.
    async with sm() as db:
        rows = (
            await db.execute(
                select(Wallet.address, Wallet.current_tier).where(
                    Wallet.is_followed.is_(True)
                )
            )
        ).all()
    followed = [
        FollowedWallet(address=r[0].lower(), tier=r[1], win_rate=0.7)
        for r in rows
    ]
    targets = {w.address for w in followed}
    print(f"Wallets seguidas: {len(followed)}")
    for w in followed:
        print(f"  - {w.address}  [{w.tier.value}]")
    print()

    captured: list[DetectedSignal] = []

    async def on_signal(sig: DetectedSignal) -> None:
        captured.append(sig)

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"Accept": "application/json"}
    ) as http:
        resolver = MarketIdResolver(
            gamma_url=settings.polymarket_gamma_api_url, http_session=http
        )
        watcher = ChainWatcher(
            rpc_url=RPC_URL,
            ws_url=None,
            followed_wallets=followed,
            signal_callback=on_signal,
            market_resolver=resolver.resolve,
            http_session=http,
        )

        # 2. Determina janela de blocos.
        latest = await watcher._eth_block_number()
        from_block = max(0, latest - SCAN_BLOCKS)
        print(f"Scan: blocos {from_block} → {latest} ({SCAN_BLOCKS} blocos ≈ {SCAN_BLOCKS * 2 // 60} min)")
        print()

        # 3. Mapa do tempo: pega o timestamp do bloco from_block para cruzar com Data API.
        block_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getBlockByNumber",
            "params": [hex(from_block), False],
        }
        async with http.post(RPC_URL, json=block_payload) as r:
            block = (await r.json())["result"]
        from_ts = int(block["timestamp"], 16)
        from_dt = datetime.fromtimestamp(from_ts, tz=timezone.utc)
        # Margem POSITIVA: só consideramos trades da API >= 30s depois do início
        # do scan, para evitar contar como missed trades cujo bloco está antes
        # do scan range mas timestamp está perto do limite.
        api_since = from_dt + timedelta(seconds=30)
        print(f"Janela temporal: desde {api_since.isoformat()} UTC (scan from_dt={from_dt.isoformat()})")
        print()

        # 4. Corre o scan real através do ChainWatcher.
        print("A varrer blocos via _scan_range...")
        await watcher._scan_range(from_block, latest)
        print(f"  → {len(captured)} sinais detectados pelo ChainWatcher")
        print()

        # 5. Cruza com Data API: pega trades de cada wallet na mesma janela.
        api_trades_by_wallet: dict[str, list[dict]] = {}
        total_api_trades = 0
        for w in followed:
            params = {"user": w.address, "limit": 100}
            async with http.get(
                f"{settings.polymarket_data_api_url}/trades", params=params
            ) as r:
                trades = await r.json()
            recent = [
                t for t in (trades or [])
                if t.get("timestamp")
                and int(t["timestamp"]) >= int(api_since.timestamp())
            ]
            api_trades_by_wallet[w.address] = recent
            total_api_trades += len(recent)

        print(
            f"Data API: {total_api_trades} trade(s) das wallets seguidas "
            f"na janela"
        )

        # Index dos sinais por (wallet, tx_hash) para cruzamento.
        sig_keys = {(s.wallet.address.lower(), s.tx_hash.lower()) for s in captured}

        # Cruza: para cada trade da API, tem sinal correspondente?
        matched = 0
        misses: list[tuple[str, dict]] = []
        for w_addr, trades in api_trades_by_wallet.items():
            for t in trades:
                tx = (t.get("transactionHash") or "").lower()
                if not tx:
                    continue
                key = (w_addr, tx)
                if key in sig_keys:
                    matched += 1
                else:
                    misses.append((w_addr, t))

        print(f"  → cruzados com sinais on-chain: {matched}/{total_api_trades}")
        print()

        if misses:
            print("Misses (trades em /trades sem sinal correspondente):")
            for w_addr, t in misses[:10]:
                ts = datetime.fromtimestamp(int(t["timestamp"]), tz=timezone.utc)
                print(
                    f"  - wallet={w_addr[:10]}... side={t.get('side')}"
                    f" price={t.get('price')} ts={ts.isoformat()}"
                    f" tx={t.get('transactionHash','')[:12]}..."
                )
            if len(misses) > 10:
                print(f"  ... e mais {len(misses)-10}")
            print()

        # 6. Mostra os sinais detectados.
        print(f"Sinais ChainWatcher ({len(captured)}):")
        for sig in captured[:15]:
            print(
                f"  - wallet={sig.wallet.address[:10]}... side={sig.side.value}"
                f" outcome={sig.outcome} price={sig.price:.4f}"
                f" market={sig.market_id[:12]}..."
                f" tx={sig.tx_hash[:12]}..."
            )
        if len(captured) > 15:
            print(f"  ... e mais {len(captured)-15}")
        print()

        # 7. Verdict.
        print("=" * 64)
        if total_api_trades == 0:
            print("AVISO: nenhuma trade na janela — janela curta ou wallets inactivas.")
            print(f"       Considera aumentar SCAN_BLOCKS (actualmente {SCAN_BLOCKS}).")
        elif matched == total_api_trades:
            print(f"[OK] 100% de cobertura: todos os {matched} trades detectados.")
        else:
            pct = matched / total_api_trades * 100
            print(
                f"[PARCIAL] {matched}/{total_api_trades} trades detectados "
                f"({pct:.0f}%). {len(misses)} misses — investigar."
            )
        print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
