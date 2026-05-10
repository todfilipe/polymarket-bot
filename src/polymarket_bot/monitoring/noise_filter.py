"""Filtro de ruído baseado na mediana de tamanhos por wallet.

Filosofia: cada wallet tem o seu padrão típico de tamanho de trade. Trades
muito abaixo desse padrão são ruído (testes de liquidez, erros de quantidade,
rebalanceamentos marginais). Copiá-los inflaciona ruído sem ganhar alpha.

Solução: para cada wallet, calcular a **mediana** dos tamanhos das últimas
``CONST.MEDIAN_WINDOW_WEEKS`` semanas. Trades < ``CONST.NOISE_FILTER_RATIO``
× mediana são considerados ruído e skippados.

A mediana (não a média) é usada porque é resistente a outliers — uma wallet
com 1 trade outlier de $5k não tem o threshold inflacionado a ponto de
descartar os seus trades normais de $80.

O filtro só se aplica quando há ``CONST.NOISE_FILTER_MIN_HISTORY`` trades
no histórico — caso contrário não há sample suficiente para mediana fiável,
e o filtro deixa passar (não bloqueia wallets recém-elegíveis).

Cache in-memory por wallet para evitar SELECT por cada signal — invalidação
implícita por TTL curto, e refresh explícito quando o `signal_reader`
persiste novo trade.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_bot.config.constants import CONST
from polymarket_bot.db.models import WalletTrade


@dataclass(frozen=True)
class NoiseFilterDecision:
    """Resultado do filtro — explícito sobre porquê passou/falhou."""

    passes: bool
    reason: str | None
    median_usd: Decimal | None       # None se histórico insuficiente
    threshold_usd: Decimal | None    # None se histórico insuficiente
    sample_size: int


class NoiseFilter:
    """Filtro de ruído por wallet com cache de mediana (TTL curto)."""

    # TTL pequeno: o `signal_reader` já invalida explicitamente quando
    # persiste um novo trade da wallet, mas o TTL é safety-net contra
    # caches stale em casos edge (ex: trades inseridos por scripts).
    _CACHE_TTL_SECONDS = 60

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._sessionmaker = session_factory
        self._cache: dict[str, tuple[datetime, Decimal | None, int]] = {}
        self._lock = asyncio.Lock()

    async def evaluate(
        self,
        wallet_address: str,
        trade_usd: Decimal,
    ) -> NoiseFilterDecision:
        """Avalia se ``trade_usd`` da ``wallet_address`` é ruído.

        Devolve sempre uma decisão (nunca lança). Em caso de erro de DB,
        deixa passar — preferimos ruído ocasional a perder trades por bug.
        """
        try:
            median_usd, sample = await self._get_median(wallet_address)
        except Exception:
            return NoiseFilterDecision(
                passes=True,
                reason="filtro indisponível (erro DB) — let through",
                median_usd=None,
                threshold_usd=None,
                sample_size=0,
            )

        # Histórico insuficiente — deixa passar (wallet recém-elegível, etc.)
        if median_usd is None or sample < CONST.NOISE_FILTER_MIN_HISTORY:
            return NoiseFilterDecision(
                passes=True,
                reason=f"histórico insuficiente ({sample} < {CONST.NOISE_FILTER_MIN_HISTORY}) — let through",
                median_usd=median_usd,
                threshold_usd=None,
                sample_size=sample,
            )

        threshold = (median_usd * Decimal(str(CONST.NOISE_FILTER_RATIO))).quantize(
            Decimal("0.01")
        )

        if trade_usd < threshold:
            return NoiseFilterDecision(
                passes=False,
                reason=(
                    f"trade ${float(trade_usd):.2f} < threshold "
                    f"${float(threshold):.2f} (= {CONST.NOISE_FILTER_RATIO:.0%} × "
                    f"mediana ${float(median_usd):.2f}, n={sample})"
                ),
                median_usd=median_usd,
                threshold_usd=threshold,
                sample_size=sample,
            )

        return NoiseFilterDecision(
            passes=True,
            reason=None,
            median_usd=median_usd,
            threshold_usd=threshold,
            sample_size=sample,
        )

    async def invalidate(self, wallet_address: str) -> None:
        """Força recálculo da mediana na próxima avaliação.

        Chamado pelo `signal_reader` quando persiste um novo trade — mantém
        a mediana sempre fresh sem ter de esperar pelo TTL.
        """
        async with self._lock:
            self._cache.pop(wallet_address.lower(), None)

    async def _get_median(
        self, wallet_address: str
    ) -> tuple[Decimal | None, int]:
        """Mediana cacheada (TTL=60s)."""
        key = wallet_address.lower()
        now = datetime.now(timezone.utc)

        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                cached_at, med, sample = cached
                if (now - cached_at).total_seconds() < self._CACHE_TTL_SECONDS:
                    return med, sample

        # Compute fresh fora do lock para não bloquear outros chamadores
        med, sample = await self._compute_median(key)

        async with self._lock:
            self._cache[key] = (now, med, sample)
        return med, sample

    async def _compute_median(
        self, wallet_address_lc: str
    ) -> tuple[Decimal | None, int]:
        """Calcula mediana das últimas N semanas de trades da wallet."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            weeks=CONST.MEDIAN_WINDOW_WEEKS
        )
        async with self._sessionmaker() as session:
            stmt = (
                select(WalletTrade.usd_value)
                .where(WalletTrade.wallet_address.ilike(wallet_address_lc))
                .where(WalletTrade.executed_at >= cutoff)
            )
            rows = list((await session.execute(stmt)).scalars().all())

        if not rows:
            return None, 0
        # Filtra valores não positivos (defensivo — schema permite mas não
        # devia acontecer); converte tudo a Decimal para median consistente.
        values = [v for v in rows if v is not None and v > 0]
        if not values:
            return None, 0
        med = median(values)
        return Decimal(med), len(values)
