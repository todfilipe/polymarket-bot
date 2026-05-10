"""Testes do NoiseFilter — DB in-memory com WalletTrade fixtures.

Cobre:
- Histórico < MIN_HISTORY → deixa passar (não bloqueia wallets recém-elegíveis)
- Trade < 20% × mediana → bloqueia (ruído)
- Trade ≥ 20% × mediana → passa (legítimo)
- Mediana resistente a outliers (whale com 1 trade enorme não inflaciona threshold)
- Cache + invalidate
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from polymarket_bot.config.constants import CONST
from polymarket_bot.db.enums import TradeSide
from polymarket_bot.db.models import Base, WalletTrade
from polymarket_bot.monitoring.noise_filter import NoiseFilter


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


_seed_counter = {"n": 0}


async def _seed_trades(
    sessionmaker,
    *,
    wallet_address: str,
    sizes_usd: list[float],
    days_ago: int = 1,
) -> None:
    """Insere N trades para uma wallet com tamanhos especificados.

    Os tx_hash incluem um contador global incremental para evitar colisões
    quando o mesmo teste chama `_seed_trades` várias vezes.
    """
    now = datetime.now(timezone.utc) - timedelta(days=days_ago)
    async with sessionmaker() as s:
        for size in sizes_usd:
            _seed_counter["n"] += 1
            n = _seed_counter["n"]
            s.add(
                WalletTrade(
                    wallet_address=wallet_address.lower(),
                    tx_hash=f"tx_{wallet_address}_{n}",
                    market_id=f"0xmarket_{n}",
                    side=TradeSide.BUY,
                    outcome="YES",
                    price=Decimal("0.50"),
                    size=Decimal(str(size * 2)),  # shares
                    usd_value=Decimal(str(size)),
                    executed_at=now + timedelta(minutes=n),
                )
            )
        await s.commit()


@pytest.mark.asyncio
async def test_lets_through_when_history_below_minimum(sessionmaker):
    """Wallet com 5 trades — abaixo de NOISE_FILTER_MIN_HISTORY (20) → passa."""
    await _seed_trades(
        sessionmaker, wallet_address="0xnew", sizes_usd=[10, 10, 10, 10, 10]
    )
    nf = NoiseFilter(session_factory=sessionmaker)

    decision = await nf.evaluate("0xnew", trade_usd=Decimal("0.50"))

    assert decision.passes is True
    assert "histórico insuficiente" in (decision.reason or "")


@pytest.mark.asyncio
async def test_blocks_when_trade_below_20pct_of_median(sessionmaker):
    """Mediana = $80 → threshold = $16. Trade de $5 → bloqueia."""
    sizes = [80.0] * 25  # 25 trades de $80 cada → mediana $80
    await _seed_trades(sessionmaker, wallet_address="0xhumble", sizes_usd=sizes)

    nf = NoiseFilter(session_factory=sessionmaker)
    decision = await nf.evaluate("0xhumble", trade_usd=Decimal("5"))

    assert decision.passes is False
    assert decision.median_usd == Decimal("80")
    # threshold = 0.20 × 80 = 16
    assert decision.threshold_usd == Decimal("16.00")
    assert decision.sample_size == 25


@pytest.mark.asyncio
async def test_allows_trade_above_threshold(sessionmaker):
    """Mediana = $80 → threshold = $16. Trade de $20 → passa."""
    sizes = [80.0] * 25
    await _seed_trades(sessionmaker, wallet_address="0xhumble", sizes_usd=sizes)

    nf = NoiseFilter(session_factory=sessionmaker)
    decision = await nf.evaluate("0xhumble", trade_usd=Decimal("20"))

    assert decision.passes is True


@pytest.mark.asyncio
async def test_median_resistant_to_outliers(sessionmaker):
    """24 trades de $80 + 1 outlier de $5000 → mediana ainda é $80
    (média seria ~$277). Threshold continua $16, não $55."""
    sizes = [80.0] * 24 + [5000.0]
    await _seed_trades(sessionmaker, wallet_address="0xwhale_test", sizes_usd=sizes)

    nf = NoiseFilter(session_factory=sessionmaker)
    decision = await nf.evaluate("0xwhale_test", trade_usd=Decimal("20"))

    assert decision.passes is True
    assert decision.median_usd == Decimal("80")  # não inflado pelo outlier


@pytest.mark.asyncio
async def test_whale_threshold_scales_with_median(sessionmaker):
    """Whale com mediana $5000 → threshold $1000. Trade de $500 → bloqueia."""
    sizes = [5000.0] * 25
    await _seed_trades(sessionmaker, wallet_address="0xwhale", sizes_usd=sizes)

    nf = NoiseFilter(session_factory=sessionmaker)

    # Trade pequeno para o padrão dela
    decision_small = await nf.evaluate("0xwhale", trade_usd=Decimal("500"))
    assert decision_small.passes is False
    assert decision_small.threshold_usd == Decimal("1000.00")

    # Trade normal para o padrão dela
    decision_normal = await nf.evaluate("0xwhale", trade_usd=Decimal("2000"))
    assert decision_normal.passes is True


@pytest.mark.asyncio
async def test_invalidate_forces_recompute(sessionmaker):
    """`invalidate()` força recálculo da mediana — útil quando signal_reader
    persiste novos trades em real-time."""
    sizes = [80.0] * 25
    await _seed_trades(sessionmaker, wallet_address="0xrt", sizes_usd=sizes)

    nf = NoiseFilter(session_factory=sessionmaker)
    d1 = await nf.evaluate("0xrt", trade_usd=Decimal("20"))
    assert d1.median_usd == Decimal("80")

    # Persistir mais 25 trades de $200 (mais recentes) — total 50, mediana sobe
    await _seed_trades(
        sessionmaker, wallet_address="0xrt", sizes_usd=[200.0] * 25, days_ago=0
    )
    # Sem invalidate, retorna o cached
    d_cached = await nf.evaluate("0xrt", trade_usd=Decimal("20"))
    assert d_cached.median_usd == Decimal("80")  # cached

    # Após invalidate, recalcula
    await nf.invalidate("0xrt")
    d_fresh = await nf.evaluate("0xrt", trade_usd=Decimal("20"))
    # 25× $80 + 25× $200 → mediana = $140
    assert d_fresh.median_usd == Decimal("140")


@pytest.mark.asyncio
async def test_old_trades_outside_window_excluded(sessionmaker):
    """Trades fora da janela (MEDIAN_WINDOW_WEEKS = 4 semanas) não contam."""
    # 25 trades de $80, mas há 5 semanas (fora da janela)
    await _seed_trades(
        sessionmaker,
        wallet_address="0xold",
        sizes_usd=[80.0] * 25,
        days_ago=CONST.MEDIAN_WINDOW_WEEKS * 7 + 5,
    )
    nf = NoiseFilter(session_factory=sessionmaker)

    decision = await nf.evaluate("0xold", trade_usd=Decimal("5"))
    # Sem trades dentro da janela → passa por histórico insuficiente
    assert decision.passes is True
    assert decision.sample_size == 0


@pytest.mark.asyncio
async def test_zero_or_negative_trade_handled_gracefully(sessionmaker):
    """trade_usd <= 0 não é avaliado pelo filtro (pipeline-level handling)."""
    sizes = [80.0] * 25
    await _seed_trades(sessionmaker, wallet_address="0xz", sizes_usd=sizes)
    nf = NoiseFilter(session_factory=sessionmaker)

    # O filtro avalia mesmo zero — é o pipeline que decide deixar passar.
    # Aqui validamos só que não lança.
    decision = await nf.evaluate("0xz", trade_usd=Decimal("0"))
    assert decision.passes is False  # 0 < threshold $16
