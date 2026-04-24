"""Algoritmo de scoring de wallets (§2.2).

Score composto 0–100 ponderado por 6 métricas:

    Profit Factor      30%   — lucro total / perda total
    Consistência       20%   — % semanas com P&L positivo
    Win Rate           20%   — fração de trades vencedores
    Drawdown           15%   — penalidade se > 35%
    Diversificação     10%   — categorias distintas
    Recência            5%   — performance das últimas 4 semanas

Cada métrica é normalizada para [0, 1] e multiplicada pelo seu peso.
Wallets inelegíveis recebem score 0 e são marcadas com razões.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from polymarket_bot.api.polymarket_data import RawPosition, RawTrade
from polymarket_bot.config.constants import CONST
from polymarket_bot.wallets.filters import EligibilityResult, check_eligibility
from polymarket_bot.wallets.metrics import WalletMetrics, compute_metrics


@dataclass(frozen=True)
class ScoredWallet:
    wallet_address: str
    total_score: float  # 0-100
    metrics: WalletMetrics
    eligibility: EligibilityResult

    # Componentes normalizados (0-1) — úteis para explicabilidade
    profit_factor_norm: float
    consistency_norm: float
    win_rate_norm: float
    drawdown_norm: float
    diversification_norm: float
    recency_norm: float

    @property
    def is_selectable(self) -> bool:
        return self.eligibility.is_eligible and self.total_score > 0


def score_wallet(
    wallet_address: str,
    trades: Iterable[RawTrade],
    window_weeks: int = CONST.SCORING_WINDOW_WEEKS,
    positions: Iterable[RawPosition] | None = None,
) -> ScoredWallet:
    """Ponto de entrada — computa métricas, aplica filtros e devolve score 0–100.

    ``positions`` é opcional: quando fornecido (produção), é a fonte de P&L;
    quando ``None`` (testes), o fallback usa ``RawTrade.realized_pnl_usd``.
    """
    metrics = compute_metrics(
        wallet_address, trades, window_weeks=window_weeks, positions=positions
    )
    eligibility = check_eligibility(metrics)

    pf_norm = _norm_profit_factor(metrics.profit_factor)
    cons_norm = _clamp01(metrics.consistency_pct)
    wr_norm = _norm_win_rate(metrics.win_rate)
    dd_norm = _norm_drawdown(metrics.max_drawdown)
    div_norm = _norm_diversification(len(metrics.categories))
    rec_norm = _clamp01(metrics.recency_score)

    total_score = (
        pf_norm * CONST.SCORE_WEIGHT_PROFIT_FACTOR
        + cons_norm * CONST.SCORE_WEIGHT_CONSISTENCY
        + wr_norm * CONST.SCORE_WEIGHT_WIN_RATE
        + dd_norm * CONST.SCORE_WEIGHT_DRAWDOWN
        + div_norm * CONST.SCORE_WEIGHT_DIVERSIFICATION
        + rec_norm * CONST.SCORE_WEIGHT_RECENCY
    ) * 100.0

    if not eligibility.is_eligible:
        total_score = 0.0

    return ScoredWallet(
        wallet_address=wallet_address.lower(),
        total_score=round(total_score, 2),
        metrics=metrics,
        eligibility=eligibility,
        profit_factor_norm=pf_norm,
        consistency_norm=cons_norm,
        win_rate_norm=wr_norm,
        drawdown_norm=dd_norm,
        diversification_norm=div_norm,
        recency_norm=rec_norm,
    )


def rank_wallets(scored: Iterable[ScoredWallet]) -> list[ScoredWallet]:
    """Ordena por score descendente, filtrando inelegíveis."""
    return sorted(
        (s for s in scored if s.is_selectable),
        key=lambda s: s.total_score,
        reverse=True,
    )


# -------------------------------------------------- Normalização de métricas
def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _norm_profit_factor(pf: float) -> float:
    """PF=1.5 → 0.0 (mínimo), PF=3.5 → 1.0, linear acima do mínimo."""
    if pf < CONST.MIN_PROFIT_FACTOR:
        return 0.0
    # PF ≥ 3.5 satura em 1.0; PF ∈ [1.5, 3.5] → [0, 1]
    return _clamp01((pf - CONST.MIN_PROFIT_FACTOR) / 2.0)


def _norm_win_rate(wr: float) -> float:
    """Win rate 55% → 0.0, 80% → 1.0 (saturação)."""
    if wr < CONST.MIN_WIN_RATE:
        return 0.0
    return _clamp01((wr - CONST.MIN_WIN_RATE) / (0.80 - CONST.MIN_WIN_RATE))


def _norm_drawdown(dd: float) -> float:
    """Drawdown é uma penalidade: dd=0 → 1.0, dd=0.35 → 0.0. Linear."""
    if dd >= CONST.MAX_HISTORICAL_DRAWDOWN:
        return 0.0
    return _clamp01(1.0 - (dd / CONST.MAX_HISTORICAL_DRAWDOWN))


def _norm_diversification(categories_count: int) -> float:
    """2 categorias → 0.5, 4+ categorias → 1.0."""
    if categories_count < CONST.MIN_CATEGORIES:
        return 0.0
    return _clamp01((categories_count - CONST.MIN_CATEGORIES + 1) / 3.0)
