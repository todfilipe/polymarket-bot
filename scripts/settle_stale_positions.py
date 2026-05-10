"""Settle one-off de posições abertas cujos mercados já resolveram.

Bug: o `_fetch_market` antes não fazia fallback para `closed=true` no Gamma,
pelo que mercados resolvidos não eram detectados pelo `ExitManager` e as
posições ficavam open para sempre. Este script aplica a settlement
manualmente — útil depois de fix do bug ou para limpar legacy.

Uso:
    python scripts/settle_stale_positions.py          # dry-run, só mostra
    python scripts/settle_stale_positions.py --apply  # escreve mudanças
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import aiohttp
from sqlalchemy import select

from polymarket_bot.config import get_settings
from polymarket_bot.db.enums import PositionStatus
from polymarket_bot.db.models import Position
from polymarket_bot.db.session import get_sessionmaker
from polymarket_bot.monitoring.market_builder import MarketBuilder


async def main(apply_changes: bool) -> int:
    settings = get_settings()
    sm = get_sessionmaker()

    async with sm() as session:
        stmt = select(Position).where(Position.status == PositionStatus.OPEN)
        positions = list((await session.execute(stmt)).scalars().all())

    if not positions:
        print("Sem posições abertas — nada a fazer.")
        return 0

    print(f"A verificar {len(positions)} posições abertas via Gamma…\n")

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"Accept": "application/json"}
    ) as http:
        builder = MarketBuilder(
            gamma_url=settings.polymarket_gamma_api_url,
            clob_url=settings.clob_api_url,
            session=http,
        )

        to_close: list[tuple[Position, str, Decimal, Decimal]] = []
        for pos in positions:
            try:
                resolved, winner = await builder.get_resolution_status(
                    pos.market_id
                )
            except Exception as exc:  # noqa: BLE001
                print(f"#{pos.id}  {pos.outcome:<25} ERRO: {exc}")
                continue

            if not resolved:
                print(f"#{pos.id}  {pos.outcome:<25} ainda activo")
                continue

            our_outcome = (pos.outcome or "").upper()
            if winner == our_outcome:
                final_value = pos.size_usd / pos.avg_entry_price
                pnl = final_value - pos.size_usd
                exit_price = Decimal("1.0")
                reason = "RESOLVED_WIN"
            else:
                pnl = -pos.size_usd
                exit_price = Decimal("0.0")
                reason = "RESOLVED_LOSS"

            print(
                f"#{pos.id}  {pos.outcome:<25} {reason}  pnl=${float(pnl):+.2f}  "
                f"(winner={winner})"
            )
            to_close.append((pos, reason, exit_price, pnl))

    if not to_close:
        print("\nNenhuma posição precisa de settlement.")
        return 0

    if not apply_changes:
        print(f"\n{len(to_close)} posições prontas para settle. Re-corre com --apply.")
        return 0

    # Aplica as mudanças
    now = datetime.now(timezone.utc)
    async with sm() as session:
        for pos, reason, exit_price, pnl in to_close:
            fresh = await session.get(Position, pos.id)
            if fresh is None or fresh.status != PositionStatus.OPEN:
                continue
            fresh.status = PositionStatus.CLOSED
            fresh.realized_pnl_usd = pnl
            fresh.closed_at = now
            fresh.notes = reason
        await session.commit()

    print(f"\n✓ {len(to_close)} posições settled.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--apply", action="store_true",
        help="Escreve mudanças à DB. Sem este flag, é dry-run."
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sys.exit(asyncio.run(main(args.apply)))
