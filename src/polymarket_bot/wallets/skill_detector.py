"""Deteção de skill de timing nos exits da wallet (§6.2).

Mede retrospetivamente: quando a wallet fechou uma posição antes da resolução,
o mercado foi contra ela? Se sim em >60% dos casos → seguir exits é útil.
Caso contrário, ignorar exits e usar apenas os nossos níveis próprios.

Input: sequência cronológica de trades + preços pós-exit (via Data API/Gamma).
Output: ratio 0.0–1.0 de "exits com timing correto".
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from polymarket_bot.api.polymarket_data import RawTrade
from polymarket_bot.db.enums import TradeSide


@dataclass(frozen=True)
class ExitDecision:
    market_id: str
    exit_price: Decimal
    exit_time: object  # datetime
    final_price: Decimal | None  # preço à resolução, ou ao fim da janela
    side_closed: TradeSide  # lado fechado pela wallet


def pair_entries_and_exits(trades: list[RawTrade]) -> list[ExitDecision]:
    """Empareja BUY/SELL por market_id para extrair decisões de exit.

    Heurística: percorre trades ordenados por tempo, acumula posição por
    (market_id, outcome). Quando a posição fecha (size ≤ 0) regista um exit.
    Não captura partial closes complexos — versão v1.
    """
    by_market: dict[tuple[str, str], list[RawTrade]] = defaultdict(list)
    for t in sorted(trades, key=lambda x: x.executed_at):
        by_market[(t.market_id, t.outcome)].append(t)

    exits: list[ExitDecision] = []
    for (market_id, _), group in by_market.items():
        net_size = Decimal("0")
        last_sell: RawTrade | None = None
        for t in group:
            signed = t.size if t.side == TradeSide.BUY else -t.size
            net_size += signed
            if t.side == TradeSide.SELL:
                last_sell = t
            if net_size <= 0 and last_sell is not None:
                exits.append(
                    ExitDecision(
                        market_id=market_id,
                        exit_price=last_sell.price,
                        exit_time=last_sell.executed_at,
                        final_price=None,  # a preencher via market resolution
                        side_closed=TradeSide.SELL,
                    )
                )
                net_size = Decimal("0")
                last_sell = None
    return exits


def compute_timing_skill(
    exits: list[ExitDecision], resolved_prices: dict[str, Decimal]
) -> float:
    """Fração de exits "acertados": preço final < preço de exit (para long).

    Requer mapa `resolved_prices` com o preço de resolução por market_id.
    Se um mercado ainda não resolveu, é ignorado.
    """
    scored = 0
    correct = 0
    for e in exits:
        final = resolved_prices.get(e.market_id)
        if final is None:
            continue
        scored += 1
        # Saiu a tempo se o preço caiu após o exit
        if final < e.exit_price:
            correct += 1
    if scored == 0:
        return 0.0
    return correct / scored
