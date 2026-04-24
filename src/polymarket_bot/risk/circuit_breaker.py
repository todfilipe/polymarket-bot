"""Circuit breakers — gestão do estado de pausa/halt (CLAUDE.md §8, §10).

Transições suportadas:

    NORMAL            →  PAUSED    weekly P&L ≤ −15%  (retoma próx. domingo 23:30)
    NORMAL/PAUSED     →  RECOVERY  2 stop losses consecutivos (sizing ×0.6 no sizer)
    *                 →  HALTED    3 semanas negativas consecutivas
    HALTED/PAUSED     →  NORMAL    `resume_if_due()` com `resumes_at` expirado
    Qualquer win      →  reset do contador de losses consecutivos (e, se estava em
                                   RECOVERY apenas por losses, volta para NORMAL)

A leitura do estado fica no `WalletMonitor` / `PortfolioManager`; este módulo é
o único ponto de ESCRITA, garantindo invariantes:

- Idempotência: se já HALTED, nova chamada a `check_and_trigger` não cria linha.
- Cada transição origina uma nova linha em `circuit_breaker_state` (historial).
- `consecutive_negative_weeks` é propagado de uma linha para a seguinte.
- `consecutive_stop_losses` não tem coluna dedicada — é derivado de `Position`
  fechadas após o último reset (ver `_count_consecutive_stop_losses`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_bot.config.constants import CONST
from polymarket_bot.db.enums import CircuitBreakerStatus, PositionStatus
from polymarket_bot.db.models import CircuitBreakerState, Position
from polymarket_bot.notifications.telegram_notifier import TelegramNotifier

# Hard stop loss por posição (§7) — −40% do investido.
HARD_STOP_LOSS_RATIO = Decimal(str(CONST.HARD_STOP_LOSS_PER_POSITION))

# Threshold semanal — abaixo disto, PAUSED até domingo seguinte.
WEEKLY_STOP_LOSS = float(CONST.WEEKLY_STOP_LOSS)

# Reduções de sizing aplicadas pelo sizer quando em RECOVERY.
RECOVERY_SIZE_REDUCTION = float(CONST.RECOVERY_MULTIPLIER)


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
                # Fora dos thresholds — preserva RECOVERY se já activo, senão NORMAL.
                new_status = (
                    CircuitBreakerStatus.RECOVERY
                    if prev_status == CircuitBreakerStatus.RECOVERY
                    else CircuitBreakerStatus.NORMAL
                )
                reason = f"weekly P&L {weekly_pnl_pct:+.1%}"
                resumes_at = None
                size_factor = (
                    RECOVERY_SIZE_REDUCTION
                    if new_status == CircuitBreakerStatus.RECOVERY
                    else 1.0
                )

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

    # ------------------------------------------------------------------ stop losses
    async def record_stop_loss(self) -> None:
        """Regista que uma posição fechou em stop loss. Ao 2.º → RECOVERY."""
        async with self._sessionmaker() as session:
            latest = await self._latest(session)
            prev_status = latest.status if latest else CircuitBreakerStatus.NORMAL
            prev_neg_weeks = latest.consecutive_negative_weeks if latest else 0

            consecutive = await self._count_consecutive_stop_losses(session)
            log = logger.bind(consecutive_stop_losses=consecutive)

            if consecutive < 2:
                log.info(
                    "circuit_breaker: stop loss registado ({}/2)", consecutive
                )
                return

            if prev_status == CircuitBreakerStatus.HALTED:
                # HALTED é mais severo — não fazemos downgrade.
                return
            if prev_status == CircuitBreakerStatus.RECOVERY:
                return  # já em recovery

            await self._transition_if_changed(
                session=session,
                prev_status=prev_status,
                new_status=CircuitBreakerStatus.RECOVERY,
                reason=f"{consecutive} stop losses consecutivos",
                resumes_at=None,
                consecutive_negative_weeks=prev_neg_weeks,
                size_reduction_factor=RECOVERY_SIZE_REDUCTION,
            )
            await session.commit()

        await self._notify_transition(
            prev_status,
            CircuitBreakerStatus.RECOVERY,
            f"{consecutive} stop losses consecutivos",
        )

    async def reset_consecutive_losses(self) -> None:
        """Após uma trade vencedora: se estávamos em RECOVERY *por causa disso*,
        voltamos a NORMAL. O contador efectivo vive em `Position` — o reset aqui
        traduz-se em promover o status para NORMAL se apropriado."""
        async with self._sessionmaker() as session:
            latest = await self._latest(session)
            if latest is None or latest.status != CircuitBreakerStatus.RECOVERY:
                return
            # Preserva contador de semanas; apenas tira o flag RECOVERY.
            await self._transition_if_changed(
                session=session,
                prev_status=CircuitBreakerStatus.RECOVERY,
                new_status=CircuitBreakerStatus.NORMAL,
                reason="trade vencedora — reset consecutive losses",
                resumes_at=None,
                consecutive_negative_weeks=latest.consecutive_negative_weeks,
                size_reduction_factor=1.0,
            )
            await session.commit()

        await self._notify_transition(
            CircuitBreakerStatus.RECOVERY,
            CircuitBreakerStatus.NORMAL,
            "reset após trade vencedora",
        )

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

    @staticmethod
    async def _count_consecutive_stop_losses(session: AsyncSession) -> int:
        """Conta as últimas posições fechadas de trás para a frente até apanhar
        uma vencedora (ou acabar). Define "stop loss" como P&L ≤ threshold
        (−40% do size)."""
        stmt = (
            select(Position)
            .where(
                Position.status == PositionStatus.CLOSED,
                Position.realized_pnl_usd.is_not(None),
            )
            .order_by(desc(Position.closed_at))
            .limit(20)  # janela generosa para evitar re-scan
        )
        rows = (await session.execute(stmt)).scalars().all()
        count = 0
        for pos in rows:
            if pos.realized_pnl_usd is None or pos.size_usd <= 0:
                continue
            ratio = pos.realized_pnl_usd / pos.size_usd
            if ratio <= HARD_STOP_LOSS_RATIO:
                count += 1
                continue
            if pos.realized_pnl_usd > 0:
                break  # apanhou um win → corta streak
            # Perda sem atingir hard stop: não conta como stop-loss, mas também
            # não quebra a streak (pode ter sido exit por wallet em perda).
            continue
        return count

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
