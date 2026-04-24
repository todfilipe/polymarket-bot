"""Gestão de saídas de posições abertas (CLAUDE.md §6).

Hierarquia de decisão por posição (em ordem estrita de prioridade):

    1. Hard Stop Loss   — P&L ≤ −40% do investido → fecho total, sempre.
    2. Take Profit      — mid price ≥ 75% → fecho parcial 50% (1× por posição).
    3. Saída temporal   — resolve em < 6h e em lucro → fecho total.
    4. Wallet exit      — wallet seguida vendeu E `timing_skill_ratio > 0.60`.

Regras §7 do CLAUDE.md:
- Stop-loss próprio sempre activo — nunca depender da wallet.
- Averaging down nunca seguido. Wallet exit só se a wallet tem histórico
  consistente de exits precisos.

Modo paper: calcula P&L, actualiza DB, zero ordens CLOB.
Modo live: submete SELL ao CLOB com limit price 2% abaixo do mid (para garantir
fill) e polling 60s. Timeout → cancela ordem, marca `exit_pending_manual` e
notifica humano — fail-safe > deixar posição em estado inconsistente.

A detecção de SELLs das wallets vive em `_detect_wallet_exits`: se um
`SignalReader` é injectado, delega ao `poll_sells()` e cruza com os endereços
seguidos pela posição. Testes injectam candidatos directamente via monkeypatch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Callable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_bot.config.constants import CONST
from polymarket_bot.db.enums import PositionStatus, TradeSide
from polymarket_bot.db.models import Position, Wallet
from polymarket_bot.monitoring.market_builder import MarketBuilder, MarketBuildError
from polymarket_bot.notifications.telegram_notifier import TelegramNotifier
from polymarket_bot.risk.circuit_breaker import CircuitBreaker

_LIVE_EXIT_LIMIT_DISCOUNT = Decimal("0.98")  # 2% abaixo do mid para garantir fill
_POLL_WINDOW_SECONDS: int = CONST.ORDER_EXECUTION_WINDOW_SECONDS
_POLL_STEP_SECONDS: int = 5
_POLL_ITERATIONS: int = _POLL_WINDOW_SECONDS // _POLL_STEP_SECONDS


class LiveExitError(Exception):
    """Saída live falhou (timeout, rejeição CLOB, ou erro inesperado).

    O caller marca a posição como `exit_pending_manual` e notifica humano.
    """

HARD_STOP_LOSS_RATIO = Decimal(str(CONST.HARD_STOP_LOSS_PER_POSITION))
TAKE_PROFIT_PROBABILITY = Decimal(str(CONST.TAKE_PROFIT_PROBABILITY))
TAKE_PROFIT_CLOSE_FRACTION = Decimal(str(CONST.TAKE_PROFIT_CLOSE_FRACTION))
MIN_WALLET_TIMING_SKILL = float(CONST.MIN_WALLET_TIMING_SKILL)
EARLY_EXIT_HOURS = CONST.EARLY_EXIT_HOURS

_NOTE_PARTIAL_TAKEN = "partial_taken"
_NOTE_EXIT_PENDING_MANUAL = "exit_pending_manual"


class ExitReason(StrEnum):
    HARD_STOP_LOSS = "HARD_STOP_LOSS"
    TAKE_PROFIT_PARTIAL = "TAKE_PROFIT_PARTIAL"
    TEMPORAL_EXIT = "TEMPORAL_EXIT"
    WALLET_EXIT = "WALLET_EXIT"


@dataclass(frozen=True)
class ExitResult:
    position_id: int
    market_id: str
    outcome: str
    exit_reason: ExitReason | None
    pnl_usd: Decimal
    is_paper: bool
    skipped: bool
    skip_reason: str | None = None


@dataclass(frozen=True)
class WalletExitCandidate:
    """Sinal de SELL duma wallet seguida — consumido pela decisão de wallet exit."""

    wallet_address: str
    timing_skill_ratio: float


ClobClientFactory = Callable[[], Any]


class ExitManager:
    """Verifica posições OPEN e decide saídas conforme §6."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clob_client_factory: ClobClientFactory,
        market_builder: MarketBuilder,
        notifier: TelegramNotifier,
        circuit_breaker: CircuitBreaker,
        live_mode: bool = False,
        signal_reader: Any | None = None,
    ):
        self._sessionmaker = session_factory
        self._clob_client_factory = clob_client_factory
        self._market_builder = market_builder
        self._notifier = notifier
        self._circuit_breaker = circuit_breaker
        self._live_mode = live_mode
        self._signal_reader = signal_reader

    # ------------------------------------------------------------------ public
    async def check_all_positions(self) -> list[ExitResult]:
        """Avalia todas as posições OPEN. Falhas individuais não param o batch."""
        async with self._sessionmaker() as session:
            stmt = select(Position).where(Position.status == PositionStatus.OPEN)
            positions = list((await session.execute(stmt)).scalars().all())

        results: list[ExitResult] = []
        for pos in positions:
            log = logger.bind(position_id=pos.id, market_id=pos.market_id)
            try:
                result = await self._evaluate_position(pos)
            except Exception as exc:  # noqa: BLE001 — isolamento por posição
                log.opt(exception=True).error(
                    "exit_manager: falha a avaliar posição — {}", exc
                )
                continue
            results.append(result)
        return results

    # ------------------------------------------------------------------ per-position
    async def _evaluate_position(self, position: Position) -> ExitResult:
        log = logger.bind(position_id=position.id, market_id=position.market_id)

        # Snapshot de mercado (cache 30s no `MarketBuilder`).
        try:
            snapshot = await self._market_builder.build(
                position.market_id, position.outcome
            )
        except MarketBuildError as exc:
            return _skipped(position, self._live_mode, f"market_build: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.warning("exit_manager: falha a construir mercado — {}", exc)
            return _skipped(position, self._live_mode, f"market_build: {exc}")

        current_price = snapshot.orderbook.mid_price
        if current_price is None or current_price <= 0:
            return _skipped(position, self._live_mode, "mid price indisponível")
        if position.avg_entry_price <= 0:
            return _skipped(position, self._live_mode, "avg_entry_price inválido")

        current_value = (
            position.size_usd * (current_price / position.avg_entry_price)
        )
        pnl_usd = current_value - position.size_usd
        pnl_ratio = (
            pnl_usd / position.size_usd if position.size_usd > 0 else Decimal("0")
        )

        log = log.bind(
            current_price=str(current_price),
            pnl_usd=str(pnl_usd),
            pnl_ratio=f"{float(pnl_ratio):.3f}",
        )

        # 1) Hard stop loss — prioridade absoluta.
        if pnl_ratio <= HARD_STOP_LOSS_RATIO:
            log.bind(exit_reason=ExitReason.HARD_STOP_LOSS.value).warning(
                "exit: hard stop loss accionado"
            )
            result = await self._close_full(
                position, ExitReason.HARD_STOP_LOSS, current_price, pnl_usd
            )
            if not result.skipped:
                await self._after_stop_loss(pnl_ratio)
            return result

        # 2) Take profit parcial — 1× por posição.
        if (
            current_price >= TAKE_PROFIT_PROBABILITY
            and position.entries_count == 1
            and position.notes != _NOTE_PARTIAL_TAKEN
        ):
            log.bind(exit_reason=ExitReason.TAKE_PROFIT_PARTIAL.value).info(
                "exit: take profit parcial"
            )
            return await self._close_partial(
                position, current_price
            )

        # 3) Saída temporal.
        if (
            snapshot.hours_to_resolution < EARLY_EXIT_HOURS
            and current_value > position.size_usd
        ):
            log.bind(
                exit_reason=ExitReason.TEMPORAL_EXIT.value,
                hours_to_resolution=snapshot.hours_to_resolution,
            ).info("exit: saída temporal (resolve em breve e em lucro)")
            return await self._close_full(
                position, ExitReason.TEMPORAL_EXIT, current_price, pnl_usd
            )

        # 4) Wallet exit — só se a wallet tem histórico consistente.
        candidates = await self._detect_wallet_exits(position)
        for c in candidates:
            if c.timing_skill_ratio > MIN_WALLET_TIMING_SKILL:
                log.bind(
                    exit_reason=ExitReason.WALLET_EXIT.value,
                    wallet=c.wallet_address,
                    timing_skill=c.timing_skill_ratio,
                ).info("exit: wallet seguida vendeu (skill suficiente)")
                return await self._close_full(
                    position, ExitReason.WALLET_EXIT, current_price, pnl_usd
                )
            else:
                log.bind(wallet=c.wallet_address, timing_skill=c.timing_skill_ratio).info(
                    "exit: wallet vendeu mas skill insuficiente — ignorar"
                )

        return _skipped(position, self._live_mode, "sem condição de saída")

    # ------------------------------------------------------------------ closes
    async def _close_full(
        self,
        position: Position,
        reason: ExitReason,
        current_price: Decimal,
        pnl_usd: Decimal,
    ) -> ExitResult:
        if self._live_mode:
            try:
                await self._submit_live_exit(position, current_price, fraction=Decimal("1"))
            except (LiveExitError, NotImplementedError) as exc:
                return await self._mark_pending_manual(position, reason, pnl_usd, exc)

        async with self._sessionmaker() as session:
            # Re-fetch com lock lógico — garante idempotência se alguém correr em paralelo.
            fresh = await session.get(Position, position.id)
            if fresh is None or fresh.status != PositionStatus.OPEN:
                return _skipped(position, self._live_mode, "posição já fechada")
            fresh.status = PositionStatus.CLOSED
            fresh.closed_at = datetime.now(timezone.utc)
            fresh.realized_pnl_usd = pnl_usd
            fresh.notes = reason.value
            await session.commit()

        await self._notify_exit(reason, position, pnl_usd, current_price)
        return ExitResult(
            position_id=position.id,
            market_id=position.market_id,
            outcome=position.outcome,
            exit_reason=reason,
            pnl_usd=pnl_usd,
            is_paper=not self._live_mode,
            skipped=False,
            skip_reason=None,
        )

    async def _close_partial(
        self,
        position: Position,
        current_price: Decimal,
    ) -> ExitResult:
        """Fecha 50% do size — cria posição-filha CLOSED, reduz original."""
        partial_size = position.size_usd * TAKE_PROFIT_CLOSE_FRACTION
        partial_pnl = partial_size * (
            (current_price / position.avg_entry_price) - Decimal("1")
        )

        if self._live_mode:
            try:
                await self._submit_live_exit(
                    position, current_price, fraction=TAKE_PROFIT_CLOSE_FRACTION
                )
            except (LiveExitError, NotImplementedError) as exc:
                return await self._mark_pending_manual(
                    position, ExitReason.TAKE_PROFIT_PARTIAL, partial_pnl, exc
                )

        now = datetime.now(timezone.utc)
        async with self._sessionmaker() as session:
            fresh = await session.get(Position, position.id)
            if fresh is None or fresh.status != PositionStatus.OPEN:
                return _skipped(position, self._live_mode, "posição já fechada")
            if fresh.notes == _NOTE_PARTIAL_TAKEN:
                return _skipped(
                    position, self._live_mode, "partial já tomado (idempotência)"
                )

            child = Position(
                market_id=fresh.market_id,
                outcome=fresh.outcome,
                side=fresh.side,
                status=PositionStatus.CLOSED,
                is_paper=fresh.is_paper,
                size_usd=partial_size,
                avg_entry_price=fresh.avg_entry_price,
                entries_count=1,
                opened_at=fresh.opened_at,
                closed_at=now,
                realized_pnl_usd=partial_pnl,
                followed_wallets=list(fresh.followed_wallets or []),
                notes=ExitReason.TAKE_PROFIT_PARTIAL.value,
            )
            session.add(child)

            fresh.size_usd = fresh.size_usd - partial_size
            fresh.entries_count = fresh.entries_count + 1
            fresh.status = PositionStatus.PARTIALLY_CLOSED
            fresh.notes = _NOTE_PARTIAL_TAKEN
            await session.commit()

        await self._notify_exit(
            ExitReason.TAKE_PROFIT_PARTIAL, position, partial_pnl, current_price
        )
        return ExitResult(
            position_id=position.id,
            market_id=position.market_id,
            outcome=position.outcome,
            exit_reason=ExitReason.TAKE_PROFIT_PARTIAL,
            pnl_usd=partial_pnl,
            is_paper=not self._live_mode,
            skipped=False,
            skip_reason=None,
        )

    # ------------------------------------------------------------------ execution
    async def _submit_live_exit(
        self,
        position: Position,
        current_price: Decimal,
        *,
        fraction: Decimal,
    ) -> None:
        """Submete SELL ao CLOB e espera pelo fill dentro da janela de 60s.

        Raises:
            LiveExitError: se o `ClobClient` não está disponível, `token_id`
                ausente, post_order rejeitado, timeout na janela, ou qualquer
                excepção imprevista. O caller marca a posição como
                `exit_pending_manual` e notifica humano.
        """
        clob_client = self._clob_client_factory() if self._clob_client_factory else None
        if clob_client is None:
            raise LiveExitError("ClobClient indisponível em live mode")
        if not position.token_id:
            raise LiveExitError(
                f"token_id ausente na Position {position.id} — não é possível submeter SELL"
            )
        if position.avg_entry_price <= 0:
            raise LiveExitError(
                f"avg_entry_price inválido na Position {position.id}"
            )
        if current_price <= 0:
            raise LiveExitError(f"current_price inválido: {current_price}")

        from py_clob_client.clob_types import OrderArgs, OrderType

        # Shares detidas = size_usd / avg_entry_price; vendemos `fraction` delas.
        total_shares = position.size_usd / position.avg_entry_price
        shares_to_sell = total_shares * fraction
        if shares_to_sell <= 0:
            raise LiveExitError(
                f"shares_to_sell inválido: {shares_to_sell}"
            )

        limit_price = current_price * _LIVE_EXIT_LIMIT_DISCOUNT
        order_args = OrderArgs(
            token_id=position.token_id,
            price=float(limit_price),
            size=float(shares_to_sell),
            side=TradeSide.SELL.value,
            fee_rate_bps=0,
        )

        log = logger.bind(position_id=position.id, market_id=position.market_id)
        try:
            signed_order = await asyncio.to_thread(
                clob_client.create_order, order_args
            )
            resp = await asyncio.to_thread(
                clob_client.post_order, signed_order, OrderType.GTC
            )
        except Exception as exc:  # noqa: BLE001 — qualquer falha vira LiveExitError
            raise LiveExitError(f"CLOB submit SELL falhou: {exc}") from exc

        resp_dict = resp if isinstance(resp, dict) else {}
        if not resp_dict.get("success", False):
            err = resp_dict.get("errorMsg") or "resposta CLOB sem sucesso"
            raise LiveExitError(f"CLOB rejeitou SELL: {err}")

        order_id = str(resp_dict.get("orderID") or resp_dict.get("order_id") or "")
        if not order_id:
            raise LiveExitError("CLOB respondeu success sem orderID")

        log.info("CLOB SELL submetida (id={}, shares={})", order_id, shares_to_sell)

        filled = await self._wait_for_sell_fill(clob_client, order_id, log=log)
        if not filled:
            await self._cancel_sell_best_effort(clob_client, order_id, log=log)
            raise LiveExitError(f"timeout 60s a aguardar fill — ordem {order_id} cancelada")

    async def _wait_for_sell_fill(
        self, clob_client: Any, order_id: str, *, log
    ) -> bool:
        """Polling a `get_order` de 5s em 5s durante 60s. True se FILLED."""
        for _ in range(_POLL_ITERATIONS):
            await asyncio.sleep(_POLL_STEP_SECONDS)
            try:
                state = await asyncio.to_thread(
                    clob_client.get_order, order_id
                )
            except Exception as exc:  # noqa: BLE001 — não aborta polling
                log.warning("SELL get_order falhou (order={}): {}", order_id, exc)
                continue
            status = _extract_status(state)
            if status == "FILLED":
                return True
            if status in {"CANCELED", "CANCELLED"}:
                log.warning(
                    "SELL cancelada externamente antes de fill (order={})", order_id
                )
                return False
        return False

    @staticmethod
    async def _cancel_sell_best_effort(
        clob_client: Any, order_id: str, *, log
    ) -> None:
        try:
            await asyncio.to_thread(clob_client.cancel, order_id)
            log.warning("SELL cancelada por timeout 60s (order={})", order_id)
        except Exception as exc:  # noqa: BLE001
            log.opt(exception=True).error(
                "falha a cancelar SELL após timeout (order={}): {}",
                order_id,
                exc,
            )

    async def _mark_pending_manual(
        self,
        position: Position,
        reason: ExitReason,
        pnl_usd: Decimal,
        exc: Exception,
    ) -> ExitResult:
        """Falha live — deixa a posição OPEN, marca nota, alerta humano."""
        async with self._sessionmaker() as session:
            fresh = await session.get(Position, position.id)
            if fresh is not None:
                fresh.notes = _NOTE_EXIT_PENDING_MANUAL
                await session.commit()

        msg = f"saída {reason.value} bloqueada (posição {position.id}): {exc}"
        logger.bind(
            position_id=position.id,
            exit_reason=reason.value,
        ).error("exit_manager: {}", msg)
        try:
            await self._notifier.critical_error(error=msg, module="exit_manager")
        except Exception as nexc:  # noqa: BLE001
            logger.warning("exit_manager: falha a notificar pending — {}", nexc)

        return ExitResult(
            position_id=position.id,
            market_id=position.market_id,
            outcome=position.outcome,
            exit_reason=None,
            pnl_usd=pnl_usd,
            is_paper=not self._live_mode,
            skipped=True,
            skip_reason=f"live exit indisponível: {exc}",
        )

    # ------------------------------------------------------------------ hooks
    async def _after_stop_loss(self, pnl_ratio: Decimal) -> None:
        try:
            await self._circuit_breaker.record_stop_loss()
        except Exception as exc:  # noqa: BLE001
            logger.warning("exit_manager: circuit_breaker falhou — {}", exc)
        try:
            await self._notifier.stop_loss_triggered(
                reason="hard stop loss por posição",
                pnl_pct=float(pnl_ratio),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("exit_manager: notifier stop_loss falhou — {}", exc)

    async def _notify_exit(
        self,
        reason: ExitReason,
        position: Position,
        pnl_usd: Decimal,
        current_price: Decimal,
    ) -> None:
        """Notificação best-effort sobre exits que NÃO sejam stop loss (esse é
        notificado em `_after_stop_loss` com detalhe)."""
        if reason == ExitReason.HARD_STOP_LOSS:
            return
        mode = "LIVE" if self._live_mode else "PAPER"
        msg = (
            f"🔚 exit {reason.value} [{mode}] pos={position.id} "
            f"{position.market_id} @ {float(current_price):.3f} "
            f"P&L ${float(pnl_usd):+.2f}"
        )
        try:
            await self._notifier.send(msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("exit_manager: notifier exit falhou — {}", exc)

    # ------------------------------------------------------------------ wallet exit
    async def _detect_wallet_exits(
        self, position: Position
    ) -> list[WalletExitCandidate]:
        """SELLs recentes das wallets seguidas neste mercado.

        Se `signal_reader` foi injectado, chama `poll_sells()` e filtra pelos
        endereços em `position.followed_wallets` e pelo `market_id` da posição.
        `timing_skill_ratio` é lido em seguida da tabela `wallets`.
        """
        if self._signal_reader is None:
            return []
        followed = {addr.lower() for addr in (position.followed_wallets or [])}
        if not followed:
            return []
        try:
            sells = await self._signal_reader.poll_sells()
        except Exception as exc:  # noqa: BLE001 — exits continuam mesmo com falha
            logger.warning("exit_manager: poll_sells falhou — {}", exc)
            return []

        candidates: list[WalletExitCandidate] = []
        for sig in sells:
            if sig.market_id != position.market_id:
                continue
            addr = sig.wallet.address.lower()
            if addr not in followed:
                continue
            skill = await self._wallet_timing_skill(addr)
            candidates.append(
                WalletExitCandidate(
                    wallet_address=addr,
                    timing_skill_ratio=skill if skill is not None else 0.0,
                )
            )
        return candidates

    async def _wallet_timing_skill(self, wallet_address: str) -> float | None:
        """Lê o `timing_skill_ratio` da `Wallet` seguida. Util para subclasses /
        detectores externos que apenas sabem o endereço."""
        async with self._sessionmaker() as session:
            row = await session.get(Wallet, wallet_address.lower())
        if row is None:
            return None
        return row.timing_skill_ratio


# ---------------------------------------------------------------- helpers
def _extract_status(state: Any) -> str | None:
    """Normaliza status de `get_order` — aceita dict ou objecto com `.status`."""
    if isinstance(state, dict):
        value = state.get("status")
    else:
        value = getattr(state, "status", None)
    return str(value).upper() if value is not None else None


def _skipped(
    position: Position, live_mode: bool, reason: str, pnl_usd: Decimal | None = None
) -> ExitResult:
    return ExitResult(
        position_id=position.id,
        market_id=position.market_id,
        outcome=position.outcome,
        exit_reason=None,
        pnl_usd=pnl_usd or Decimal("0"),
        is_paper=not live_mode,
        skipped=True,
        skip_reason=reason,
    )


__all__ = [
    "ExitManager",
    "ExitReason",
    "ExitResult",
    "LiveExitError",
    "WalletExitCandidate",
]
