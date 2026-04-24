from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from polymarket_bot.config.settings import Settings
from polymarket_bot.db.enums import BotTradeStatus, TradeSide
from polymarket_bot.db.models import Base, BotTrade, DedupHash
from polymarket_bot.execution import order_manager as om_mod
from polymarket_bot.execution.dedup import compute_dedup_hash
from polymarket_bot.execution.order_manager import OrderManager
from tests.fixtures.markets import make_snapshot


# ---- Fixtures ------------------------------------------------------------

@pytest_asyncio.fixture
async def sessionmaker():
    """Engine SQLite in-memory para isolamento entre testes."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


def _make_settings(live_mode: bool = False, with_clob: bool = False) -> Settings:
    """Construir Settings com valores dummy — override explícito para ignorar .env real."""
    # NB: valores `None` passados em init_kwargs têm prioridade sobre os env vars
    # e sobre o .env — garantindo isolamento dos testes.
    return Settings(  # type: ignore[call-arg]
        live_mode=live_mode,
        polygon_private_key="0" * 64,
        telegram_bot_token="test",
        telegram_chat_id="0",
        clob_api_key="key" if with_clob else None,
        clob_api_secret="secret" if with_clob else None,
        clob_api_passphrase="pass" if with_clob else None,
    )


def _submit_kwargs(dedup_hash: str | None = None):
    """Kwargs comuns para OrderManager.submit()."""
    market = make_snapshot()
    return dict(
        market=market,
        outcome="YES",
        side=TradeSide.BUY,
        size_usd=Decimal("80.00"),
        intended_price=Decimal("0.41"),
        expected_value=0.25,
        followed_wallet="0xABC",
        dedup_hash=dedup_hash or compute_dedup_hash(
            wallet_address="0xABC",
            market_id=market.market_id,
            side=TradeSide.BUY,
            outcome="YES",
        ),
    )


# ---- Tests ---------------------------------------------------------------


class TestOrderManagerDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_fills_at_intended_price(self, sessionmaker):
        om = OrderManager(_make_settings(live_mode=False), sessionmaker)
        kwargs = _submit_kwargs()

        result = await om.submit(**kwargs)

        assert result.success
        assert result.is_paper
        assert not result.is_duplicate
        assert result.executed_price == Decimal("0.41")
        assert result.bot_trade_id is not None

    @pytest.mark.asyncio
    async def test_dry_run_persists_bot_trade_and_hash(self, sessionmaker):
        om = OrderManager(_make_settings(live_mode=False), sessionmaker)
        kwargs = _submit_kwargs()

        result = await om.submit(**kwargs)

        async with sessionmaker() as s:
            trades = (await s.execute(select(BotTrade))).scalars().all()
            hashes = (await s.execute(select(DedupHash))).scalars().all()

        assert len(trades) == 1
        trade = trades[0]
        assert trade.is_paper is True
        assert trade.status == BotTradeStatus.FILLED
        assert trade.dedup_hash == kwargs["dedup_hash"]
        assert trade.size_usd == Decimal("80.00")
        assert trade.intended_price == Decimal("0.41")
        assert trade.executed_price == Decimal("0.41")
        assert trade.slippage == 0.0  # paper fill = zero slippage
        assert trade.skip_reason is None
        assert trade.followed_wallet_address == "0xABC"
        assert trade.id == result.bot_trade_id

        assert len(hashes) == 1
        assert hashes[0].hash_key == kwargs["dedup_hash"]

    @pytest.mark.asyncio
    async def test_dedup_prevents_second_submission(self, sessionmaker):
        om = OrderManager(_make_settings(live_mode=False), sessionmaker)
        kwargs = _submit_kwargs()

        first = await om.submit(**kwargs)
        assert first.success

        # Mesmo hash → segunda chamada detecta duplicado.
        second = await om.submit(**kwargs)
        assert not second.success
        assert second.is_duplicate
        assert second.reason is not None

        # DB deve ter apenas um BotTrade + um hash.
        async with sessionmaker() as s:
            n_trades = len((await s.execute(select(BotTrade))).scalars().all())
            n_hashes = len((await s.execute(select(DedupHash))).scalars().all())

        assert n_trades == 1
        assert n_hashes == 1


class TestOrderManagerLiveGuards:
    @pytest.mark.asyncio
    async def test_live_without_clob_creds_fails(self, sessionmaker):
        """LIVE_MODE=true mas sem credenciais → FAILED com motivo claro."""
        om = OrderManager(_make_settings(live_mode=True, with_clob=False), sessionmaker)
        kwargs = _submit_kwargs()

        result = await om.submit(**kwargs)

        assert not result.success
        assert not result.is_paper  # é uma tentativa live
        assert not result.is_duplicate
        assert "credenciais" in (result.reason or "").lower()

        # Deve registar a tentativa falhada em DB (auditoria §12).
        async with sessionmaker() as s:
            trades = (await s.execute(select(BotTrade))).scalars().all()
            hashes = (await s.execute(select(DedupHash))).scalars().all()

        assert len(trades) == 1
        assert trades[0].status == BotTradeStatus.FAILED
        assert trades[0].is_paper is False
        # Hash NÃO é registado em caso de falha — permite retry legítimo.
        assert len(hashes) == 0

    @pytest.mark.asyncio
    async def test_live_with_creds_but_no_clob_client_fails(self, sessionmaker):
        """Credenciais presentes mas clob_client não injectado → FAILED (fail-safe)."""
        om = OrderManager(_make_settings(live_mode=True, with_clob=True), sessionmaker)
        kwargs = _submit_kwargs()

        result = await om.submit(**kwargs)

        assert not result.success
        assert not result.is_paper
        assert "clobclient" in (result.reason or "").lower()

    @pytest.mark.asyncio
    async def test_is_live_property(self, sessionmaker):
        dry = OrderManager(_make_settings(live_mode=False), sessionmaker)
        live = OrderManager(_make_settings(live_mode=True), sessionmaker)
        assert dry.is_live is False
        assert live.is_live is True


# ---- Live submission tests (com ClobClient mockado) -----------------------

@pytest.fixture
def fast_poll(monkeypatch):
    """Polling sem waits reais: `asyncio.sleep(_POLL_STEP_SECONDS)` → no-op."""
    async def _no_sleep(_secs):
        return None

    monkeypatch.setattr(om_mod.asyncio, "sleep", _no_sleep)


def _live_submit_kwargs():
    """Kwargs com `token_id` populado (obrigatório em live)."""
    market = make_snapshot(token_id="tok-123", volume_usd="150000")
    return dict(
        market=market,
        outcome="YES",
        side=TradeSide.BUY,
        size_usd=Decimal("80.00"),
        intended_price=Decimal("0.40"),
        expected_value=0.25,
        followed_wallet="0xABC",
        dedup_hash=compute_dedup_hash(
            wallet_address="0xABC",
            market_id=market.market_id,
            side=TradeSide.BUY,
            outcome="YES",
        ),
    )


class TestOrderManagerLiveSubmit:
    @pytest.mark.asyncio
    async def test_live_submit_success(self, sessionmaker, fast_poll):
        """`create_order` → `post_order` OK → `get_order` FILLED → success."""
        clob = MagicMock()
        clob.create_order = MagicMock(return_value={"signed": True})
        clob.post_order = MagicMock(
            return_value={"success": True, "orderID": "0xORDER123"}
        )
        clob.get_order = MagicMock(
            return_value={"status": "FILLED", "price": "0.40"}
        )
        clob.cancel = MagicMock()

        om = OrderManager(
            _make_settings(live_mode=True, with_clob=True),
            sessionmaker,
            clob_client=clob,
        )
        result = await om.submit(**_live_submit_kwargs())

        assert result.success is True
        assert result.is_paper is False
        assert result.clob_order_id == "0xORDER123"
        assert result.executed_price == Decimal("0.40")
        clob.cancel.assert_not_called()

        async with sessionmaker() as s:
            [trade] = (await s.execute(select(BotTrade))).scalars().all()
            [dhash] = (await s.execute(select(DedupHash))).scalars().all()
        assert trade.status == BotTradeStatus.FILLED
        assert trade.is_paper is False
        assert trade.clob_order_id == "0xORDER123"
        assert trade.executed_price == Decimal("0.40")
        assert dhash.hash_key == trade.dedup_hash

    @pytest.mark.asyncio
    async def test_live_timeout_cancels_order_and_fails(
        self, sessionmaker, fast_poll
    ):
        """`get_order` nunca FILLED → 12 polls → `cancel()` + FAILED."""
        clob = MagicMock()
        clob.create_order = MagicMock(return_value={"signed": True})
        clob.post_order = MagicMock(
            return_value={"success": True, "orderID": "0xORDER_TO"}
        )
        # Sempre OPEN — nunca transiciona para FILLED
        clob.get_order = MagicMock(return_value={"status": "OPEN"})
        clob.cancel = MagicMock()

        om = OrderManager(
            _make_settings(live_mode=True, with_clob=True),
            sessionmaker,
            clob_client=clob,
        )
        result = await om.submit(**_live_submit_kwargs())

        assert result.success is False
        assert "timeout" in (result.reason or "").lower()
        clob.cancel.assert_called_once_with("0xORDER_TO")
        # 12 iterações de polling (60s / 5s)
        assert clob.get_order.call_count == 12

        async with sessionmaker() as s:
            [trade] = (await s.execute(select(BotTrade))).scalars().all()
            hashes = (await s.execute(select(DedupHash))).scalars().all()
        assert trade.status == BotTradeStatus.FAILED
        assert trade.clob_order_id == "0xORDER_TO"
        # Hash NÃO é registado em falha — permite retry legítimo.
        assert hashes == []

    @pytest.mark.asyncio
    async def test_live_slippage_above_limit_logs_warning(
        self, sessionmaker, fast_poll
    ):
        """Fill OK mas executed_price desvia > limite → WARNING no log.

        Com volume=150k (< 500k threshold) o limite é 2%. Submetemos a 0.40 e
        o fill volta a 0.45 — desvio de 12.5% > 2%.
        """
        from loguru import logger as _lg

        captured: list[str] = []
        sink_id = _lg.add(lambda msg: captured.append(msg.record["message"]), level="WARNING")
        try:
            clob = MagicMock()
            clob.create_order = MagicMock(return_value={"signed": True})
            clob.post_order = MagicMock(
                return_value={"success": True, "orderID": "0xORDER_SLIP"}
            )
            clob.get_order = MagicMock(
                return_value={"status": "FILLED", "price": "0.45"}
            )

            om = OrderManager(
                _make_settings(live_mode=True, with_clob=True),
                sessionmaker,
                clob_client=clob,
            )
            result = await om.submit(**_live_submit_kwargs())
        finally:
            _lg.remove(sink_id)

        assert result.success is True  # ordem executou — aviso é auditoria
        assert result.executed_price == Decimal("0.45")
        assert any("slippage acima do limite" in m.lower() for m in captured), (
            f"esperava warning de slippage em captured: {captured}"
        )
