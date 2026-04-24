"""Dimensionamento de posições (CLAUDE.md §6, docx §4.2).

Fórmula:
    base = 5% da banca atual
    × tier       (Top 3 = 1.4, Bottom 4 = 0.7)     — via ConsensusDecision
    × consenso   (2+ wallets = 1.3, 1 wallet = 0.5, all = 1.5) — via ConsensusDecision
    × recuperação (após 2 perdas consecutivas = 0.6)
    HARD CAP: 8% da banca
    MÍNIMO: $20 — abaixo disto, skip porque fees absorvem o lucro

O sizer NÃO conhece o mercado nem o EV. Devolve apenas o tamanho proposto.
O pipeline decide se avança (após validar depth, EV, limites de portfolio).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from polymarket_bot.config.constants import CONST
from polymarket_bot.market.consensus import ConsensusDecision


@dataclass(frozen=True)
class SizeDecision:
    """Resultado do sizing — self-contained para persistir em logs/DB."""

    bankroll_usd: Decimal
    base_size_usd: Decimal
    tier_multiplier: float
    signal_multiplier: float
    recovery_multiplier: float
    pre_cap_size_usd: Decimal
    final_size_usd: Decimal           # 0 se rejeitado
    hit_cap: bool
    below_minimum: bool
    skip_reason: str | None

    @property
    def should_execute(self) -> bool:
        return self.final_size_usd >= Decimal(str(CONST.MIN_TRADE_USD))


def compute_size(
    bankroll_usd: Decimal,
    consensus: ConsensusDecision,
    recovery_mode: bool = False,
) -> SizeDecision:
    """Aplica a fórmula hierárquica com hard cap 8% e mínimo $20."""
    base = (bankroll_usd * Decimal(str(CONST.BASE_SIZE_RATIO))).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    recovery_mult = CONST.RECOVERY_MULTIPLIER if recovery_mode else 1.0

    pre_cap = (
        base
        * Decimal(str(consensus.tier_multiplier))
        * Decimal(str(consensus.signal_multiplier))
        * Decimal(str(recovery_mult))
    ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    cap = (bankroll_usd * Decimal(str(CONST.MAX_SIZE_RATIO))).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    capped = min(pre_cap, cap)
    hit_cap = pre_cap > cap

    min_trade = Decimal(str(CONST.MIN_TRADE_USD))
    below_min = capped < min_trade

    return SizeDecision(
        bankroll_usd=bankroll_usd,
        base_size_usd=base,
        tier_multiplier=consensus.tier_multiplier,
        signal_multiplier=consensus.signal_multiplier,
        recovery_multiplier=recovery_mult,
        pre_cap_size_usd=pre_cap,
        final_size_usd=Decimal("0") if below_min else capped,
        hit_cap=hit_cap,
        below_minimum=below_min,
        skip_reason=(
            f"size ${capped} < mínimo ${min_trade}" if below_min else None
        ),
    )
