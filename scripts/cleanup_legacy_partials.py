"""One-shot cleanup das posições PARTIALLY_CLOSED legacy.

Contexto: o TP parcial foi removido do `exit_manager` (commit 7116edc) mas
as posições criadas antes desse commit ficaram em ``PARTIALLY_CLOSED``
para sempre — ninguém as fechava. O ``paper_reset`` original também não
as apanhava (só filtrava OPEN).

Este script fecha-as mark-to-market via Gamma API, com:
- ``status = CLOSED``
- ``closed_at = 2026-05-10 22:55:00`` (= timestamp do paper_reset W19 que
  estava em vigor quando estas eram open; mantém o pnl FORA da W20)
- ``notes = 'legacy_partial_cleanup'``

Uso:
    .venv/bin/python scripts/cleanup_legacy_partials.py            # dry-run
    .venv/bin/python scripts/cleanup_legacy_partials.py --apply    # aplica
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
from polymarket_bot.monitoring.market_builder import MarketBuilder, MarketBuildError


# Timestamp do paper_reset W19 (= antes da W20 começar). Garante que o pnl
# destas trades NÃO conta para a semana actual (W20 começa a 11/5 00:00 UTC).
CLEANUP_CLOSED_AT = datetime(2026, 5, 10, 22, 55, 0, tzinfo=timezone.utc)


async def main(apply_changes: bool) -> int:
    settings = get_settings()
    sm = get_sessionmaker()

    async with sm() as session:
        stmt = select(Position).where(
            Position.status == PositionStatus.PARTIALLY_CLOSED
        )
        positions = list((await session.execute(stmt)).scalars().all())

    if not positions:
        print("Sem PARTIALLY_CLOSED — nada a fazer.")
        return 0

    print(f"A verificar {len(positions)} posições PARTIALLY_CLOSED via Gamma…\n")

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"Accept": "application/json"}
    ) as http:
        builder = MarketBuilder(
            gamma_url=settings.polymarket_gamma_api_url,
            clob_url=settings.clob_api_url,
            session=http,
        )

        to_close: list[tuple[Position, Decimal, Decimal]] = []
        for pos in positions:
            current_price: Decimal | None = None
            try:
                snap = await builder.build(pos.market_id, pos.outcome)
                # mid price > implied_probability > avg_entry como fallback
                if snap.orderbook.mid_price is not None:
                    current_price = snap.orderbook.mid_price
                elif snap.implied_probability is not None:
                    current_price = snap.implied_probability
            except MarketBuildError as exc:
                print(f"#{pos.id}  build falhou: {exc}")
                # Tenta verificar se está resolvido
                try:
                    resolved, winner = await builder.get_resolution_status(
                        pos.market_id
                    )
                    if resolved:
                        if (winner or "").upper() == (pos.outcome or "").upper():
                            current_price = Decimal("1.0")
                        else:
                            current_price = Decimal("0.0")
                except Exception as exc2:  # noqa: BLE001
                    print(f"#{pos.id}  resolução check falhou: {exc2}")
            except Exception as exc:  # noqa: BLE001
                print(f"#{pos.id}  erro inesperado: {exc}")

            if current_price is None:
                # Fallback final: usa avg_entry (= pnl 0). Não inventa.
                current_price = pos.avg_entry_price

            if pos.avg_entry_price > 0:
                pnl_ratio = (current_price / pos.avg_entry_price) - Decimal("1")
                pnl = (pos.size_usd * pnl_ratio).quantize(Decimal("0.01"))
            else:
                pnl = Decimal("0")

            print(
                f"#{pos.id:3d}  {pos.market_id[:20]}...  "
                f"entry={float(pos.avg_entry_price):.3f}  "
                f"now={float(current_price):.3f}  "
                f"size=${float(pos.size_usd):.2f}  "
                f"pnl=${float(pnl):+.2f}"
            )
            to_close.append((pos, current_price, pnl))

    if not apply_changes:
        total_pnl = sum(pnl for _, _, pnl in to_close)
        print(
            f"\n{len(to_close)} posições prontas. P&L total ${float(total_pnl):+.2f}."
            f"\nRe-corre com --apply para escrever na DB."
        )
        return 0

    async with sm() as session:
        for pos, exit_price, pnl in to_close:
            fresh = await session.get(Position, pos.id)
            if fresh is None or fresh.status == PositionStatus.CLOSED:
                continue
            fresh.status = PositionStatus.CLOSED
            fresh.closed_at = CLEANUP_CLOSED_AT
            fresh.realized_pnl_usd = pnl
            fresh.notes = "legacy_partial_cleanup"
        await session.commit()

    print(
        f"\n✓ {len(to_close)} posições PARTIALLY_CLOSED legacy fechadas "
        f"com closed_at={CLEANUP_CLOSED_AT.isoformat()}."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(apply_changes=args.apply)))
