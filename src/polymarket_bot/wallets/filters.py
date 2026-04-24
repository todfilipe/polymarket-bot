"""Filtros obrigatórios de elegibilidade (§2.3).

Uma wallet é excluída se falhar QUALQUER filtro, independentemente do score.
As razões de exclusão são registadas para auditoria semanal via Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from polymarket_bot.config.constants import CONST
from polymarket_bot.wallets.metrics import WalletMetrics


@dataclass(frozen=True)
class EligibilityResult:
    is_eligible: bool
    reasons: list[str]  # vazio se elegível


def check_eligibility(
    metrics: WalletMetrics, now: datetime | None = None
) -> EligibilityResult:
    """Aplica os 6 filtros obrigatórios + deteção sorte/skill."""
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []

    if metrics.total_trades < CONST.MIN_TRADES_HISTORY:
        reasons.append(
            f"trades insuficientes ({metrics.total_trades} < {CONST.MIN_TRADES_HISTORY})"
        )

    if metrics.last_trade_at is None:
        reasons.append("sem trades na janela")
    else:
        recency_cutoff = now - timedelta(weeks=CONST.MIN_ACTIVITY_WEEKS)
        if metrics.last_trade_at < recency_cutoff:
            reasons.append(
                f"inativa há mais de {CONST.MIN_ACTIVITY_WEEKS} semanas"
            )

    if metrics.max_drawdown > CONST.MAX_HISTORICAL_DRAWDOWN:
        reasons.append(
            f"drawdown {metrics.max_drawdown:.1%} > máx {CONST.MAX_HISTORICAL_DRAWDOWN:.0%}"
        )

    if float(metrics.total_volume_usd) < CONST.MIN_VOLUME_USD:
        reasons.append(f"volume ${metrics.total_volume_usd} < mín ${CONST.MIN_VOLUME_USD}")

    if metrics.profit_factor < CONST.MIN_PROFIT_FACTOR:
        reasons.append(
            f"profit factor {metrics.profit_factor:.2f} < mín {CONST.MIN_PROFIT_FACTOR}"
        )

    if len(metrics.categories) < CONST.MIN_CATEGORIES:
        reasons.append(
            f"categorias {len(metrics.categories)} < mín {CONST.MIN_CATEGORIES}"
        )

    if metrics.win_rate < CONST.MIN_WIN_RATE:
        reasons.append(
            f"win rate {metrics.win_rate:.1%} < mín {CONST.MIN_WIN_RATE:.0%}"
        )

    if metrics.luck_concentration > CONST.MAX_LUCK_CONCENTRATION:
        reasons.append(
            f"sorte: {metrics.luck_concentration:.1%} do lucro em 2 trades "
            f"(máx {CONST.MAX_LUCK_CONCENTRATION:.0%})"
        )

    return EligibilityResult(is_eligible=not reasons, reasons=reasons)
