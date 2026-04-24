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

from polymarket_bot.api.polymarket_data import RawPosition, RawTrade
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
    positions: Iterable[RawPosition] | None = None,
) -> WalletMetrics:
    """Computa as métricas de scoring numa janela de `window_weeks` semanas.

    Fontes de dados:
    - ``trades``  → volume, total de trades, recência, categorias, last_trade_at.
    - ``positions`` (opcional, produção) → P&L realizado por posição, que
      alimenta win_rate/profit_factor/drawdown/consistency/luck_concentration.

    Quando ``positions`` é ``None`` (testes com fixtures sintéticas), caímos
    de volta para o P&L per-trade via ``RawTrade.realized_pnl_usd``.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=window_weeks)
    trades_list = [t for t in trades if t.executed_at >= cutoff]

    total_trades = len(trades_list)
    total_volume = sum((t.usd_value for t in trades_list), Decimal("0"))

    if positions is not None:
        wins_pnl, losses_pnl = _pnl_from_positions(positions, cutoff=cutoff, now=now)
        # Para drawdown/consistency/recency precisamos duma série temporal —
        # ordenamos posições resolvidas pela end_date.
        pnl_series = _pnl_series_from_positions(
            positions, cutoff=cutoff, now=now
        )
        categories = {p.category for p in positions if p.category}
        categories |= {t.category for t in trades_list if t.category}
    else:
        realized = [t for t in trades_list if t.realized_pnl_usd is not None]
        wins_pnl = [t.realized_pnl_usd for t in realized if t.realized_pnl_usd > 0]  # type: ignore[operator]
        losses_pnl = [
            -t.realized_pnl_usd for t in realized if t.realized_pnl_usd < 0  # type: ignore[operator]
        ]
        pnl_series = [
            (t.executed_at, t.realized_pnl_usd) for t in realized
        ]
        categories = {t.category for t in trades_list if t.category}

    total_profit = sum(wins_pnl, Decimal("0"))
    total_loss = sum(losses_pnl, Decimal("0"))
    resolved_count = len(wins_pnl) + len(losses_pnl)

    profit_factor = _safe_div(float(total_profit), float(total_loss))
    win_rate = (len(wins_pnl) / resolved_count) if resolved_count else 0.0
    max_drawdown = _compute_max_drawdown_from_series(pnl_series)
    consistency_pct = _compute_consistency_from_series(
        pnl_series, now=now, window_weeks=window_weeks
    )
    recency_score = _compute_recency_score_from_series(pnl_series, now=now)
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


def _pnl_from_positions(
    positions: Iterable[RawPosition], cutoff: datetime, now: datetime
) -> tuple[list[Decimal], list[Decimal]]:
    """Agrega P&L das posições resolvidas dentro da janela. Open → ignorar."""
    wins: list[Decimal] = []
    losses: list[Decimal] = []
    for p in positions:
        if not p.is_resolved:
            continue
        # Se temos end_date, filtramos pela janela; se não, incluímos (o leaderboard
        # já restringe a wallets activas no período).
        if p.end_date is not None and p.end_date < cutoff:
            continue
        pnl = p.effective_pnl_usd
        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(-pnl)
    return wins, losses


def _pnl_series_from_positions(
    positions: Iterable[RawPosition], cutoff: datetime, now: datetime
) -> list[tuple[datetime, Decimal]]:
    """Série (timestamp, pnl) para drawdown/consistência/recência."""
    series: list[tuple[datetime, Decimal]] = []
    for p in positions:
        if not p.is_resolved:
            continue
        if p.end_date is not None and p.end_date < cutoff:
            continue
        pnl = p.effective_pnl_usd
        if pnl == 0:
            continue
        ts = p.end_date or now
        series.append((ts, pnl))
    return series


def _safe_div(num: float, den: float) -> float:
    if den == 0:
        # Sem perdas e com lucro → profit factor "infinito", capamos num valor grande
        return 999.0 if num > 0 else 0.0
    return num / den


def _compute_max_drawdown_from_series(
    series: list[tuple[datetime, Decimal]],
) -> float:
    """Max drawdown sobre equity curve cumulativa dos P&Ls realizados.

    Expresso como fração positiva (0.35 = -35%).
    """
    if not series:
        return 0.0
    sorted_series = sorted(series, key=lambda item: item[0])
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    total_loss = sum(float(pnl) for _, pnl in sorted_series if pnl < 0)
    # Base de normalização: pico realizado ou, se nunca houve pico (wallet só
    # com perdas no período), o total bruto de perdas. Evita drawdowns absurdos
    # quando peak ≈ 0 e o equity vai fundo no negativo.
    fallback_base = max(abs(total_loss), 1.0)
    for _, pnl in sorted_series:
        equity += float(pnl or 0)
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        base = max(abs(peak), fallback_base)
        dd_ratio = drawdown / base
        if dd_ratio > max_dd:
            max_dd = dd_ratio
    return min(max_dd, 1.0)


def _compute_consistency_from_series(
    series: list[tuple[datetime, Decimal]],
    now: datetime,
    window_weeks: int,
) -> float:
    """% de semanas com P&L positivo na janela."""
    if not series or window_weeks <= 0:
        return 0.0

    week_pnl: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for ts, pnl in series:
        week_index = _week_index(ts, now, window_weeks)
        if 0 <= week_index < window_weeks:
            week_pnl[week_index] += pnl or Decimal("0")

    if not week_pnl:
        return 0.0
    positive_weeks = sum(1 for pnl in week_pnl.values() if pnl > 0)
    return positive_weeks / len(week_pnl)


def _compute_recency_score_from_series(
    series: list[tuple[datetime, Decimal]], now: datetime
) -> float:
    """Performance nas últimas 4 semanas pesadas mais do que histórico antigo.

    Devolve 0.0–1.0. Média ponderada linear com mais peso nas semanas mais
    recentes (semana 0 = peso 4, semana 3 = peso 1).
    """
    if not series:
        return 0.0
    weeks = CONST.RECENCY_WINDOW_WEEKS
    week_pnl: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for ts, pnl in series:
        week_index = _week_index(ts, now, weeks)
        if 0 <= week_index < weeks:
            week_pnl[week_index] += pnl or Decimal("0")

    if not week_pnl:
        return 0.0
    weights = list(range(weeks, 0, -1))
    weighted_sum = 0.0
    total_abs = 0.0
    for week_idx, pnl in week_pnl.items():
        weighted_sum += float(pnl) * weights[week_idx]
        total_abs += abs(float(pnl)) * weights[week_idx]
    if total_abs == 0:
        return 0.0
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
