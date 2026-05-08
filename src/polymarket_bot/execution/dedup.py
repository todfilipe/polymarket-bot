"""Prevenção de trades duplicadas (§9.2).

Hash único por trade baseado no ``tx_hash`` da transacção on-chain. Cada fill
on-chain tem um `tx_hash` único na blockchain, pelo que esta é a granularidade
ideal: o mesmo fill nunca executa duas vezes (idempotência), mas dois fills
distintos da mesma wallet no mesmo mercado (e.g., momentum adds) ficam com
hashes diferentes e são processados separadamente.

Antes era usado um bucket de minuto que conflua momentum-adds legítimos com
duplicações. Mudámos para tx_hash em alinhamento com o modo "copytrade puro"
— seguir tudo o que as wallets fazem on-chain.

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
    tx_hash: str,
) -> str:
    """Gera hash determinístico baseado no ``tx_hash`` on-chain.

    Mesma `tx_hash` (mesmo fill) → mesmo hash → bloqueio de re-execução.
    Diferentes `tx_hash` → hashes diferentes → permite momentum adds.
    """
    payload = (
        f"{wallet_address.lower()}|{market_id}|{side.value}|"
        f"{outcome.upper()}|{tx_hash.lower()}"
    )
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
