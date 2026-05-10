"""Sessão SQLAlchemy async. Cria o engine singleton a partir das settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polymarket_bot.config import get_settings
from polymarket_bot.db.models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, echo=False, future=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager com commit/rollback automático."""
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """Cria tabelas se não existirem + aplica migrações ad-hoc.

    Para dev — em prod usar Alembic. Migrações idempotentes (verificam se a
    coluna já existe via ``PRAGMA table_info``).
    """
    from sqlalchemy import text  # local — usado só aqui

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Migração: adicionar `sl_anchor_price` à tabela `positions` se ainda
        # não existir. Backfilla com `avg_entry_price` para posições legacy
        # (mantém SL trigger funcional, embora aproximado).
        cols = await conn.execute(text("PRAGMA table_info(positions)"))
        col_names = {row[1] for row in cols.fetchall()}
        if "sl_anchor_price" not in col_names:
            await conn.execute(text(
                "ALTER TABLE positions ADD COLUMN sl_anchor_price NUMERIC(10, 6)"
            ))
            await conn.execute(text(
                "UPDATE positions SET sl_anchor_price = avg_entry_price "
                "WHERE sl_anchor_price IS NULL"
            ))
