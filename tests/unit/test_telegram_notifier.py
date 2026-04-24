"""Testes do TelegramNotifier — mocks HTTP via patch na ClientSession.post."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from polymarket_bot.db.enums import TradeSide, WalletTier
from polymarket_bot.execution.pipeline import (
    PipelineOutcome,
    PipelineResult,
)
from polymarket_bot.execution.sizer import SizeDecision
from polymarket_bot.market import (
    ConsensusDecision,
    ConsensusStrength,
    EvResult,
    OrderBookDepthResult,
)
from polymarket_bot.notifications.telegram_notifier import (
    TelegramNotifier,
    WeeklyReport,
)


# ---- Helpers ------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int = 200, text: str = "ok"):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def text(self) -> str:
        return self._text


class FakeSession:
    """Mimics aiohttp.ClientSession.post — capta args e devolve resposta preparada."""

    def __init__(self, responses: list[_FakeResponse] | None = None):
        self.calls: list[dict] = []
        self._responses = responses or [_FakeResponse(200)]
        self._idx = 0

    def post(self, url: str, *, json=None):
        self.calls.append({"url": url, "json": json})
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1

        @asynccontextmanager
        async def ctx():
            yield resp

        return ctx()


def _make_result(outcome: PipelineOutcome, **overrides) -> PipelineResult:
    defaults = dict(
        consensus=ConsensusDecision(
            strength=ConsensusStrength.CONSENSUS,
            direction_outcome="YES",
            n_wallets=2,
            n_top_tier=1,
            n_bottom_tier=1,
            tier_multiplier=1.4,
            signal_multiplier=1.3,
            reason=None,
        ),
        market_filters=None,
        probability_estimate=None,
        ev=EvResult(
            stake_usd=Decimal("80"),
            entry_price=Decimal("0.41"),
            true_probability=Decimal("0.55"),
            potential_profit_usd=Decimal("115"),
            expected_value_usd=Decimal("27"),
            margin=0.34,
            passes_min_margin=True,
            reason=None,
        ),
        size=SizeDecision(
            bankroll_usd=Decimal("1000"),
            base_size_usd=Decimal("50"),
            tier_multiplier=1.4,
            signal_multiplier=1.3,
            recovery_multiplier=1.0,
            pre_cap_size_usd=Decimal("91"),
            final_size_usd=Decimal("80"),
            hit_cap=True,
            below_minimum=False,
            skip_reason=None,
        ),
        depth=OrderBookDepthResult(
            fillable=True,
            fillable_usd=Decimal("80"),
            avg_fill_price=Decimal("0.412"),
            price_impact=0.01,
            reason=None,
        ),
    )
    defaults.update(overrides)
    return PipelineResult(outcome=outcome, reason=None, **defaults)


# ---- Tests --------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_posts_correct_payload():
    session = FakeSession([_FakeResponse(200)])
    notifier = TelegramNotifier(
        bot_token="TOK", chat_id="123", session=session  # type: ignore[arg-type]
    )

    await notifier.send("olá <b>mundo</b>", parse_mode="HTML")

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"].endswith("/botTOK/sendMessage")
    assert call["json"]["chat_id"] == "123"
    assert call["json"]["text"] == "olá <b>mundo</b>"
    assert call["json"]["parse_mode"] == "HTML"
    assert call["json"]["disable_web_page_preview"] is True


@pytest.mark.asyncio
async def test_send_retries_on_http_failure_then_swallows():
    # 2 tentativas, ambas HTTP 500 — nunca lança, só loga.
    session = FakeSession([_FakeResponse(500, "boom"), _FakeResponse(500, "boom")])
    notifier = TelegramNotifier(
        bot_token="T", chat_id="0", session=session  # type: ignore[arg-type]
    )

    # Monkey-patch asyncio.sleep para o teste ser instantâneo.
    import polymarket_bot.notifications.telegram_notifier as tn_mod

    original_sleep = tn_mod.asyncio.sleep
    tn_mod.asyncio.sleep = AsyncMock(return_value=None)  # type: ignore[assignment]
    try:
        await notifier.send("hey")  # não levanta
    finally:
        tn_mod.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert len(session.calls) == 2  # 2 tentativas


@pytest.mark.asyncio
async def test_send_swallows_connection_exception():
    """Exceção de rede → não propaga."""

    class ExplodingSession:
        def __init__(self):
            self.calls = 0

        def post(self, *_a, **_k):
            self.calls += 1

            @asynccontextmanager
            async def ctx():
                raise ConnectionError("network down")
                yield  # noqa: pragma

            return ctx()

    session = ExplodingSession()
    notifier = TelegramNotifier(
        bot_token="T", chat_id="0", session=session  # type: ignore[arg-type]
    )

    import polymarket_bot.notifications.telegram_notifier as tn_mod

    original_sleep = tn_mod.asyncio.sleep
    tn_mod.asyncio.sleep = AsyncMock(return_value=None)  # type: ignore[assignment]
    try:
        await notifier.send("hey")  # não levanta
    finally:
        tn_mod.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert session.calls == 2


@pytest.mark.asyncio
async def test_trade_executed_formats_with_checkmark_and_fields():
    session = FakeSession()
    notifier = TelegramNotifier("T", "0", session=session)  # type: ignore[arg-type]
    result = _make_result(PipelineOutcome.EXECUTED)

    await notifier.trade_executed(
        result, market_question="Will X happen?", is_paper=True
    )

    text = session.calls[0]["json"]["text"]
    assert "✅" in text
    assert "PAPER" in text
    assert "Will X happen?" in text
    assert "YES" in text
    assert "0.412" in text  # preço de fill (3 casas)
    assert "80.00" in text  # size (2 casas)
    assert "EV" in text and "+34" in text.replace(".", "")  # margin 34%


@pytest.mark.asyncio
async def test_trade_executed_live_mode_label():
    session = FakeSession()
    notifier = TelegramNotifier("T", "0", session=session)  # type: ignore[arg-type]

    await notifier.trade_executed(
        _make_result(PipelineOutcome.EXECUTED),
        market_question="Q",
        is_paper=False,
    )

    text = session.calls[0]["json"]["text"]
    assert "LIVE" in text
    assert "PAPER" not in text


@pytest.mark.asyncio
async def test_trade_skipped_is_single_line_with_reason():
    session = FakeSession()
    notifier = TelegramNotifier("T", "0", session=session)  # type: ignore[arg-type]

    result = PipelineResult(
        outcome=PipelineOutcome.SKIPPED_EV,
        reason="EV margin 5% ≤ 10%",
    )

    await notifier.trade_skipped(result, market_question="Will X?")

    text = session.calls[0]["json"]["text"]
    # Uma única linha — sem newlines internas.
    assert "\n" not in text
    assert text.startswith("⏭ skip:")
    assert "EV margin" in text


@pytest.mark.asyncio
async def test_stop_loss_triggered_formats_pnl():
    session = FakeSession()
    notifier = TelegramNotifier("T", "0", session=session)  # type: ignore[arg-type]

    await notifier.stop_loss_triggered(reason="hard SL", pnl_pct=-0.18)

    text = session.calls[0]["json"]["text"]
    assert "🛑" in text
    assert "STOP LOSS" in text
    assert "hard SL" in text
    assert "-18.0%" in text


@pytest.mark.asyncio
async def test_critical_error_includes_module():
    session = FakeSession()
    notifier = TelegramNotifier("T", "0", session=session)  # type: ignore[arg-type]

    await notifier.critical_error(error="boom", module="pipeline")

    text = session.calls[0]["json"]["text"]
    assert "🚨" in text
    assert "[pipeline]" in text
    assert "boom" in text


@pytest.mark.asyncio
async def test_weekly_report_includes_pnl_winrate_and_wallets():
    session = FakeSession()
    notifier = TelegramNotifier("T", "0", session=session)  # type: ignore[arg-type]

    report = WeeklyReport(
        week_start=date(2026, 4, 20),
        pnl_usdc=Decimal("35.50"),
        pnl_pct=0.0355,
        trades_won=7,
        trades_lost=3,
        trades_skipped=42,
        top_wallets=["0xaaa", "0xbbb", "0xccc"],
        next_wallets=[
            "0xnew1",
            "0xnew2",
            "0xnew3",
            "0xnew4",
            "0xnew5",
            "0xnew6",
            "0xnew7",
        ],
        capital_current=Decimal("1035.50"),
        capital_initial=Decimal("1000"),
    )

    await notifier.weekly_report(report)

    text = session.calls[0]["json"]["text"]
    assert "Relatório semanal" in text
    assert "2026-04-20" in text
    assert "+35.50" in text        # P&L USDC
    assert "+3.5%" in text          # P&L %
    assert "70.0%" in text          # win rate 7/(7+3)
    assert "0xaaa" in text
    assert "0xnew1" in text and "0xnew7" in text
    assert "42 skip" in text
