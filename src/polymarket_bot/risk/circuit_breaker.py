"""Circuit breakers — gestão do estado de pausa/halt (CLAUDE.md §8, §10).

Transições suportadas:

    NORMAL/PAUSED     →  PAUSED    weekly P&L ≤ −15%  (retoma próx. domingo)
    *                 →  HALTED    3 semanas negativas consecutivas
    HALTED/PAUSED     →  NORMAL    `resume_if_due()` com `resumes_at` expirado

O hard stop loss por posição foi removido (pure copy) → RECOVERY-via-SLs
deixou de existir. O sizer continua a respeitar `size_reduction_factor` da
linha actual, que é sempre 1.0 a não ser que seja explicitamente reduzido.

A leitura do estado fica no `WalletMonitor` / `PortfolioManager`; este módulo é
o único ponto de ESCRITA, garantindo invariantes:

- Idempotência: se já HALTED, nova chamada a `check_and_trigger` não cria linha.
- Cada transição origina uma nova linha em `circuit_breaker_state` (historial).
- `consecutive_negative_weeks` é propagado de uma linha para a seguinte.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_bot.config.constants import CONST
from polymarket_bot.db.enums import CircuitBreakerStatus
from polymarket_bot.db.models import CircuitBreakerState
from polymarket_bot.notifications.telegram_notifier import TelegramNotifier

# Threshold semanal — abaixo disto, PAUSED até domingo seguinte.
WEEKLY_STOP_LOSS = float(CONST.WEEKLY_STOP_LOSS)


class CircuitBreaker:
    """API de escrita sobre `CircuitBreakerState`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: TelegramNotifier | None = None,
    ):
        self._sessionmaker = session_factory
        self._notifier = notifier

    # ------------------------------------------------------------------ read
    async def get_status(self) -> CircuitBreakerStatus:
        """Status actual — sem cache, chamado antes de cada trade."""
        async with self._sessionmaker() as session:
            latest = await self._latest(session)
        if latest is None:
            return CircuitBreakerStatus.NORMAL
        # Janela de pausa expirou → tratamos como NORMAL até novo evento.
        resumes = _aware(latest.resumes_at)
        if (
            resumes is not None
            and resumes <= datetime.now(timezone.utc)
            and latest.status in (CircuitBreakerStatus.PAUSED, CircuitBreakerStatus.HALTED)
        ):
            return CircuitBreakerStatus.NORMAL
        return latest.status

    # ------------------------------------------------------------------ weekly gate
    async def check_and_trigger(
        self, weekly_pnl_pct: float
    ) -> CircuitBreakerStatus:
        """Avalia P&L semanal e decide transição (§8). Idempotente se HALTED."""
        async with self._sessionmaker() as session:
            latest = await self._latest(session)
            prev_status = latest.status if latest else CircuitBreakerStatus.NORMAL
            prev_neg_weeks = latest.consecutive_negative_weeks if latest else 0

            # Idempotência: HALTED é terminal até intervenção manual via
            # `resume_if_due()`. Nova chamada não cria linha nem altera estado.
            if prev_status == CircuitBreakerStatus.HALTED:
                return CircuitBreakerStatus.HALTED

            # Propaga contador de semanas negativas consecutivas.
            if weekly_pnl_pct < 0:
                neg_weeks = prev_neg_weeks + 1
            else:
                neg_weeks = 0

            # Decisão de status — ordem de precedência importa.
            if neg_weeks >= CONST.HALT_AFTER_NEGATIVE_WEEKS:
                new_status = CircuitBreakerStatus.HALTED
                reason = f"{neg_weeks} semanas negativas consecutivas"
                resumes_at = None
                size_factor = 0.0
            elif weekly_pnl_pct <= WEEKLY_STOP_LOSS:
                new_status = CircuitBreakerStatus.PAUSED
                reason = (
                    f"weekly P&L {weekly_pnl_pct:+.1%} ≤ {WEEKLY_STOP_LOSS:+.0%}"
                )
                resumes_at = _next_sunday_2330_utc()
                size_factor = 0.0
            else:
                # Fora dos thresholds → NORMAL. RECOVERY já não é entrado
                # automaticamente (era disparado por SLs consecutivos, agora
                # removidos). Linhas RECOVERY historic ainda podem existir;
                # tratamos como NORMAL para efeito de sizing.
                new_status = CircuitBreakerStatus.NORMAL
                reason = f"weekly P&L {weekly_pnl_pct:+.1%}"
                resumes_at = None
                size_factor = 1.0

            # Cada check-in semanal persiste o estado (counter evolui).
            await self._persist_weekly_check(
                session=session,
                prev_latest=latest,
                new_status=new_status,
                reason=reason,
                resumes_at=resumes_at,
                consecutive_negative_weeks=neg_weeks,
                size_reduction_factor=size_factor,
            )
            await session.commit()

        if new_status != prev_status:
            await self._notify_transition(prev_status, new_status, reason)
        return new_status

    # ------------------------------------------------------------------ resume
    async def resume_if_due(self) -> None:
        """Se o status actual for PAUSED e `resumes_at` já passou → NORMAL."""
        now = datetime.now(timezone.utc)
        async with self._sessionmaker() as session:
            latest = await self._latest(session)
            if latest is None:
                return
            if latest.status != CircuitBreakerStatus.PAUSED:
                return
            resumes = _aware(latest.resumes_at)
            if resumes is None or resumes > now:
                return

            await self._transition_if_changed(
                session=session,
                prev_status=latest.status,
                new_status=CircuitBreakerStatus.NORMAL,
                reason="pausa expirada — retoma automática",
                resumes_at=None,
                consecutive_negative_weeks=latest.consecutive_negative_weeks,
                size_reduction_factor=1.0,
            )
            await session.commit()

        await self._notify_transition(
            CircuitBreakerStatus.PAUSED,
            CircuitBreakerStatus.NORMAL,
            "pausa expirada",
        )

    # ------------------------------------------------------------------ internals
    @staticmethod
    async def _latest(
        session: AsyncSession,
    ) -> CircuitBreakerState | None:
        stmt = (
            select(CircuitBreakerState)
            .order_by(desc(CircuitBreakerState.triggered_at))
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def _persist_weekly_check(
        *,
        session: AsyncSession,
        prev_latest: CircuitBreakerState | None,
        new_status: CircuitBreakerStatus,
        reason: str,
        resumes_at: datetime | None,
        consecutive_negative_weeks: int,
        size_reduction_factor: float,
    ) -> None:
        """Persiste o resultado do check semanal.

        Se o status mudou → sempre cria nova linha.
        Se manteve-se: atualiza contador in-place na linha existente para não
        poluir o histórico com linhas redundantes. Se não existia linha e
        entramos em NORMAL com contador 0, não persiste (estado baseline).
        """
        prev_status = (
            prev_latest.status if prev_latest else CircuitBreakerStatus.NORMAL
        )

        if new_status != prev_status:
            session.add(
                CircuitBreakerState(
                    status=new_status,
                    reason=reason[:300],
                    triggered_at=datetime.now(timezone.utc),
                    resumes_at=resumes_at,
                    consecutive_negative_weeks=consecutive_negative_weeks,
                    size_reduction_factor=size_reduction_factor,
                )
            )
            await session.flush()
            return

        # Mesmo status. Se não há linha e o baseline é NORMAL+0, não precisa persistir.
        if prev_latest is None:
            if consecutive_negative_weeks == 0:
                return
            # Precisamos persistir o contador — cria linha baseline.
            session.add(
                CircuitBreakerState(
                    status=new_status,
                    reason=reason[:300],
                    triggered_at=datetime.now(timezone.utc),
                    resumes_at=resumes_at,
                    consecutive_negative_weeks=consecutive_negative_weeks,
                    size_reduction_factor=size_reduction_factor,
                )
            )
            await session.flush()
            return

        # Atualização in-place do contador — evita histórico redundante.
        prev_latest.consecutive_negative_weeks = consecutive_negative_weeks
        prev_latest.size_reduction_factor = size_reduction_factor
        prev_latest.reason = reason[:300]
        await session.flush()

    @staticmethod
    async def _transition_if_changed(
        *,
        session: AsyncSession,
        prev_status: CircuitBreakerStatus,
        new_status: CircuitBreakerStatus,
        reason: str,
        resumes_at: datetime | None,
        consecutive_negative_weeks: int,
        size_reduction_factor: float,
    ) -> bool:
        """Cria nova linha se status mudou (ou não há histórico). Idempotente."""
        if new_status == prev_status:
            latest = (
                await session.execute(
                    select(CircuitBreakerState).order_by(
                        desc(CircuitBreakerState.triggered_at)
                    ).limit(1)
                )
            ).scalar_one_or_none()
            # Preservar contador se só metadata mudou — apenas update leve para
            # `consecutive_negative_weeks` quando a propagação semanal dá números
            # diferentes. Evita nova linha por ruído.
            if (
                latest is not None
                and latest.consecutive_negative_weeks != consecutive_negative_weeks
            ):
                latest.consecutive_negative_weeks = consecutive_negative_weeks
                latest.size_reduction_factor = size_reduction_factor
                await session.flush()
            return False

        row = CircuitBreakerState(
            status=new_status,
            reason=reason[:300],
            triggered_at=datetime.now(timezone.utc),
            resumes_at=resumes_at,
            consecutive_negative_weeks=consecutive_negative_weeks,
            size_reduction_factor=size_reduction_factor,
        )
        session.add(row)
        await session.flush()
        return True

    async def _notify_transition(
        self,
        prev: CircuitBreakerStatus,
        new: CircuitBreakerStatus,
        reason: str,
    ) -> None:
        if self._notifier is None:
            return
        try:
            if new == CircuitBreakerStatus.PAUSED:
                await self._notifier.stop_loss_triggered(
                    reason=f"semana parou ({reason})",
                    pnl_pct=float(CONST.WEEKLY_STOP_LOSS),
                )
            elif new == CircuitBreakerStatus.HALTED:
                await self._notifier.critical_error(
                    error=f"HALTED — {reason}", module="circuit_breaker"
                )
            elif new == CircuitBreakerStatus.RECOVERY:
                await self._notifier.critical_error(
                    error=f"RECOVERY — {reason}", module="circuit_breaker"
                )
            elif new == CircuitBreakerStatus.NORMAL:
                logger.info(
                    "circuit_breaker: transição {} → NORMAL ({})",
                    prev.value,
                    reason,
                )
        except Exception as exc:  # noqa: BLE001 — notifier deve ser tolerante
            logger.warning("circuit_breaker: falha a notificar — {}", exc)


# ---------------------------------------------------------------- helpers
def _aware(dt: datetime | None) -> datetime | None:
    """SQLite devolve datetimes naive — renormaliza para UTC-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _next_sunday_2330_utc(now: datetime | None = None) -> datetime:
    """Próximo domingo às 23:30 UTC (CLAUDE.md §11 — rebalanceamento)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # weekday(): Mon=0, Sun=6
    days_ahead = (6 - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=23, minute=30, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=7)
    return target
