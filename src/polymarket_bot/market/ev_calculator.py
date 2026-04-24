"""Cálculo de Expected Value (§3.4).

Fórmula do docx:
    potential_profit = stake × (1 / entry_price − 1)
    EV = true_probability × potential_profit − (1 − true_probability) × stake
    margin = EV / stake
    condição de entrada: margin > MIN_EV_MARGIN (10%)

------------------------------------------------------------------------------
ESTIMATIVA DE `true_probability` — ancorada no win rate das wallets
------------------------------------------------------------------------------
O docx apresenta a fórmula do EV mas não define como estimar `true_probability`.
Se `true_probability == entry_price`, o EV é sempre 0 (mercado eficiente).

V2 — edge proporcional ao win rate histórico das wallets sinalizadoras:

    edge_base        = (avg_win_rate − 0.50) × WIN_RATE_EDGE_FACTOR
    consensus_factor = 1.0 (1 wallet) | 1.5 (2 wallets) | 2.0 (3+ wallets)
    estimated_true_prob = clamp(
        market_price + edge_base × consensus_factor,
        market_price,
        0.99,
    )

Racional:
- Wallet com 50% win rate não tem edge → edge_base = 0.
- Wallet de elite a 70% → edge_base = 0.10, com 2 wallets = +15% sobre preço.
- Ancorado em dados reais (o win rate é medido, não assumido).
- O fator `WIN_RATE_EDGE_FACTOR` (0.5 por agora) é o único parâmetro a
  recalibrar com dados reais após ≥ 2 semanas de paper trading.
------------------------------------------------------------------------------
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from polymarket_bot.config.constants import CONST


# Parâmetros calibráveis (v2)
WIN_RATE_EDGE_FACTOR: float = 0.5
"""Quanto do excesso de win rate se traduz em edge sobre o preço de mercado."""

CONSENSUS_FACTORS: dict[int, float] = {1: 1.0, 2: 1.5}
"""n_wallets → multiplicador. 3+ wallets usa o default 2.0."""
CONSENSUS_FACTOR_DEFAULT_FOR_3_PLUS: float = 2.0

MAX_ESTIMATED_PROB: Decimal = Decimal("0.99")


@dataclass(frozen=True)
class EvResult:
    stake_usd: Decimal
    entry_price: Decimal
    true_probability: Decimal
    potential_profit_usd: Decimal
    expected_value_usd: Decimal
    margin: float                     # EV / stake — fração
    passes_min_margin: bool
    reason: str | None                # preenchido se falha


def compute_ev(
    stake_usd: Decimal,
    entry_price: Decimal,
    true_probability: Decimal,
) -> EvResult:
    """Calcula EV puro. `true_probability` é input — estimado externamente."""
    if entry_price <= 0 or entry_price >= 1:
        return EvResult(
            stake_usd=stake_usd,
            entry_price=entry_price,
            true_probability=true_probability,
            potential_profit_usd=Decimal("0"),
            expected_value_usd=Decimal("0"),
            margin=0.0,
            passes_min_margin=False,
            reason=f"entry_price inválido: {entry_price}",
        )
    if not (0 <= true_probability <= 1):
        return EvResult(
            stake_usd=stake_usd,
            entry_price=entry_price,
            true_probability=true_probability,
            potential_profit_usd=Decimal("0"),
            expected_value_usd=Decimal("0"),
            margin=0.0,
            passes_min_margin=False,
            reason=f"true_probability fora de [0,1]: {true_probability}",
        )

    potential_profit = stake_usd * (Decimal(1) / entry_price - Decimal(1))
    ev = true_probability * potential_profit - (Decimal(1) - true_probability) * stake_usd
    margin = float(ev / stake_usd) if stake_usd > 0 else 0.0
    passes = margin > CONST.MIN_EV_MARGIN
    reason = (
        None
        if passes
        else f"EV margin {margin:.2%} ≤ mín {CONST.MIN_EV_MARGIN:.0%}"
    )

    return EvResult(
        stake_usd=stake_usd,
        entry_price=entry_price,
        true_probability=true_probability,
        potential_profit_usd=potential_profit,
        expected_value_usd=ev,
        margin=margin,
        passes_min_margin=passes,
        reason=reason,
    )


@dataclass(frozen=True)
class ProbabilityEstimate:
    """Explicabilidade do estimador — úteis para logs e Telegram."""

    market_price: Decimal
    avg_win_rate: float
    n_wallets: int
    consensus_factor: float
    edge_base: float
    edge_applied: float
    estimated_probability: Decimal


def estimate_true_probability(
    market_price: Decimal,
    win_rates: Sequence[float],
    edge_factor: float = WIN_RATE_EDGE_FACTOR,
) -> ProbabilityEstimate:
    """Estima `true_probability` a partir do win rate das wallets em consenso.

    Args:
        market_price: probabilidade implícita pelo orderbook (mid).
        win_rates: win rate histórico de cada wallet sinalizadora (0-1).
        edge_factor: quanto do excesso sobre 0.50 vira edge. Calibrável.
    """
    n = len(win_rates)
    if n == 0:
        return ProbabilityEstimate(
            market_price=market_price,
            avg_win_rate=0.0,
            n_wallets=0,
            consensus_factor=0.0,
            edge_base=0.0,
            edge_applied=0.0,
            estimated_probability=market_price,
        )

    avg_wr = sum(win_rates) / n
    consensus_factor = CONSENSUS_FACTORS.get(n, CONSENSUS_FACTOR_DEFAULT_FOR_3_PLUS)

    edge_base = (avg_wr - 0.50) * edge_factor
    edge_applied = max(0.0, edge_base * consensus_factor)  # nunca penaliza
    estimated = market_price + Decimal(str(edge_applied))
    estimated = min(estimated, MAX_ESTIMATED_PROB)
    estimated = max(estimated, market_price)  # floor = preço de mercado

    return ProbabilityEstimate(
        market_price=market_price,
        avg_win_rate=avg_wr,
        n_wallets=n,
        consensus_factor=consensus_factor,
        edge_base=edge_base,
        edge_applied=edge_applied,
        estimated_probability=estimated,
    )
