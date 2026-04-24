"""Notificador Telegram — HTTP directo para Bot API, sem dependências extra.

Princípio (CLAUDE.md §12, §13): notificações NUNCA podem parar o bot. Todas as
chamadas a `send()` são fire-and-forget com retry 2× + 5s de espera; se tudo
falhar, apenas é logado um warning.

Tipos de mensagem suportados:
- Trade executada (paper ou live)
- Trade ignorada (skip com motivo)
- Stop loss (crítico)
- Erro crítico (exceção não tratada)
- Relatório semanal (domingo 22:00)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import aiohttp
from loguru import logger

from polymarket_bot.execution.pipeline import PipelineOutcome, PipelineResult

TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_PARSE_MODE = "HTML"
_MAX_ATTEMPTS = 2
_RETRY_SLEEP_SECONDS = 5


@dataclass(frozen=True)
class WeeklyReport:
    """Dados agregados para o relatório semanal (§12)."""

    week_start: date
    pnl_usdc: Decimal
    pnl_pct: float
    trades_won: int
    trades_lost: int
    trades_skipped: int
    top_wallets: list[str] = field(default_factory=list)   # top 3 por P&L
    next_wallets: list[str] = field(default_factory=list)  # 7 para a semana seguinte
    capital_current: Decimal = Decimal("0")
    capital_initial: Decimal = Decimal("0")

    @property
    def total_trades(self) -> int:
        return self.trades_won + self.trades_lost

    @property
    def win_rate(self) -> float:
        return self.trades_won / self.total_trades if self.total_trades else 0.0


class TelegramNotifier:
    """Envia mensagens ao chat configurado via Telegram Bot API."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        session: aiohttp.ClientSession | None = None,
        api_base: str = TELEGRAM_API_BASE,
    ):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._api_base = api_base.rstrip("/")
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "TelegramNotifier":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Accept": "application/json"},
            )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ send
    async def send(self, text: str, parse_mode: str = DEFAULT_PARSE_MODE) -> None:
        """POST /sendMessage com retry 2×. Nunca lança excepção."""
        url = f"{self._api_base}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        last_error: str | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                session = await self._ensure_session()
                async with session.post(url, json=payload) as resp:
                    if resp.status < 400:
                        return
                    body = await resp.text()
                    last_error = f"HTTP {resp.status}: {body[:200]}"
                    logger.warning(
                        "telegram: envio falhou (tentativa {}/{}) — {}",
                        attempt,
                        _MAX_ATTEMPTS,
                        last_error,
                    )
            except Exception as exc:  # noqa: BLE001 — notifier must not raise
                last_error = str(exc)
                logger.warning(
                    "telegram: exceção (tentativa {}/{}) — {}",
                    attempt,
                    _MAX_ATTEMPTS,
                    exc,
                )

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_SLEEP_SECONDS)

        logger.error("telegram: envio abandonado após {} tentativas — {}",
                     _MAX_ATTEMPTS, last_error)

    # ------------------------------------------------------------------ helpers
    async def trade_executed(
        self,
        result: PipelineResult,
        market_question: str,
        is_paper: bool,
    ) -> None:
        mode = "PAPER" if is_paper else "LIVE"
        outcome = result.consensus.direction_outcome if result.consensus else "?"
        price = _fmt_decimal(
            result.depth.avg_fill_price if result.depth else None,
            digits=3,
            default="?",
        )
        size = _fmt_decimal(
            result.size.final_size_usd if result.size else None,
            digits=2,
            default="?",
        )
        ev_pct = f"{result.ev.margin:+.1%}" if result.ev else "?"

        text = (
            f"✅ <b>TRADE</b> [{mode}]\n"
            f"{_escape(market_question)}\n"
            f"{outcome} @ {price}\n"
            f"${size} | EV {ev_pct}"
        )
        await self.send(text)

    async def trade_skipped(
        self,
        result: PipelineResult,
        market_question: str,
    ) -> None:
        reason = result.reason or result.outcome.value
        text = f"⏭ skip: {_escape(reason)} — {_escape(market_question[:80])}"
        await self.send(text)

    async def stop_loss_triggered(self, reason: str, pnl_pct: float) -> None:
        text = (
            f"🛑 <b>STOP LOSS</b> {_escape(reason)} | "
            f"P&L semana: {pnl_pct:+.1%}"
        )
        await self.send(text)

    async def critical_error(self, error: str, module: str) -> None:
        text = (
            f"🚨 <b>ERRO</b> [{_escape(module)}]\n"
            f"{_escape(error)}"
        )
        await self.send(text)

    async def weekly_report(self, report: WeeklyReport) -> None:
        top = "\n".join(f"  • {_escape(w)}" for w in report.top_wallets) or "  (nenhuma)"
        nxt = "\n".join(f"  • {_escape(w)}" for w in report.next_wallets) or "  (nenhuma)"
        text = (
            f"📊 <b>Relatório semanal</b> — {report.week_start.isoformat()}\n"
            f"\n"
            f"P&L: {report.pnl_usdc:+.2f} USDC ({report.pnl_pct:+.1%})\n"
            f"Capital: ${report.capital_current:.2f} (inicial ${report.capital_initial:.2f})\n"
            f"\n"
            f"Trades: {report.trades_won}W / {report.trades_lost}L / "
            f"{report.trades_skipped} skip\n"
            f"Win rate: {report.win_rate:.1%}\n"
            f"\n"
            f"<b>Top 3 wallets da semana:</b>\n{top}\n"
            f"\n"
            f"<b>Próximas 7 (a partir de segunda):</b>\n{nxt}"
        )
        await self.send(text)

    # ------------------------------------------------------------------ internal
    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Accept": "application/json"},
            )
            self._owns_session = True
        return self._session


def _escape(text: str) -> str:
    """Escape mínimo HTML para não partir a formatação do Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_decimal(value: Decimal | None, digits: int, default: str = "?") -> str:
    if value is None:
        return default
    return f"{float(value):.{digits}f}"


# Pequeno atalho para o caller checar se o pipeline executou.
def _was_executed(result: PipelineResult) -> bool:
    return result.outcome == PipelineOutcome.EXECUTED
