"""Discovery do universo de wallets candidatas (§2).

Domingo 23:00 — pede à Data API o top-N por profit nas últimas 4 semanas,
para cada candidata puxa o histórico completo e alimenta o scoring.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger

from polymarket_bot.api import PolymarketDataClient, WalletProfitSummary
from polymarket_bot.config import get_settings
from polymarket_bot.wallets.scoring import ScoredWallet, rank_wallets, score_wallet


@dataclass(frozen=True)
class DiscoveryReport:
    candidates: int
    eligible: int
    top_scored: list[ScoredWallet]


async def discover_and_score(
    top_n: int | None = None,
    window_weeks: int | None = None,
    max_concurrency: int = 5,
) -> DiscoveryReport:
    """Pipeline completo de discovery + scoring.

    Retorna a lista ordenada de scored wallets. A seleção final das 7 é feita
    pelo scheduler (aplica pesos Top3/Bottom4 e bónus de diversificação).
    """
    settings = get_settings()
    top_n = top_n or settings.wallet_discovery_top_n
    window_weeks = window_weeks or settings.scoring_window_weeks

    async with PolymarketDataClient() as client:
        leaderboard = await client.get_top_wallets_by_profit(
            window_weeks=window_weeks, top_n=top_n
        )
        logger.info("discovery: {} candidatas do leaderboard", len(leaderboard))

        sem = asyncio.Semaphore(max_concurrency)
        scored_or_none = await asyncio.gather(
            *(
                _score_one(client, entry, window_weeks, sem)
                for entry in leaderboard
                if entry.wallet_address
            )
        )
        scored = [s for s in scored_or_none if s is not None]

    ranked = rank_wallets(scored)
    eligible_count = sum(1 for s in scored if s.eligibility.is_eligible)
    logger.info(
        "discovery: {}/{} elegíveis, top score = {:.1f}",
        eligible_count,
        len(scored),
        ranked[0].total_score if ranked else 0,
    )
    return DiscoveryReport(
        candidates=len(leaderboard),
        eligible=eligible_count,
        top_scored=ranked,
    )


async def _score_one(
    client: PolymarketDataClient,
    entry: WalletProfitSummary,
    window_weeks: int,
    sem: asyncio.Semaphore,
) -> ScoredWallet | None:
    async with sem:
        try:
            trades = await client.get_wallet_trades_for_scoring(
                entry.wallet_address, window_weeks=window_weeks
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "discovery: falha a buscar trades de {}: {}",
                entry.wallet_address,
                exc,
            )
            return None
    return score_wallet(entry.wallet_address, trades, window_weeks=window_weeks)
