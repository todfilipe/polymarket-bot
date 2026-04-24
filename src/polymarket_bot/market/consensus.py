"""Análise de consenso entre wallets seguidas (§3.3 + §4.2).

Dado um conjunto de sinais de wallets sobre o mesmo mercado, decide:
- Direção consolidada (BUY YES, BUY NO, ou ABORT).
- Força do sinal (solo / consenso / all-wallets) — multiplicador de sizing.
- Tier dominante (top / bottom) — multiplicador de tier.

Regras (§3.3):
    0 wallets                 → ABORT (sem sinal)
    1 wallet                  → força = SOLO (0.5×)
    2+ wallets mesma direção  → força = CONSENSUS (1.3×)
    2+ wallets opostas        → ABORT (conflito)
    Todas as 7 mesma direção  → força = ALL (até 1.5×)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from polymarket_bot.config.constants import CONST
from polymarket_bot.db.enums import TradeSide, WalletTier


@dataclass(frozen=True)
class WalletSignal:
    """Sinal isolado de uma wallet num mercado.

    `outcome` = "YES" ou "NO". `side` sempre BUY no contexto de copytrade
    (entradas) — SELL é para exits e não entra nesta análise.
    """

    wallet_address: str
    tier: WalletTier
    outcome: str
    side: TradeSide = TradeSide.BUY


class ConsensusStrength(StrEnum):
    ABORT = "ABORT"
    SOLO = "SOLO"           # 1 wallet apenas
    CONSENSUS = "CONSENSUS"  # 2+ na mesma direção
    ALL = "ALL"              # todas as wallets seguidas


@dataclass(frozen=True)
class ConsensusDecision:
    strength: ConsensusStrength
    direction_outcome: str | None      # YES / NO, None se ABORT
    n_wallets: int
    n_top_tier: int
    n_bottom_tier: int
    tier_multiplier: float              # 1.4 ou 0.7
    signal_multiplier: float            # 0.5, 1.0, 1.3 ou 1.5
    reason: str | None                  # se ABORT

    @property
    def should_enter(self) -> bool:
        return self.strength != ConsensusStrength.ABORT

    @property
    def combined_sizing_multiplier(self) -> float:
        """Multiplicador total a aplicar à size base (tier × signal)."""
        return self.tier_multiplier * self.signal_multiplier


def analyze_consensus(
    signals: list[WalletSignal],
    total_followed_wallets: int = CONST.WALLETS_FOLLOWED,
) -> ConsensusDecision:
    """Avalia sinais e devolve decisão de entrada + multiplicadores."""
    if not signals:
        return _abort("sem sinais")

    # --- Conflito YES vs NO ---
    outcomes = {s.outcome.upper() for s in signals}
    if len(outcomes) > 1:
        return _abort(f"wallets em direções opostas ({sorted(outcomes)})")

    direction = next(iter(outcomes))
    n = len(signals)
    tier_counts = Counter(s.tier for s in signals)
    n_top = tier_counts.get(WalletTier.TOP, 0)
    n_bot = tier_counts.get(WalletTier.BOTTOM, 0)

    # --- Tier multiplier: se qualquer wallet TOP sinaliza, usa 1.4; senão 0.7 ---
    tier_mult = CONST.TIER_TOP_MULTIPLIER if n_top > 0 else CONST.TIER_BOTTOM_MULTIPLIER

    # --- Signal strength ---
    if n == 1:
        strength = ConsensusStrength.SOLO
        signal_mult = CONST.SOLO_WALLET_MULTIPLIER
    elif n >= total_followed_wallets:
        strength = ConsensusStrength.ALL
        signal_mult = CONST.ALL_WALLETS_MULTIPLIER
    else:
        strength = ConsensusStrength.CONSENSUS
        signal_mult = CONST.CONSENSUS_MULTIPLIER

    return ConsensusDecision(
        strength=strength,
        direction_outcome=direction,
        n_wallets=n,
        n_top_tier=n_top,
        n_bottom_tier=n_bot,
        tier_multiplier=tier_mult,
        signal_multiplier=signal_mult,
        reason=None,
    )


def _abort(reason: str) -> ConsensusDecision:
    return ConsensusDecision(
        strength=ConsensusStrength.ABORT,
        direction_outcome=None,
        n_wallets=0,
        n_top_tier=0,
        n_bottom_tier=0,
        tier_multiplier=0.0,
        signal_multiplier=0.0,
        reason=reason,
    )
