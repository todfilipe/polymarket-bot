"""Testes do TelegramCommander — dispatch, isolamento por chat_id e ajuda."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from polymarket_bot.db.models import Base
from polymarket_bot.notifications.telegram_commander import TelegramCommander


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
    """Mimics aiohttp.ClientSession.post para capturar replies."""

    def __init__(self):
        self.posts: list[dict] = []

    def post(self, url: str, *, json=None):
        self.posts.append({"url": url, "json": json})

        @asynccontextmanager
        async def ctx():
            yield _FakeResponse(200)

        return ctx()


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


def _make_commander(session, sessionmaker_) -> TelegramCommander:
    monitor = MagicMock()
    monitor.reader = MagicMock()
    monitor.reader.followed_addresses = ["0xaaa", "0xbbb"]

    breaker = MagicMock()
    breaker.get_status = AsyncMock(return_value=MagicMock(value="NORMAL"))

    return TelegramCommander(
        bot_token="TOK",
        allowed_chat_id="42",
        session=session,
        session_factory=sessionmaker_,
        monitor=monitor,
        circuit_breaker=breaker,
        started_at=datetime.now(timezone.utc),
        live_mode=False,
    )


def _update(text: str, chat_id: int = 42, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_handle_update_dispatches_to_correct_handler(sessionmaker):
    session = FakeSession()
    cmd = _make_commander(session, sessionmaker)

    cmd._cmd_status = AsyncMock()  # type: ignore[assignment]
    cmd._cmd_ajuda = AsyncMock()  # type: ignore[assignment]

    await cmd._handle_update(_update("/status"))
    await cmd._handle_update(_update("/ajuda"))

    cmd._cmd_status.assert_awaited_once_with("42")
    cmd._cmd_ajuda.assert_awaited_once_with("42")


@pytest.mark.asyncio
async def test_updates_from_other_chat_are_ignored(sessionmaker):
    session = FakeSession()
    cmd = _make_commander(session, sessionmaker)
    cmd._cmd_status = AsyncMock()  # type: ignore[assignment]

    await cmd._handle_update(_update("/status", chat_id=999))

    cmd._cmd_status.assert_not_awaited()
    assert session.posts == []


@pytest.mark.asyncio
async def test_command_with_botname_suffix_is_recognised(sessionmaker):
    session = FakeSession()
    cmd = _make_commander(session, sessionmaker)
    cmd._cmd_status = AsyncMock()  # type: ignore[assignment]

    await cmd._handle_update(_update("/status@MyBot"))

    cmd._cmd_status.assert_awaited_once_with("42")


@pytest.mark.asyncio
async def test_unknown_command_does_not_reply(sessionmaker):
    session = FakeSession()
    cmd = _make_commander(session, sessionmaker)

    await cmd._handle_update(_update("/desconhecido"))

    assert session.posts == []


@pytest.mark.asyncio
async def test_non_command_text_is_ignored(sessionmaker):
    session = FakeSession()
    cmd = _make_commander(session, sessionmaker)
    cmd._cmd_status = AsyncMock()  # type: ignore[assignment]

    await cmd._handle_update(_update("olá bot"))

    cmd._cmd_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_ajuda_lists_all_commands(sessionmaker):
    session = FakeSession()
    cmd = _make_commander(session, sessionmaker)

    await cmd._cmd_ajuda("42")

    assert len(session.posts) == 1
    text = session.posts[0]["json"]["text"]
    for name in (
        "status",
        "saldo",
        "posicoes",
        "historico",
        "skips",
        "wallets",
        "semana",
        "pause",
        "resume",
        "halt",
        "ajuda",
    ):
        assert f"/{name}" in text


@pytest.mark.asyncio
async def test_status_includes_mode_and_breaker(sessionmaker):
    session = FakeSession()
    cmd = _make_commander(session, sessionmaker)

    await cmd._cmd_status("42")

    text = session.posts[0]["json"]["text"]
    assert "PAPER" in text
    assert "NORMAL" in text
    assert "Wallets seguidas: 2" in text


@pytest.mark.asyncio
async def test_handler_exception_replies_internal_error(sessionmaker):
    session = FakeSession()
    cmd = _make_commander(session, sessionmaker)
    cmd._cmd_status = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[assignment]

    await cmd._handle_update(_update("/status"))

    assert len(session.posts) == 1
    assert "Erro interno" in session.posts[0]["json"]["text"]


@pytest.mark.asyncio
async def test_pause_resume_halt_persist_breaker_state(sessionmaker):
    from sqlalchemy import select

    from polymarket_bot.db.enums import CircuitBreakerStatus
    from polymarket_bot.db.models import CircuitBreakerState

    session = FakeSession()
    cmd = _make_commander(session, sessionmaker)

    await cmd._cmd_pause("42")
    await cmd._cmd_resume("42")
    await cmd._cmd_halt("42")

    async with sessionmaker() as s:
        rows = (
            await s.execute(
                select(CircuitBreakerState).order_by(
                    CircuitBreakerState.triggered_at
                )
            )
        ).scalars().all()

    statuses = [r.status for r in rows]
    assert statuses == [
        CircuitBreakerStatus.PAUSED,
        CircuitBreakerStatus.NORMAL,
        CircuitBreakerStatus.HALTED,
    ]
    assert all("manual" in r.reason for r in rows)


@pytest.mark.asyncio
async def test_parse_command_strips_botname_and_lowercases():
    assert TelegramCommander._parse_command("/Status@MyBot") == "status"
    assert TelegramCommander._parse_command("/ajuda") == "ajuda"
    assert TelegramCommander._parse_command("olá") is None
    assert TelegramCommander._parse_command("/halt arg1 arg2") == "halt"
