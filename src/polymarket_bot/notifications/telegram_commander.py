"""Comandos interativos do Telegram — long polling sobre a Bot API.

Princípio (CLAUDE.md §12, §13): comandos são bidirecionais mas não podem parar
o bot. O loop `run_forever` apanha qualquer excepção, dorme 10s e continua.
Mensagens de chats não autorizados são silenciosamente ignoradas.

Não usa `python-telegram-bot` — apenas HTTP direto via `aiohttp`, partilhando
a `ClientSession` do `TelegramNotifier`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

import aiohttp
from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_bot.db.enums import (
    BotTradeStatus,
    CircuitBreakerStatus,
    PositionStatus,
)
from polymarket_bot.db.models import (
    BotTrade,
    CircuitBreakerState,
    Market,
    Position,
    Wallet,
    WalletScore,
)
from polymarket_bot.monitoring.market_builder import MarketBuilder
from polymarket_bot.monitoring.wallet_monitor import WalletMonitor
from polymarket_bot.risk.circuit_breaker import CircuitBreaker

TELEGRAM_API_BASE = "https://api.telegram.org"
LONG_POLL_TIMEOUT_SECONDS = 30
ERROR_BACKOFF_SECONDS = 10
SEND_MAX_ATTEMPTS = 2
SEND_RETRY_SLEEP = 5
INITIAL_CAPITAL_USD = Decimal("1000")


@dataclass(frozen=True)
class _Command:
    name: str
    description: str
    handler_attr: str


class TelegramCommander:
    """Long polling Telegram com handlers para comandos do bot."""

    def __init__(
        self,
        bot_token: str,
        allowed_chat_id: str,
        session: aiohttp.ClientSession,
        session_factory: async_sessionmaker[AsyncSession],
        monitor: WalletMonitor,
        circuit_breaker: CircuitBreaker,
        started_at: datetime,
        live_mode: bool,
        market_builder: MarketBuilder | None = None,
        api_base: str = TELEGRAM_API_BASE,
    ):
        self._bot_token = bot_token
        self._allowed_chat_id = str(allowed_chat_id)
        self._session = session
        self._sessionmaker = session_factory
        self._monitor = monitor
        self._circuit_breaker = circuit_breaker
        self._started_at = started_at
        self._live_mode = live_mode
        self._market_builder = market_builder
        self._api_base = api_base.rstrip("/")
        self._last_update_id = 0
        self._stop_event = asyncio.Event()

        self._commands: list[_Command] = [
            _Command("status", "Estado atual do bot", "_cmd_status"),
            _Command("saldo", "Capital estimado e P&L", "_cmd_saldo"),
            _Command("posicoes", "Posições abertas", "_cmd_posicoes"),
            _Command("historico", "Últimas 15 trades", "_cmd_historico"),
            _Command("skips", "Últimas 10 trades ignoradas", "_cmd_skips"),
            _Command("wallets", "Wallets seguidas", "_cmd_wallets"),
            _Command("semana", "Resumo da semana atual", "_cmd_semana"),
            _Command("pause", "Pausar trading manualmente", "_cmd_pause"),
            _Command("resume", "Retomar trading", "_cmd_resume"),
            _Command("halt", "Parar bot completamente", "_cmd_halt"),
            _Command("ajuda", "Lista todos os comandos", "_cmd_ajuda"),
        ]

    # ------------------------------------------------------------------ loop
    async def run_forever(self) -> None:
        """Long-polling loop. Nunca lança — apanha tudo, dorme, continua."""
        logger.info("telegram_commander: a iniciar long polling")
        while not self._stop_event.is_set():
            try:
                updates = await self._get_updates()
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._last_update_id = max(self._last_update_id, update_id)
                    try:
                        await self._handle_update(update)
                    except Exception as exc:  # noqa: BLE001 — handler isolado
                        logger.warning(
                            "telegram_commander: handler falhou — {}", exc
                        )
            except asyncio.CancelledError:
                logger.info("telegram_commander: cancelado")
                raise
            except Exception as exc:  # noqa: BLE001 — loop não pode crashar
                logger.warning(
                    "telegram_commander: erro no polling — {} (sleep {}s)",
                    exc,
                    ERROR_BACKOFF_SECONDS,
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=ERROR_BACKOFF_SECONDS
                    )
                except asyncio.TimeoutError:
                    continue

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------ polling
    async def _get_updates(self) -> list[dict[str, Any]]:
        url = f"{self._api_base}/bot{self._bot_token}/getUpdates"
        params = {
            "timeout": LONG_POLL_TIMEOUT_SECONDS,
            "offset": self._last_update_id + 1,
        }
        async with self._session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=LONG_POLL_TIMEOUT_SECONDS + 10),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"getUpdates HTTP {resp.status}: {body[:200]}")
            data = await resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(f"getUpdates resposta inválida: {str(data)[:200]}")
        result = data.get("result") or []
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------ dispatch
    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id or chat_id != self._allowed_chat_id:
            # Ignora silenciosamente — não responde nem loga.
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return

        command = self._parse_command(text)
        if command is None:
            return

        for cmd in self._commands:
            if cmd.name == command:
                handler = getattr(self, cmd.handler_attr)
                try:
                    await handler(chat_id)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "telegram_commander: comando /{} falhou — {}",
                        cmd.name,
                        exc,
                    )
                    await self._reply(
                        chat_id, "⚠️ Erro interno — tenta de novo"
                    )
                return

    @staticmethod
    def _parse_command(text: str) -> str | None:
        """Extrai o nome do comando, removendo `@botname` e args."""
        first = text.strip().split()[0] if text.strip() else ""
        if not first.startswith("/"):
            return None
        body = first[1:]
        if "@" in body:
            body = body.split("@", 1)[0]
        return body.lower() or None

    # ------------------------------------------------------------------ commands
    async def _cmd_status(self, chat_id: str) -> None:
        breaker = await self._circuit_breaker.get_status()
        n_followed = len(self._monitor.reader.followed_addresses)
        uptime = _format_uptime(datetime.now(timezone.utc) - self._started_at)
        mode = "LIVE" if self._live_mode else "PAPER"
        text = (
            f"🤖 <b>Bot Polymarket</b>\n"
            f"Modo: {mode} | Estado: {breaker.value}\n"
            f"Wallets seguidas: {n_followed}\n"
            f"Uptime: {uptime}"
        )
        await self._reply(chat_id, text)

    async def _cmd_saldo(self, chat_id: str) -> None:
        async with self._sessionmaker() as session:
            total_realized = await _sum_realized_pnl(session)
            week_start = _current_week_start()
            week_realized = await _sum_realized_pnl_since(session, week_start)

        capital = INITIAL_CAPITAL_USD + total_realized
        total_pct = float(total_realized / INITIAL_CAPITAL_USD) if INITIAL_CAPITAL_USD else 0.0
        week_pct = float(week_realized / INITIAL_CAPITAL_USD) if INITIAL_CAPITAL_USD else 0.0
        text = (
            f"💰 <b>Saldo estimado</b>\n"
            f"Capital: ${capital:,.2f}\n"
            f"P&L total: {total_realized:+.2f} ({total_pct:+.1%})\n"
            f"P&L esta semana: {week_realized:+.2f} ({week_pct:+.1%})"
        )
        await self._reply(chat_id, text)

    async def _cmd_posicoes(self, chat_id: str) -> None:
        async with self._sessionmaker() as session:
            stmt = (
                select(Position)
                .where(Position.status == PositionStatus.OPEN)
                .order_by(desc(Position.opened_at))
                .limit(10)
            )
            positions = (await session.execute(stmt)).scalars().all()
            market_questions = await _load_market_questions(
                session, [p.market_id for p in positions]
            )

        if not positions:
            await self._reply(chat_id, "📂 Sem posições abertas")
            return

        lines = [f"📂 <b>Posições abertas ({len(positions)})</b>"]
        now = datetime.now(timezone.utc)
        for p in positions:
            question = market_questions.get(p.market_id) or p.market_id
            question = _truncate(question, 40)
            opened_age = _format_age(now - _aware(p.opened_at))
            lines.append(
                f"• {_escape(question)} — {p.outcome} @ {float(p.avg_entry_price):.3f} | "
                f"${float(p.size_usd):.2f} | aberta há {opened_age}"
            )
        await self._reply(chat_id, "\n".join(lines))

    async def _cmd_historico(self, chat_id: str) -> None:
        async with self._sessionmaker() as session:
            stmt = (
                select(BotTrade)
                .where(BotTrade.status != BotTradeStatus.SKIPPED)
                .order_by(desc(BotTrade.created_at))
                .limit(15)
            )
            trades = (await session.execute(stmt)).scalars().all()
            market_questions = await _load_market_questions(
                session, [t.market_id for t in trades]
            )

        if not trades:
            await self._reply(chat_id, "📋 Sem histórico de trades")
            return

        lines = ["📋 <b>Últimas trades</b>"]
        now = datetime.now(timezone.utc)
        for t in trades:
            icon = _status_icon(t.status)
            question = market_questions.get(t.market_id) or t.market_id
            question = _truncate(question, 40)
            age = _format_age(now - _aware(t.created_at))
            lines.append(
                f"{icon} {_escape(question)} {t.outcome} ${float(t.size_usd):.0f} "
                f"{t.status.value} (há {age})"
            )
        await self._reply(chat_id, "\n".join(lines))

    async def _cmd_skips(self, chat_id: str) -> None:
        async with self._sessionmaker() as session:
            stmt = (
                select(BotTrade)
                .where(BotTrade.status == BotTradeStatus.SKIPPED)
                .order_by(desc(BotTrade.created_at))
                .limit(10)
            )
            trades = (await session.execute(stmt)).scalars().all()
            market_questions = await _load_market_questions(
                session, [t.market_id for t in trades]
            )

        if not trades:
            await self._reply(chat_id, "⏭ Sem trades ignoradas recentemente")
            return

        lines = ["⏭ <b>Últimas trades ignoradas</b>"]
        for t in trades:
            question = market_questions.get(t.market_id) or t.market_id
            question = _truncate(question, 40)
            reason = t.skip_reason or "—"
            lines.append(f"• {_escape(reason)} — {_escape(question)}")
        await self._reply(chat_id, "\n".join(lines))

    async def _cmd_wallets(self, chat_id: str) -> None:
        async with self._sessionmaker() as session:
            stmt = select(Wallet).where(Wallet.is_followed.is_(True))
            wallets = (await session.execute(stmt)).scalars().all()

            entries: list[tuple[Wallet, WalletScore | None]] = []
            for w in wallets:
                score_stmt = (
                    select(WalletScore)
                    .where(WalletScore.wallet_address == w.address)
                    .order_by(desc(WalletScore.scored_at))
                    .limit(1)
                )
                latest = (await session.execute(score_stmt)).scalar_one_or_none()
                entries.append((w, latest))

        if not entries:
            await self._reply(chat_id, "👛 Sem wallets seguidas")
            return

        entries.sort(
            key=lambda e: (e[1].total_score if e[1] else 0.0), reverse=True
        )

        lines = [f"👛 <b>Wallets seguidas ({len(entries)})</b>"]
        for wallet, score in entries:
            tier = (wallet.current_tier.value if wallet.current_tier else "—")
            icon = "🥇" if tier == "TOP" else "🔹"
            score_text = f"{score.total_score:.1f}" if score else "—"
            tier_short = "TOP" if tier == "TOP" else "BOT"
            lines.append(
                f"{icon} {_short_addr(wallet.address)} | score {score_text} | {tier_short}"
            )
        await self._reply(chat_id, "\n".join(lines))

    async def _cmd_semana(self, chat_id: str) -> None:
        week_start = _current_week_start()
        async with self._sessionmaker() as session:
            stmt = (
                select(BotTrade)
                .where(BotTrade.created_at >= week_start)
            )
            trades = (await session.execute(stmt)).scalars().all()

            pos_stmt = (
                select(Position)
                .where(
                    Position.status == PositionStatus.CLOSED,
                    Position.closed_at >= week_start,
                    Position.realized_pnl_usd.is_not(None),
                )
            )
            closed = (await session.execute(pos_stmt)).scalars().all()

        won = sum(
            1 for p in closed if p.realized_pnl_usd and p.realized_pnl_usd > 0
        )
        lost = sum(
            1 for p in closed if p.realized_pnl_usd and p.realized_pnl_usd <= 0
        )
        skipped = sum(1 for t in trades if t.status == BotTradeStatus.SKIPPED)
        pnl = sum(
            (p.realized_pnl_usd for p in closed if p.realized_pnl_usd is not None),
            Decimal("0"),
        )
        best = max(
            (p.realized_pnl_usd for p in closed if p.realized_pnl_usd is not None),
            default=None,
        )
        worst = min(
            (p.realized_pnl_usd for p in closed if p.realized_pnl_usd is not None),
            default=None,
        )

        lines = [
            f"📆 <b>Semana de {week_start.date().isoformat()}</b>",
            f"Trades: {won}W / {lost}L / {skipped} skip",
            f"P&L realizado: {pnl:+.2f}",
        ]
        if best is not None:
            lines.append(f"Melhor: {best:+.2f}")
        if worst is not None:
            lines.append(f"Pior: {worst:+.2f}")
        await self._reply(chat_id, "\n".join(lines))

    async def _cmd_pause(self, chat_id: str) -> None:
        await self._insert_breaker_state(
            CircuitBreakerStatus.PAUSED, "manual /pause"
        )
        await self._reply(chat_id, "⏸ Bot pausado — usa /resume para retomar")

    async def _cmd_resume(self, chat_id: str) -> None:
        await self._insert_breaker_state(
            CircuitBreakerStatus.NORMAL, "manual /resume"
        )
        await self._reply(chat_id, "▶️ Bot retomado")

    async def _cmd_halt(self, chat_id: str) -> None:
        await self._insert_breaker_state(
            CircuitBreakerStatus.HALTED, "manual /halt"
        )
        await self._reply(chat_id, "🛑 Bot HALTED — apenas /resume reativa")

    async def _cmd_ajuda(self, chat_id: str) -> None:
        lines = ["🆘 <b>Comandos</b>"]
        for cmd in self._commands:
            lines.append(f"/{cmd.name} — {cmd.description}")
        await self._reply(chat_id, "\n".join(lines))

    # ------------------------------------------------------------------ helpers
    async def _insert_breaker_state(
        self, status: CircuitBreakerStatus, reason: str
    ) -> None:
        async with self._sessionmaker() as session:
            latest_stmt = (
                select(CircuitBreakerState)
                .order_by(desc(CircuitBreakerState.triggered_at))
                .limit(1)
            )
            latest = (await session.execute(latest_stmt)).scalar_one_or_none()
            session.add(
                CircuitBreakerState(
                    status=status,
                    reason=reason[:300],
                    triggered_at=datetime.now(timezone.utc),
                    resumes_at=None,
                    consecutive_negative_weeks=(
                        latest.consecutive_negative_weeks if latest else 0
                    ),
                    size_reduction_factor=(
                        0.0 if status != CircuitBreakerStatus.NORMAL else 1.0
                    ),
                )
            )
            await session.commit()

    async def _reply(self, chat_id: str, text: str) -> None:
        url = f"{self._api_base}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        last_error: str | None = None
        for attempt in range(1, SEND_MAX_ATTEMPTS + 1):
            try:
                async with self._session.post(url, json=payload) as resp:
                    if resp.status < 400:
                        return
                    body = await resp.text()
                    last_error = f"HTTP {resp.status}: {body[:200]}"
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)

            if attempt < SEND_MAX_ATTEMPTS:
                await asyncio.sleep(SEND_RETRY_SLEEP)
        logger.warning("telegram_commander: reply abandonado — {}", last_error)


# ---------------------------------------------------------------- DB helpers
async def _sum_realized_pnl(session: AsyncSession) -> Decimal:
    stmt = select(Position.realized_pnl_usd).where(
        Position.realized_pnl_usd.is_not(None)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return sum((r for r in rows if r is not None), Decimal("0"))


async def _sum_realized_pnl_since(
    session: AsyncSession, since: datetime
) -> Decimal:
    stmt = select(Position.realized_pnl_usd).where(
        Position.realized_pnl_usd.is_not(None),
        Position.closed_at.is_not(None),
        Position.closed_at >= since,
    )
    rows = (await session.execute(stmt)).scalars().all()
    return sum((r for r in rows if r is not None), Decimal("0"))


async def _load_market_questions(
    session: AsyncSession, market_ids: list[str]
) -> dict[str, str]:
    if not market_ids:
        return {}
    unique_ids = list({mid for mid in market_ids if mid})
    if not unique_ids:
        return {}
    stmt = select(Market).where(Market.market_id.in_(unique_ids))
    rows = (await session.execute(stmt)).scalars().all()
    return {m.market_id: m.question for m in rows}


# ---------------------------------------------------------------- formatting
def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _short_addr(addr: str) -> str:
    if len(addr) <= 12:
        return addr
    return f"{addr[:6]}...{addr[-3:]}"


def _status_icon(status: BotTradeStatus) -> str:
    if status == BotTradeStatus.FILLED:
        return "✅"
    if status in (BotTradeStatus.PENDING, BotTradeStatus.SUBMITTED, BotTradeStatus.PARTIAL):
        return "⏳"
    if status in (BotTradeStatus.FAILED, BotTradeStatus.CANCELLED):
        return "❌"
    return "•"


def _format_age(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def _format_uptime(delta: timedelta) -> str:
    total = max(int(delta.total_seconds()), 0)
    hours = total // 3600
    minutes = (total % 3600) // 60
    if hours == 0:
        return f"{minutes}m"
    return f"{hours}h {minutes}m"


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _current_week_start() -> datetime:
    """Segunda-feira 00:00 UTC da semana corrente."""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)
