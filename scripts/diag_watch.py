"""Watch-mode: corre o ChainWatcher real durante N minutos e imprime cada
sinal detectado em tempo real. Não toca em Telegram, não envia ordens, não
mexe na DB. Útil para confirmar que o bot apanha trades das wallets seguidas
ao vivo, antes de fazer deploy.

Uso:
    python scripts/diag_watch.py [--minutes N]
"""
from __future__ import annotations

import argparse
import asyncio
import signal as sigmod
from datetime import datetime, timezone

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


RPC = "https://polygon-bor.publicnode.com"


def _short(s: str, n: int = 10) -> str:
    return s[:n] + "..." if len(s) > n else s


async def main(minutes: int) -> None:
    settings = get_settings()
    sm = get_sessionmaker()

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
    if not followed:
        print("DB vazia: corre `python scripts/force_rebalance.py` primeiro.")
        return

    print(f"A vigiar {len(followed)} wallets durante {minutes} min:")
    for w in followed:
        print(f"  - {w.address}  [{w.tier.value}]")
    print()
    print(f"RPC: {RPC}")
    print(f"Início: {datetime.now(timezone.utc).isoformat()}  (CTRL+C para parar)")
    print("-" * 80)

    n_signals = 0

    async def on_signal(sig: DetectedSignal) -> None:
        nonlocal n_signals
        n_signals += 1
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(
            f"[{ts}] #{n_signals} {sig.wallet.address[:10]}... "
            f"{sig.side.value:4} {sig.outcome:<20} @ {float(sig.price):.4f} "
            f"market={_short(sig.market_id, 14)} tx={_short(sig.tx_hash, 12)}",
            flush=True,
        )

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"Accept": "application/json"}
    ) as http:
        resolver = MarketIdResolver(
            gamma_url=settings.polymarket_gamma_api_url,
            http_session=http,
        )
        watcher = ChainWatcher(
            rpc_url=RPC,
            ws_url=None,
            followed_wallets=followed,
            signal_callback=on_signal,
            market_resolver=resolver.resolve,
            http_session=http,
        )

        # SIGINT → para com grace
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(sigmod.SIGINT, watcher.stop)
        except NotImplementedError:
            pass  # Windows: KeyboardInterrupt apanhado por asyncio.run

        run_task = asyncio.create_task(watcher.run_forever())
        timeout_task = asyncio.create_task(asyncio.sleep(minutes * 60))

        try:
            done, _ = await asyncio.wait(
                {run_task, timeout_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                watcher.stop()
            else:
                timeout_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
        except KeyboardInterrupt:
            watcher.stop()
            await asyncio.gather(run_task, return_exceptions=True)

    print("-" * 80)
    print(f"Total de sinais detectados: {n_signals}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=int, default=5)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(main(args.minutes))
    except KeyboardInterrupt:
        pass
