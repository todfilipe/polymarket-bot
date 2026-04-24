"""Prevenção de trades duplicadas (§9.2).

Hash único por trade: `wallet_address + market_id + side + outcome + minuto_timestamp`.
Antes de executar qualquer ordem, verificar se o hash já existe na base de dados
dentro da janela de `DEDUP_WINDOW_MINUTES`. Se sim, ignorar como duplicado.

O hash é determinístico (sha256 truncado) para facilitar debugging nos logs.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket_bot.config.constants import CONST
from polymarket_bot.db.enums import TradeSide
from polymarket_bot.db.models import DedupHash


def compute_dedup_hash(
    wallet_address: str,
    market_id: str,
    side: TradeSide,
    outcome: str,
    timestamp: datetime | None = None,
) -> str:
    """Gera hash determinístico truncado a minuto.

    Duas chamadas no mesmo minuto com mesmos parâmetros → mesmo hash.
    Chamadas espaçadas > 1 minuto → hashes diferentes (permite re-entry
    legítima após a janela de dedup).
    """
    ts = timestamp or datetime.now(timezone.utc)
    minute_bucket = ts.replace(second=0, microsecond=0).isoformat()
    payload = f"{wallet_address.lower()}|{market_id}|{side.value}|{outcome.upper()}|{minute_bucket}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


async def is_duplicate(
    session: AsyncSession,
    hash_key: str,
    window_minutes: int = CONST.DEDUP_WINDOW_MINUTES,
) -> bool:
    """True se `hash_key` foi registado dentro da janela."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    stmt = select(DedupHash).where(
        DedupHash.hash_key == hash_key,
        DedupHash.created_at >= cutoff,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def register_hash(session: AsyncSession, hash_key: str) -> None:
    """Persiste o hash. `hash_key` é PK — conflitos levantam IntegrityError."""
    session.add(DedupHash(hash_key=hash_key))
    await session.flush()
