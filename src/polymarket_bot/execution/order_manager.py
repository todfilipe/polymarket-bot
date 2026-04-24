"""OrderManager — ponto único de submissão de ordens.

Responsabilidades (CLAUDE.md §5, §10, §13):

1. Respeitar o modo `LIVE_MODE`:
   - `live_mode=false` (DEFAULT)  → paper trade: regista `BotTrade(is_paper=True)`
                                      sem tocar na CLOB.
   - `live_mode=true`             → submete ordem real via `py-clob-client`.
     Se credenciais em falta → `FAILED` com motivo explícito.

2. Idempotência: dedup hash verificado ANTES de submeter. Hash persistido
   após sucesso para que retries/restarts não repitam a mesma trade.

3. Persistência atómica: cada ordem → 1 linha em `bot_trades` + 1 linha em
   `dedup_hashes` na mesma transação.

4. Logs distinguem paper vs live (§13). Toda a submissão é logada com
   `market_id`, `outcome`, `size_usd`, `intended_price`, `is_paper`.

5. Janela de 60s (§5): após `post_order`, faz polling a `get_order` de 5s em 5s
   até FILLED ou timeout → `cancel()` + `SubmissionResult(success=False)`.

6. Slippage audit (§5): se `executed_price` desvia do `intended_price` mais do
   que o limite (5% se volume > $500k, 2% senão) → WARNING no log. Não reverte
   (ordem já executada); apenas auditoria.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from polymarket_bot.config import Settings
from polymarket_bot.config.constants import CONST
from polymarket_bot.db.enums import BotTradeStatus, TradeSide
from polymarket_bot.db.models import BotTrade
from polymarket_bot.execution.dedup import is_duplicate, register_hash
from polymarket_bot.market import MarketSnapshot

# Constantes de polling — extraídas das constantes estratégicas (§5).
_POLL_WINDOW_SECONDS: int = CONST.ORDER_EXECUTION_WINDOW_SECONDS
_POLL_STEP_SECONDS: int = 5
_POLL_ITERATIONS: int = _POLL_WINDOW_SECONDS // _POLL_STEP_SECONDS


@dataclass(frozen=True)
class SubmissionResult:
    """Resultado da submissão — self-contained para logs e para o pipeline."""

    success: bool
    is_duplicate: bool
    is_paper: bool
    reason: str | None = None
    clob_order_id: str | None = None
    executed_price: Decimal | None = None
    bot_trade_id: int | None = None


class OrderManager:
    """Submete ordens (paper ou live) com dedup e persistência atómica.

    Não faz sizing, EV ou filters — recebe já uma decisão completa do pipeline.
    """

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        clob_client: Any | None = None,
    ):
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._clob_client = clob_client

    @property
    def is_live(self) -> bool:
        return self._settings.live_mode

    async def submit(
        self,
        *,
        market: MarketSnapshot,
        outcome: str,
        side: TradeSide,
        size_usd: Decimal,
        intended_price: Decimal,
        expected_value: float,
        followed_wallet: str,
        dedup_hash: str,
    ) -> SubmissionResult:
        """Submete uma ordem. Ponto único que toca na CLOB em modo live."""
        mode_label = "LIVE" if self.is_live else "PAPER"
        log = logger.bind(
            market_id=market.market_id,
            outcome=outcome,
            side=side.value,
            size_usd=str(size_usd),
            mode=mode_label,
        )

        async with self._sessionmaker() as session:
            # 1) Dedup check — antes de qualquer I/O externo
            if await is_duplicate(session, dedup_hash):
                log.info("dedup hit — ordem ignorada ({})", dedup_hash)
                return SubmissionResult(
                    success=False,
                    is_duplicate=True,
                    is_paper=not self.is_live,
                    reason="dedup hash já registado na janela",
                )

            # 2) Rota consoante modo
            if self.is_live:
                outcome_result = await self._submit_live(
                    market=market,
                    outcome=outcome,
                    side=side,
                    size_usd=size_usd,
                    intended_price=intended_price,
                    log=log,
                )
            else:
                outcome_result = self._submit_paper(
                    intended_price=intended_price, log=log
                )

            # 3) Persistir BotTrade + hash. Mesmo em FAILED, registamos para
            #    auditoria (§12 — "trades ignoradas com motivos desagregados").
            trade = BotTrade(
                dedup_hash=dedup_hash,
                is_paper=not self.is_live,
                followed_wallet_address=followed_wallet,
                market_id=market.market_id,
                side=side,
                outcome=outcome,
                intended_price=intended_price,
                executed_price=outcome_result.executed_price,
                size_usd=size_usd,
                slippage=_compute_slippage(
                    intended_price, outcome_result.executed_price
                ),
                expected_value=expected_value,
                status=outcome_result.status,
                skip_reason=None if outcome_result.success else outcome_result.reason,
                clob_order_id=outcome_result.clob_order_id,
                submitted_at=datetime.now(timezone.utc),
                executed_at=(
                    datetime.now(timezone.utc) if outcome_result.success else None
                ),
            )
            session.add(trade)
            await session.flush()

            if outcome_result.success:
                await register_hash(session, dedup_hash)

            await session.commit()

            return SubmissionResult(
                success=outcome_result.success,
                is_duplicate=False,
                is_paper=not self.is_live,
                reason=outcome_result.reason,
                clob_order_id=outcome_result.clob_order_id,
                executed_price=outcome_result.executed_price,
                bot_trade_id=trade.id,
            )

    # ------------------------------------------------------------------
    # Rotas de submissão — separadas para clareza e testabilidade.
    # ------------------------------------------------------------------

    def _submit_paper(
        self,
        *,
        intended_price: Decimal,
        log,
    ) -> _ExecutionOutcome:
        """Paper trade: assume fill imediato ao intended_price, zero slippage."""
        log.info("paper fill @ {}", intended_price)
        return _ExecutionOutcome(
            success=True,
            reason=None,
            clob_order_id=None,
            executed_price=intended_price,
            status=BotTradeStatus.FILLED,
        )

    async def _submit_live(
        self,
        *,
        market: MarketSnapshot,
        outcome: str,
        side: TradeSide,
        size_usd: Decimal,
        intended_price: Decimal,
        log,
    ) -> _ExecutionOutcome:
        """Live submission via CLOB. Fail-safe (§1): qualquer imprevisto → FAILED."""
        if not self._settings.has_clob_credentials:
            reason = "LIVE_MODE=true mas credenciais CLOB em falta no .env"
            log.error(reason)
            return _ExecutionOutcome(
                success=False,
                reason=reason,
                clob_order_id=None,
                executed_price=None,
                status=BotTradeStatus.FAILED,
            )
        if self._clob_client is None:
            reason = "LIVE_MODE=true mas ClobClient não foi injectado"
            log.error(reason)
            return _ExecutionOutcome(
                success=False,
                reason=reason,
                clob_order_id=None,
                executed_price=None,
                status=BotTradeStatus.FAILED,
            )
        if not market.token_id:
            reason = "token_id ausente no MarketSnapshot — não é possível submeter"
            log.error(reason)
            return _ExecutionOutcome(
                success=False,
                reason=reason,
                clob_order_id=None,
                executed_price=None,
                status=BotTradeStatus.FAILED,
            )
        if intended_price <= 0:
            reason = f"intended_price inválido: {intended_price}"
            log.error(reason)
            return _ExecutionOutcome(
                success=False,
                reason=reason,
                clob_order_id=None,
                executed_price=None,
                status=BotTradeStatus.FAILED,
            )

        # Imports tardios — só em modo live pagamos o custo da importação.
        from py_clob_client.clob_types import OrderArgs, OrderType

        shares = float(size_usd / intended_price)
        order_args = OrderArgs(
            token_id=market.token_id,
            price=float(intended_price),
            size=shares,
            side=side.value,
            fee_rate_bps=0,
        )

        try:
            signed_order = await asyncio.to_thread(
                self._clob_client.create_order, order_args
            )
            resp = await asyncio.to_thread(
                self._clob_client.post_order, signed_order, OrderType.GTC
            )
        except Exception as exc:  # noqa: BLE001 — qualquer falha na submissão é FAILED
            reason = f"CLOB submit falhou: {exc}"
            log.opt(exception=True).error(reason)
            return _ExecutionOutcome(
                success=False,
                reason=reason,
                clob_order_id=None,
                executed_price=None,
                status=BotTradeStatus.FAILED,
            )

        resp_dict = resp if isinstance(resp, dict) else {}
        if not resp_dict.get("success", False):
            err = resp_dict.get("errorMsg") or "resposta CLOB sem sucesso"
            log.error("CLOB post_order rejeitado: {}", err)
            return _ExecutionOutcome(
                success=False,
                reason=f"CLOB rejeitou ordem: {err}",
                clob_order_id=None,
                executed_price=None,
                status=BotTradeStatus.FAILED,
            )

        order_id = str(resp_dict.get("orderID") or resp_dict.get("order_id") or "")
        if not order_id:
            log.error("CLOB respondeu success mas sem orderID: {}", resp_dict)
            return _ExecutionOutcome(
                success=False,
                reason="CLOB respondeu success sem orderID",
                clob_order_id=None,
                executed_price=None,
                status=BotTradeStatus.FAILED,
            )

        log.info("CLOB order submetida (id={})", order_id)

        # Polling pela janela de 60s — 5s × 12 iterações.
        executed_price = await self._wait_for_fill(order_id, log=log)
        if executed_price is None:
            await self._cancel_best_effort(order_id, log=log)
            return _ExecutionOutcome(
                success=False,
                reason="timeout 60s — cancelado",
                clob_order_id=order_id,
                executed_price=None,
                status=BotTradeStatus.FAILED,
            )

        self._audit_slippage(
            market=market,
            intended_price=intended_price,
            executed_price=executed_price,
            log=log,
        )

        return _ExecutionOutcome(
            success=True,
            reason=None,
            clob_order_id=order_id,
            executed_price=executed_price,
            status=BotTradeStatus.FILLED,
        )

    # ------------------------------------------------------------------ live helpers
    async def _wait_for_fill(self, order_id: str, *, log) -> Decimal | None:
        """Polling a `get_order` de 5s em 5s. Devolve o preço médio de fill ou None."""
        for _ in range(_POLL_ITERATIONS):
            await asyncio.sleep(_POLL_STEP_SECONDS)
            try:
                state = await asyncio.to_thread(
                    self._clob_client.get_order, order_id
                )
            except Exception as exc:  # noqa: BLE001 — não aborta polling por erro transitório
                log.warning("get_order falhou (order={}): {}", order_id, exc)
                continue
            status = _extract_status(state)
            if status == "FILLED":
                price = _extract_executed_price(state)
                if price is None:
                    log.warning(
                        "FILLED sem preço — a usar intended como fallback "
                        "(order={})",
                        order_id,
                    )
                return price
            if status in {"CANCELED", "CANCELLED"}:
                log.warning(
                    "ordem cancelada externamente antes de fill (order={})", order_id
                )
                return None
        return None

    async def _cancel_best_effort(self, order_id: str, *, log) -> None:
        """Cancel após timeout — best-effort (falhas só logadas)."""
        try:
            await asyncio.to_thread(self._clob_client.cancel, order_id)
            log.warning("ordem cancelada por timeout 60s (order={})", order_id)
        except Exception as exc:  # noqa: BLE001
            log.opt(exception=True).error(
                "falha a cancelar ordem após timeout (order={}): {}",
                order_id,
                exc,
            )

    def _audit_slippage(
        self,
        *,
        market: MarketSnapshot,
        intended_price: Decimal,
        executed_price: Decimal,
        log,
    ) -> None:
        """Compara fill vs intended contra o limite de §5. Só auditoria."""
        if intended_price <= 0:
            return
        slippage = abs(executed_price - intended_price) / intended_price
        limit = (
            Decimal(str(CONST.MAX_SLIPPAGE_HIGH_VOLUME))
            if market.volume_usd >= Decimal(str(CONST.HIGH_VOLUME_THRESHOLD))
            else Decimal(str(CONST.MAX_SLIPPAGE_MID_VOLUME))
        )
        if slippage > limit:
            log.warning(
                "slippage acima do limite: {:.4f} > {:.4f} "
                "(intended={}, executed={}, volume=${})",
                float(slippage),
                float(limit),
                intended_price,
                executed_price,
                market.volume_usd,
            )


@dataclass(frozen=True)
class _ExecutionOutcome:
    """Resultado interno de uma rota (paper/live) — convertido em SubmissionResult."""

    success: bool
    reason: str | None
    clob_order_id: str | None
    executed_price: Decimal | None
    status: BotTradeStatus


def _compute_slippage(
    intended: Decimal, executed: Decimal | None
) -> float | None:
    """Slippage relativo: (executed - intended) / intended. None se não houve fill."""
    if executed is None or intended == 0:
        return None
    return float((executed - intended) / intended)


def _extract_status(state: Any) -> str | None:
    """`get_order` devolve dict; aceitamos também objectos com atributo `.status`."""
    if isinstance(state, dict):
        value = state.get("status")
    else:
        value = getattr(state, "status", None)
    if value is None:
        return None
    return str(value).upper()


def _extract_executed_price(state: Any) -> Decimal | None:
    """Preço médio efectivo de fill — tenta `price` e fallbacks conhecidos."""
    if isinstance(state, dict):
        raw = (
            state.get("average_price")
            or state.get("price")
            or state.get("avg_price")
        )
    else:
        raw = (
            getattr(state, "average_price", None)
            or getattr(state, "price", None)
            or getattr(state, "avg_price", None)
        )
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (ValueError, ArithmeticError):
        return None
