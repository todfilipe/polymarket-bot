"""Força o rebalanceamento semanal — correr sob demanda.

Uso:
    python scripts/force_rebalance.py              # rebalance normal (persiste DB)
    python scripts/force_rebalance.py --verbose    # diagnóstico das 3 primeiras
                                                   # candidatas (não toca na DB)

Modo normal: corre o pipeline de discovery + scoring e persiste as novas top 7
wallets em SQLite (top 3 como ``WalletTier.TOP``, 4 seguintes como
``WalletTier.BOTTOM``).

Modo ``--verbose``: puxa o leaderboard, pega nas 3 primeiras, busca os trades
de cada uma e imprime as métricas brutas + razões de exclusão. Usa-se quando
o pipeline devolve 0 elegíveis e queremos saber se o problema está na API
(dados vazios/mal formatados) ou nos filtros (demasiado restritivos).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from polymarket_bot.api import PolymarketDataClient  # noqa: E402
from polymarket_bot.config import get_settings  # noqa: E402
from polymarket_bot.db.enums import WalletTier  # noqa: E402
from polymarket_bot.db.models import Wallet  # noqa: E402
from polymarket_bot.db.session import get_sessionmaker  # noqa: E402
from polymarket_bot.wallets.discovery import discover_and_score  # noqa: E402
from polymarket_bot.wallets.filters import check_eligibility  # noqa: E402
from polymarket_bot.wallets.metrics import compute_metrics  # noqa: E402


DIVIDER = "=" * 64
SUBDIVIDER = "-" * 64
TOP_TIER_COUNT = 3
TOTAL_FOLLOWED = 7
VERBOSE_SAMPLE_SIZE = 3


async def run_rebalance() -> int:
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
        print("        Corre com --verbose para ver porquê.")
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


async def run_verbose_diagnostic(sample_size: int = VERBOSE_SAMPLE_SIZE) -> int:
    """Pega nas primeiras N wallets do leaderboard e dump das métricas brutas.

    Não persiste nada. Serve apenas para perceber se o pipeline está a receber
    dados válidos da Data API e onde é que os filtros excluem as candidatas.
    """
    settings = get_settings()
    window_weeks = settings.scoring_window_weeks

    print(DIVIDER)
    print(f"  Polymarket — diagnóstico verbose (top {sample_size} wallets)")
    print(DIVIDER)
    print(f"Janela de scoring: {window_weeks} semanas")
    print()

    async with PolymarketDataClient() as client:
        leaderboard = await client.get_top_wallets_by_profit(
            window_weeks=window_weeks, top_n=sample_size
        )
        print(f"Leaderboard devolveu {len(leaderboard)} entrada(s).")
        print()

        if not leaderboard:
            print("[ERRO] Leaderboard vazio — a API está a devolver lista vazia.")
            return 1

        exclusion_counter: Counter[str] = Counter()

        for idx, entry in enumerate(leaderboard[:sample_size], start=1):
            print(SUBDIVIDER)
            print(f"  [{idx}] Wallet {entry.wallet_address}")
            print(SUBDIVIDER)
            print(
                f"  Leaderboard: profit=${entry.profit_usd:,.2f} | "
                f"volume=${entry.volume_usd:,.2f}"
            )

            trades = await client.get_wallet_trades_for_scoring(
                entry.wallet_address, window_weeks=window_weeks
            )
            positions = await client.get_realized_positions(
                entry.wallet_address, window_weeks=window_weeks
            )
            print(f"  Trades (últ. {window_weeks}w)      : {len(trades)}")
            print(f"  Posições (activity+snapshot): {len(positions)}")

            if not trades and not positions:
                print("  [AVISO] Sem trades nem posições — API devolveu vazio.")
                print()
                continue

            resolved_positions = [p for p in positions if p.is_resolved]
            with_category_trades = sum(1 for t in trades if t.category)
            with_category_pos = sum(1 for p in positions if p.category)
            print(
                f"  Posições resolvidas     : {len(resolved_positions)}/{len(positions)}"
            )
            print(
                f"  Trades com categoria    : {with_category_trades}/{len(trades)}"
                f"  |  Posições com categoria: {with_category_pos}/{len(positions)}"
            )

            metrics = compute_metrics(
                entry.wallet_address,
                trades,
                window_weeks=window_weeks,
                positions=positions,
            )
            eligibility = check_eligibility(metrics)

            print(f"  total_trades            : {metrics.total_trades}")
            print(f"  total_volume_usd        : ${float(metrics.total_volume_usd):,.2f}")
            print(
                f"  total_profit_usd        : ${float(metrics.total_profit_usd):,.2f}"
                f"  |  total_loss_usd: ${float(metrics.total_loss_usd):,.2f}"
            )
            print(f"  profit_factor           : {metrics.profit_factor:.3f}")
            print(f"  win_rate                : {metrics.win_rate:.1%}")
            print(f"  max_drawdown            : {metrics.max_drawdown:.1%}")
            print(f"  consistency_pct         : {metrics.consistency_pct:.1%}")
            print(f"  categorias únicas       : {len(metrics.categories)}  {sorted(metrics.categories) or '—'}")
            print(f"  luck_concentration      : {metrics.luck_concentration:.1%}")
            print(f"  last_trade_at           : {metrics.last_trade_at}")

            if eligibility.is_eligible:
                print("  RESULTADO               : ELEGÍVEL")
            else:
                print("  RESULTADO               : EXCLUÍDA")
                for reason in eligibility.reasons:
                    print(f"    - {reason}")
                    exclusion_counter[_reason_key(reason)] += 1
            print()

    print(DIVIDER)
    print("  Resumo das exclusões (nas 3 amostras)")
    print(DIVIDER)
    if not exclusion_counter:
        print("  Nenhuma exclusão — os filtros não são o bloqueio.")
    else:
        for key, count in exclusion_counter.most_common():
            print(f"  {count}×  {key}")
    print(DIVIDER)
    return 0


def _reason_key(reason: str) -> str:
    """Extrai a primeira parte da razão para agrupar contagens."""
    return reason.split(" (", 1)[0].split(":")[0].strip()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Força rebalance ou corre diagnóstico.")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Diagnóstico das 3 primeiras wallets do leaderboard (não persiste).",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=VERBOSE_SAMPLE_SIZE,
        help=f"Número de wallets a inspecionar em modo verbose (default {VERBOSE_SAMPLE_SIZE}).",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.verbose:
        return await run_verbose_diagnostic(sample_size=args.sample_size)
    return await run_rebalance()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
