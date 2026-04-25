"""Detecção de novas trades nas wallets seguidas.

O reader mantém cursores `(last_seen_at, seen_tx_hashes)` em memória por wallet.
Na primeira execução o cursor é `now()` — não reprocessa histórico antigo.

- `poll_once()` devolve BUYs novos desde o cursor.
- `poll_sells()` devolve SELLs novos desde o cursor.

Ambos partilham o mesmo cursor + set de `tx_hash` por wallet, pelo que uma trade
vista numa das duas funções NUNCA reaparece noutra (dedup partilhado).

Concurrency: um `asyncio.Lock` por wallet garante que polls paralelos à mesma
wallet não emitem duplicados nem corrompem o cursor.

Modos de operação (`source`):
- `"api"`     — só o polling à Data API (comportamento histórico).
- `"chain"`   — só o `ChainWatcher` injecta sinais via `inject_signal()`.
                Não toca na Data API.
- `"both"`    — chain como fonte primária + Data API como verificação/fallback.
                O dedup por `tx_hash` garante que sinais já vistos via chain
                não reaparecem via API e vice-versa.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from loguru import logger

from polymarket_bot.api.polymarket_data import PolymarketDataClient, RawTrade
from polymarket_bot.db.enums import TradeSide, WalletTier


SignalSource = Literal["api", "chain", "both"]


@dataclass(frozen=True)
class FollowedWallet:
    """Wallet seguida + métricas do último scoring guardado em DB."""

    address: str
    tier: WalletTier
    win_rate: float


@dataclass(frozen=True)
class DetectedSignal:
    """Trade nova detectada numa wallet seguida. Default `side=BUY` mantém
    retrocompatibilidade — `poll_sells()` preenche `side=SELL`."""

    wallet: FollowedWallet
    market_id: str
    outcome: str
    price: Decimal
    detected_at: datetime
    tx_hash: str
    side: TradeSide = TradeSide.BUY


@dataclass
class _WalletCursor:
    last_seen_at: datetime
    seen_tx_hashes: set[str]
    lock: asyncio.Lock


class SignalReader:
    """Lê trades das wallets seguidas e emite `DetectedSignal` para BUYs novas."""

    # Cap no set de hashes por wallet para não crescer sem limites em sessões longas.
    _MAX_HASHES_PER_WALLET = 1000

    def __init__(
        self,
        wallets: list[FollowedWallet],
        data_client: PolymarketDataClient,
        source: SignalSource = "api",
    ):
        self._data_client = data_client
        self._source = source
        self._wallets: dict[str, FollowedWallet] = {}
        self._cursors: dict[str, _WalletCursor] = {}
        # Buffers preenchidos pelo `ChainWatcher` via `inject_signal()`. Drenados
        # em cada `poll_once`/`poll_sells`; consenso natural se múltiplos sinais
        # chegarem entre poll ticks.
        self._chain_buys: list[DetectedSignal] = []
        self._chain_sells: list[DetectedSignal] = []
        self.replace_wallets(wallets)

    @property
    def source(self) -> SignalSource:
        return self._source

    def replace_wallets(self, wallets: list[FollowedWallet]) -> None:
        """Substitui o universo seguido. Cursores de wallets removidas são descartados;
        cursores de wallets existentes são preservados."""
        now = datetime.now(timezone.utc)
        new_map = {w.address.lower(): w for w in wallets}
        self._wallets = new_map

        # Remover cursores obsoletos.
        for addr in list(self._cursors.keys()):
            if addr not in new_map:
                self._cursors.pop(addr, None)

        # Inicializar cursores para novas wallets.
        for addr in new_map:
            if addr not in self._cursors:
                self._cursors[addr] = _WalletCursor(
                    last_seen_at=now,
                    seen_tx_hashes=set(),
                    lock=asyncio.Lock(),
                )

    async def poll_once(self) -> list[DetectedSignal]:
        """Uma passagem: lê trades BUY de todas as wallets desde `last_seen_at`."""
        return await self._poll_all(side=TradeSide.BUY)

    async def poll_sells(self) -> list[DetectedSignal]:
        """Igual a `poll_once` mas filtra `side == SELL`.

        Usado pelo ExitManager para wallet exit detection. Partilha cursor +
        dedup por wallet com `poll_once` — um `tx_hash` visto num lado NUNCA
        reaparece no outro.
        """
        return await self._poll_all(side=TradeSide.SELL)

    async def _poll_all(self, *, side: TradeSide) -> list[DetectedSignal]:
        # Drena o buffer alimentado pelo ChainWatcher antes de tocar na API.
        chain_signals = self._drain_chain_buffer(side=side)

        # Em modo `chain`, NUNCA toca na Data API — evita lag artificial e
        # desperdício de quota.
        if self._source == "chain":
            api_signals: list[DetectedSignal] = []
        else:
            results: list[list[DetectedSignal]] = await asyncio.gather(
                *(self._poll_wallet(addr, side=side) for addr in self._wallets),
                return_exceptions=False,
            )
            api_signals = [s for batch in results for s in batch]

        signals = chain_signals + api_signals
        if signals:
            logger.info(
                "signal_reader: {} {} sinal(is) novo(s) "
                "(chain={}, api={}, source={})",
                len(signals), side.value,
                len(chain_signals), len(api_signals), self._source,
            )
        return signals

    def _drain_chain_buffer(self, *, side: TradeSide) -> list[DetectedSignal]:
        if side == TradeSide.BUY:
            buf, self._chain_buys = self._chain_buys, []
        else:
            buf, self._chain_sells = self._chain_sells, []
        return buf

    def inject_signal(self, signal: DetectedSignal) -> None:
        """Buffer-push usado pelo `ChainWatcher`. Idempotente por `tx_hash`.

        Ignora sinais de wallets que não estão actualmente seguidas — pode
        acontecer se o ChainWatcher ainda não recebeu um update após
        rebalanceamento.
        """
        addr = signal.wallet.address.lower()
        cursor = self._cursors.get(addr)
        if cursor is None:
            return
        if signal.tx_hash and signal.tx_hash in cursor.seen_tx_hashes:
            return
        if signal.tx_hash:
            cursor.seen_tx_hashes.add(signal.tx_hash)
        if signal.detected_at > cursor.last_seen_at:
            cursor.last_seen_at = signal.detected_at
        if signal.side == TradeSide.BUY:
            self._chain_buys.append(signal)
        else:
            self._chain_sells.append(signal)

    async def _poll_wallet(
        self, address: str, *, side: TradeSide
    ) -> list[DetectedSignal]:
        wallet = self._wallets[address]
        cursor = self._cursors[address]
        async with cursor.lock:
            since = cursor.last_seen_at
            log = logger.bind(
                wallet=address, since=since.isoformat(), side=side.value
            )
            try:
                trades = await self._data_client.get_wallet_trades(
                    wallet_address=address, since=since
                )
            except Exception as exc:  # noqa: BLE001 — loop must keep running
                log.warning("signal_reader: falha a ler trades — {}", exc)
                return []

            emitted: list[DetectedSignal] = []
            max_ts = since
            for trade in trades:
                if trade.side != side:
                    continue
                if not trade.tx_hash or trade.tx_hash in cursor.seen_tx_hashes:
                    continue
                if trade.executed_at < since:
                    continue

                emitted.append(
                    DetectedSignal(
                        wallet=wallet,
                        market_id=trade.market_id,
                        outcome=(trade.outcome or "YES").upper(),
                        price=trade.price,
                        detected_at=trade.executed_at,
                        tx_hash=trade.tx_hash,
                        side=trade.side,
                    )
                )
                cursor.seen_tx_hashes.add(trade.tx_hash)
                if trade.executed_at > max_ts:
                    max_ts = trade.executed_at

            # Avança o cursor só se viu algo mais recente; caso contrário mantém.
            if max_ts > cursor.last_seen_at:
                cursor.last_seen_at = max_ts

            # Trim do set de hashes se crescer demasiado — mantemos os mais recentes.
            if len(cursor.seen_tx_hashes) > self._MAX_HASHES_PER_WALLET:
                # Sem ordem temporal no set — descartamos metade arbitrariamente;
                # combinado com `last_seen_at` isto é seguro para dedup.
                cursor.seen_tx_hashes = set(
                    list(cursor.seen_tx_hashes)[self._MAX_HASHES_PER_WALLET // 2 :]
                )

            return emitted

    # ----- helpers expostos para o monitor / testes -----
    @property
    def followed_addresses(self) -> list[str]:
        return list(self._wallets.keys())

    def get_cursor(self, address: str) -> datetime | None:
        cursor = self._cursors.get(address.lower())
        return cursor.last_seen_at if cursor else None

    def _seen_hashes(self, address: str) -> set[str]:
        """Helper para testes — expõe set interno (não usar em produção)."""
        cursor = self._cursors.get(address.lower())
        return cursor.seen_tx_hashes if cursor else set()
