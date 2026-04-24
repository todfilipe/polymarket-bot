"""Cálculo puro das métricas usadas pelo scoring (§2.2, §2.3, §2.4).

Funções sem side-effects sobre uma lista de `RawTrade` agrupada por wallet.
Testáveis isoladamente com fixtures sintéticas.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from polymarket_bot.api.polymarket_data import RawTrade
from polymarket_bot.config.constants import CONST


@dataclass(frozen=True)
class WalletMetrics:
    """Métricas brutas de uma wallet numa janela temporal."""

    wallet_address: str
    total_trades: int
    total_volume_usd: Decimal
    total_profit_usd: Decimal
    total_loss_usd: Decimal  # valor positivo
    profit_factor: float
    win_rate: float
    max_drawdown: float
    consistency_pct: float
    categories: set[str]
    recency_score: float
    luck_concentration: float
    last_trade_at: datetime | None


def compute_metrics(
    wallet_address: str,
    trades: Iterable[RawTrade],
    window_weeks: int = CONST.SCORING_WINDOW_WEEKS,
    now: datetime | None = None,
) -> WalletMetrics:
    """Computa as métricas de scoring numa janela de `window_weeks` semanas.

    Só considera trades com `realized_pnl_usd` definido para cálculo de P&L.
    Trades sem P&L (posições ainda abertas) contam para volume/recência apenas.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=window_weeks)
    trades_list = [t for t in trades if t.executed_at >= cutoff]

    total_trades = len(trades_list)
    total_volume = sum((t.usd_value for t in trades_list), Decimal("0"))
    realized = [t for t in trades_list if t.realized_pnl_usd is not None]

    wins_pnl = [t.realized_pnl_usd for t in realized if t.realized_pnl_usd > 0]  # type: ignore[operator]
    losses_pnl = [-t.realized_pnl_usd for t in realized if t.realized_pnl_usd < 0]  # type: ignore[operator]
    total_profit = sum(wins_pnl, Decimal("0"))
    total_loss = sum(losses_pnl, Decimal("0"))

    profit_factor = _safe_div(float(total_profit), float(total_loss))
    win_rate = (len(wins_pnl) / len(realized)) if realized else 0.0

    max_drawdown = _compute_max_drawdown(realized)
    consistency_pct = _compute_consistency(realized, now=now, window_weeks=window_weeks)
    categories = {t.category for t in trades_list if t.category}
    recency_score = _compute_recency_score(realized, now=now)
    luck_concentration = _compute_luck_concentration(wins_pnl, total_profit)

    return WalletMetrics(
        wallet_address=wallet_address.lower(),
        total_trades=total_trades,
        total_volume_usd=total_volume,
        total_profit_usd=total_profit,
        total_loss_usd=total_loss,
        profit_factor=profit_factor,
        win_rate=win_rate,
        max_drawdown=max_drawdown,
        consistency_pct=consistency_pct,
        categories=categories,
        recency_score=recency_score,
        luck_concentration=luck_concentration,
        last_trade_at=max((t.executed_at for t in trades_list), default=None),
    )


def _safe_div(num: float, den: float) -> float:
    if den == 0:
        # Sem perdas e com lucro → profit factor "infinito", capamos num valor grande
        return 999.0 if num > 0 else 0.0
    return num / den


def _compute_max_drawdown(realized: list[RawTrade]) -> float:
    """Max drawdown sobre equity curve cumulativa dos P&Ls realizados.

    Expresso como fração positiva (0.35 = -35%).
    """
    if not realized:
        return 0.0
    sorted_trades = sorted(realized, key=lambda t: t.executed_at)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted_trades:
        equity += float(t.realized_pnl_usd or 0)
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        # Normalizar sobre o pico — se peak ≤ 0, usamos o equity como base
        base = max(abs(peak), 1.0)
        dd_ratio = drawdown / base
        if dd_ratio > max_dd:
            max_dd = dd_ratio
    return max_dd


def _compute_consistency(
    realized: list[RawTrade], now: datetime, window_weeks: int
) -> float:
    """% de semanas com P&L positivo na janela."""
    if not realized or window_weeks <= 0:
        return 0.0

    week_pnl: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for t in realized:
        week_index = _week_index(t.executed_at, now, window_weeks)
        if 0 <= week_index < window_weeks:
            week_pnl[week_index] += t.realized_pnl_usd or Decimal("0")

    if not week_pnl:
        return 0.0
    positive_weeks = sum(1 for pnl in week_pnl.values() if pnl > 0)
    return positive_weeks / len(week_pnl)


def _compute_recency_score(realized: list[RawTrade], now: datetime) -> float:
    """Performance nas últimas 4 semanas pesadas mais do que histórico antigo.

    Devolve 0.0–1.0. Usamos uma média ponderada linear com mais peso nas
    semanas mais recentes (semana 0 = peso 4, semana 3 = peso 1).
    """
    if not realized:
        return 0.0
    weeks = CONST.RECENCY_WINDOW_WEEKS
    week_pnl: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for t in realized:
        week_index = _week_index(t.executed_at, now, weeks)
        if 0 <= week_index < weeks:
            week_pnl[week_index] += t.realized_pnl_usd or Decimal("0")

    if not week_pnl:
        return 0.0
    weights = list(range(weeks, 0, -1))  # [4, 3, 2, 1] para weeks=4
    total_weight = sum(weights)
    weighted_sum = 0.0
    total_abs = 0.0
    for week_idx, pnl in week_pnl.items():
        weighted_sum += float(pnl) * weights[week_idx]
        total_abs += abs(float(pnl)) * weights[week_idx]
    if total_abs == 0:
        return 0.0
    # Normaliza para [0, 1]: (score + 1) / 2
    raw = weighted_sum / total_abs
    return max(0.0, min(1.0, (raw + 1) / 2))


def _compute_luck_concentration(
    wins_pnl: list[Decimal], total_profit: Decimal
) -> float:
    """Fração do lucro total que vem dos 2 maiores trades vencedores.

    Se > `MAX_LUCK_CONCENTRATION` → candidato a sorte, não skill.
    """
    if total_profit <= 0 or not wins_pnl:
        return 0.0
    top2 = sum(sorted(wins_pnl, reverse=True)[:2], Decimal("0"))
    return float(top2 / total_profit)


def _week_index(trade_time: datetime, now: datetime, window_weeks: int) -> int:
    """0 = semana mais recente, `window_weeks-1` = mais antiga dentro da janela."""
    delta_days = (now - trade_time).days
    return delta_days // 7
