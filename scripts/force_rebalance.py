"""Força o rebalanceamento semanal — correr sob demanda.

Uso:
    python scripts/force_rebalance.py

Corre o pipeline de discovery + scoring imediatamente (sem esperar pelo
scheduler de domingo às 23:00) e persiste as novas top 7 wallets em SQLite:
top 3 marcadas como ``WalletTier.TOP`` (peso 60%) e as 4 seguintes como
``WalletTier.BOTTOM`` (peso 40%). Se o scoring não devolver candidatas
elegíveis, o script avisa e termina sem erro — útil quando a Data API
está temporariamente sem dados ou nenhuma wallet passa os filtros.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from polymarket_bot.config import get_settings  # noqa: E402
from polymarket_bot.db.enums import WalletTier  # noqa: E402
from polymarket_bot.db.models import Wallet  # noqa: E402
from polymarket_bot.db.session import get_sessionmaker  # noqa: E402
from polymarket_bot.wallets.discovery import discover_and_score  # noqa: E402


DIVIDER = "=" * 64
TOP_TIER_COUNT = 3
TOTAL_FOLLOWED = 7


async def main() -> int:
    get_settings()
    sessionmaker = get_sessionmaker()

    print(DIVIDER)
    print("  Polymarket — force rebalance")
    print(DIVIDER)
    print("A correr discovery + scoring...")
    print()

    report = await discover_and_score()

    print(f"Candidatas do leaderboard : {report.candidates}")
    print(f"Elegíveis após filtros    : {report.eligible}")
    print()

    if not report.top_scored:
        print("[AVISO] Nenhuma wallet elegível devolvida pelo scoring.")
        print("        A DB não foi alterada. Tentar novamente mais tarde.")
        return 0

    selected = report.top_scored[:TOTAL_FOLLOWED]

    async with sessionmaker() as session:
        for idx, scored in enumerate(selected):
            tier = WalletTier.TOP if idx < TOP_TIER_COUNT else WalletTier.BOTTOM
            await session.merge(
                Wallet(
                    address=scored.wallet_address,
                    is_followed=True,
                    current_tier=tier,
                )
            )
        await session.commit()

    print(DIVIDER)
    print(f"  Top {len(selected)} wallets persistidas em SQLite")
    print(DIVIDER)
    for idx, scored in enumerate(selected, start=1):
        tier = WalletTier.TOP if idx <= TOP_TIER_COUNT else WalletTier.BOTTOM
        addr = scored.wallet_address
        short_addr = f"{addr[:6]}...{addr[-4:]}"
        print(f"  {idx:>2}. [{tier.value:<6}] {short_addr}  score={scored.total_score:6.2f}")
    print(DIVIDER)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
