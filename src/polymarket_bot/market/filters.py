"""Filtros obrigatórios de mercado (§3.1) + validação de order book depth.

Uso:
    from polymarket_bot.market import check_market_filters, check_orderbook_depth

Todos os filtros são aplicados em conjunção — uma falha reprova o mercado.
As razões são retornadas para log/Telegram (§12 — "trade ignorada com motivo").
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polymarket_bot.config.constants import CONST
from polymarket_bot.market.models import MarketSnapshot, OrderBook


@dataclass(frozen=True)
class MarketFilterResult:
    passes: bool
    is_ideal_zone: bool
    reasons: list[str]


def check_market_filters(snapshot: MarketSnapshot) -> MarketFilterResult:
    """Aplica os 4 filtros hard (§3.1) + classifica como zona ideal.

    Nota: order book depth é avaliado em separado via `check_orderbook_depth`,
    porque depende do stake que se tenciona enviar.
    """
    reasons: list[str] = []

    # --- Volume ---
    if snapshot.volume_usd < Decimal(str(CONST.MIN_MARKET_VOLUME_USD)):
        reasons.append(
            f"volume ${snapshot.volume_usd} < mín ${CONST.MIN_MARKET_VOLUME_USD:.0f}"
        )

    # --- Tempo até resolução ---
    hours = snapshot.hours_to_resolution
    days = snapshot.days_to_resolution
    if hours < CONST.MIN_HOURS_TO_RESOLUTION:
        reasons.append(
            f"tempo {hours:.1f}h < mín {CONST.MIN_HOURS_TO_RESOLUTION}h"
        )
    if days > CONST.MAX_DAYS_TO_RESOLUTION:
        reasons.append(
            f"tempo {days:.1f}d > máx {CONST.MAX_DAYS_TO_RESOLUTION}d"
        )

    # --- Probabilidade implícita ---
    prob = snapshot.implied_probability
    if prob is None:
        reasons.append("order book sem bid/ask — probabilidade indefinida")
    else:
        if prob < Decimal(str(CONST.MIN_IMPLIED_PROB)):
            reasons.append(
                f"prob {float(prob):.1%} < mín {CONST.MIN_IMPLIED_PROB:.0%}"
            )
        if prob > Decimal(str(CONST.MAX_IMPLIED_PROB)):
            reasons.append(
                f"prob {float(prob):.1%} > máx {CONST.MAX_IMPLIED_PROB:.0%}"
            )

    # --- Zona ideal (bónus, não bloqueia) ---
    is_ideal = (
        not reasons
        and prob is not None
        and Decimal(str(CONST.IDEAL_PROB_MIN)) <= prob <= Decimal(str(CONST.IDEAL_PROB_MAX))
        and CONST.IDEAL_DAYS_MIN <= days <= CONST.IDEAL_DAYS_MAX
        and snapshot.volume_usd >= Decimal(str(CONST.IDEAL_MARKET_VOLUME_USD))
    )

    return MarketFilterResult(passes=not reasons, is_ideal_zone=is_ideal, reasons=reasons)


@dataclass(frozen=True)
class OrderBookDepthResult:
    """Resultado da análise de profundidade para um stake específico."""

    fillable: bool
    fillable_usd: Decimal            # quanto seria efetivamente comprado
    avg_fill_price: Decimal | None
    price_impact: float              # fração: 0.02 = 2%
    reason: str | None


def check_orderbook_depth(
    orderbook: OrderBook,
    stake_usd: Decimal,
    side: str = "BUY",
) -> OrderBookDepthResult:
    """Verifica se conseguimos preencher `stake_usd` sem mover o preço > 2%.

    Regra: `CONST.MAX_ORDERBOOK_IMPACT` (2%) — §3.1 ("fill sem mover > 2%").
    Percorre o lado relevante (asks para BUY, bids para SELL) até acumular
    o stake. Calcula preço médio ponderado e compara com o melhor nível.
    """
    if side.upper() == "BUY":
        levels = orderbook.asks
        reference = orderbook.best_ask
    else:
        levels = orderbook.bids
        reference = orderbook.best_bid

    if not levels or reference is None:
        return OrderBookDepthResult(
            fillable=False,
            fillable_usd=Decimal("0"),
            avg_fill_price=None,
            price_impact=0.0,
            reason="order book vazio do lado pretendido",
        )

    remaining = stake_usd
    spent_usd = Decimal("0")
    acquired_size = Decimal("0")

    for level in levels:
        level_usd_capacity = level.price * level.size
        take_usd = min(remaining, level_usd_capacity)
        if take_usd <= 0:
            break
        take_size = take_usd / level.price
        spent_usd += take_usd
        acquired_size += take_size
        remaining -= take_usd
        if remaining <= 0:
            break

    if acquired_size == 0:
        return OrderBookDepthResult(
            fillable=False,
            fillable_usd=Decimal("0"),
            avg_fill_price=None,
            price_impact=0.0,
            reason="nenhum nível do book para preencher",
        )

    avg_price = spent_usd / acquired_size
    impact = abs(float((avg_price - reference) / reference))

    if remaining > 0:
        return OrderBookDepthResult(
            fillable=False,
            fillable_usd=spent_usd,
            avg_fill_price=avg_price,
            price_impact=impact,
            reason=f"book insuficiente: só preenche ${spent_usd:.2f} de ${stake_usd:.2f}",
        )

    if impact > CONST.MAX_ORDERBOOK_IMPACT:
        return OrderBookDepthResult(
            fillable=False,
            fillable_usd=spent_usd,
            avg_fill_price=avg_price,
            price_impact=impact,
            reason=(
                f"impacto {impact:.2%} > máx {CONST.MAX_ORDERBOOK_IMPACT:.0%}"
            ),
        )

    return OrderBookDepthResult(
        fillable=True,
        fillable_usd=spent_usd,
        avg_fill_price=avg_price,
        price_impact=impact,
        reason=None,
    )
