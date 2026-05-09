"""Detecção em tempo real de trades via Polygon RPC.

Substitui o polling à Polymarket Data API (1h+ de lag) por leitura directa
dos contratos Polymarket Exchange V2 em Polygon. Cada trade emite um
`OrderFilled` com `maker`/`taker` indexados — filtramos pelos endereços
(proxy wallets) das wallets seguidas.

Modos de operação:
- WebSocket (`eth_subscribe('logs', ...)`) — deteção sub-segundo, preferido.
- HTTP polling (`eth_getLogs` a cada 15s) — fallback automático se o WS cair.

Após restart, processa eventos dos últimos 5 minutos via `eth_getLogs` para
não perder trades que ocorreram durante o downtime.

Decoding do evento (sem dependência adicional de ABI):
- assinatura: ``OrderFilled(bytes32,address,address,uint8,uint256,uint256,
                            uint256,uint256,bytes32,bytes32)``
- topic[0] = keccak256(assinatura)
- topic[1] = orderHash (bytes32)
- topic[2] = maker (address, padded 32 bytes)
- topic[3] = taker (address, padded 32 bytes)
- data    = side (uint8 padded), tokenId, makerAmountFilled, takerAmountFilled,
            fee, builder (bytes32), metadata (bytes32) — 7 × 32 bytes

Side / token / price:
- ``side`` na posição 0 do data é o lado do maker (0 = BUY, 1 = SELL).
- **Filtragem só por maker.** Quando a wallet seguida aparece como maker, o
  ``tokenId`` e o ``side`` referem-se à ordem que ela assinou. Quando aparece
  como taker, é uma ordem da contraparte — em mercados binários, o tokenId é
  o complementar YES↔NO e o side é o oposto, pelo que matchar-taker emitiria
  sinal com mercado e direcção invertidos. O operator de Polymarket emite
  sempre OrderFilled com a wallet como maker para a ordem que ela assinou,
  pelo que esta filtragem é completa.
- price = USDC amount / shares amount. side=BUY: maker paga USDC = makerAmount,
  recebe shares = takerAmount. side=SELL: o inverso. USDC e shares partilham
  6 decimais no Polymarket → divisão unitless ≈ probabilidade ∈ [0,1].
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Iterable

import aiohttp
from eth_utils import keccak
from loguru import logger

from polymarket_bot.db.enums import TradeSide
from polymarket_bot.monitoring.signal_reader import (
    DetectedSignal,
    FollowedWallet,
)


# Polymarket: CTF Exchange V2 — mercados binários YES/NO.
POLYMARKET_CTF_EXCHANGE = "0xe111180000d2663c0091e4f400237545b87b996b"

# Polymarket: Neg Risk CTF Exchange V2 — mercados multi-outcome.
POLYMARKET_NEG_RISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310f59"

_ORDER_FILLED_SIG = (
    "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,"
    "bytes32,bytes32)"
)
ORDER_FILLED_TOPIC = "0x" + keccak(text=_ORDER_FILLED_SIG).hex()

# Side enum no contrato Solidity: 0 = BUY, 1 = SELL. Refere o lado do MAKER.
_SIDE_BUY = 0
_SIDE_SELL = 1

# Polygon: ~2s/block. 5min ≈ 150 blocos. Damos folga generosa.
_REPLAY_BLOCKS = 200
_HTTP_POLL_INTERVAL_SECONDS = 15
_WS_RECONNECT_INITIAL_DELAY = 1.0
_WS_RECONNECT_MAX_DELAY = 60.0
# eth_getLogs em RPCs públicos: Polymarket emite ~150 OrderFilled/bloco no V2.
# 500 blocos → ~75k logs → timeout em endpoints públicos. 100 blocos → ~15k,
# manejável. Providers privados (Alchemy/Infura) suportam mais, mas mantemos
# 100 como default seguro.
_MAX_LOG_RANGE = 100


SignalCallback = Callable[[DetectedSignal], Awaitable[None]]
MarketResolver = Callable[[str], Awaitable[tuple[str, str] | None]]


class MarketIdResolver:
    """Cache de `token_id → (market_id, outcome)` via Polymarket Gamma API.

    Endpoint: ``GET <gamma_url>/markets?clob_token_ids=<token_id>`` — devolve
    uma lista de markets contendo ``conditionId``, ``outcomes`` (array com os
    nomes em ordem: binários ``["Yes","No"]``, NegRisk ``["Fenerbahce", ...]``)
    e ``clobTokenIds`` (array dos token IDs alinhado com ``outcomes``).
    Mapeamos o ``token_id`` recebido pelo seu índice em ``clobTokenIds`` para
    extrair o nome do outcome.

    Antes era usado o endpoint ``<clob_url>/markets/<token_id>`` mas isso devolve
    404 desde a migração para os contratos V2 — o CLOB indexa por
    ``condition_id``, não por token id.

    Falhas devolvem ``None`` (caller deve dropar o sinal silenciosamente).
    """

    def __init__(self, gamma_url: str, http_session: aiohttp.ClientSession):
        self._url = gamma_url.rstrip("/")
        self._session = http_session
        self._cache: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, token_id: str) -> tuple[str, str] | None:
        async with self._lock:
            cached = self._cache.get(token_id)
        if cached is not None:
            return cached

        try:
            async with self._session.get(
                f"{self._url}/markets",
                params={"clob_token_ids": token_id},
            ) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning(
                "chain_watcher: market resolve falhou para token={} — {}",
                token_id, exc,
            )
            return None

        if not isinstance(payload, list) or not payload:
            return None
        market = payload[0]
        market_id = market.get("conditionId") or market.get("condition_id")
        outcome = _extract_outcome_for_token(market, token_id)
        if not market_id or not outcome:
            return None

        result = (str(market_id), outcome)
        async with self._lock:
            self._cache[token_id] = result
        return result


@dataclass(frozen=True)
class _ParsedLog:
    """Vista normalizada dum log `OrderFilled` parseado."""

    tx_hash: str
    log_index: int
    block_number: int
    maker: str
    taker: str
    side: int  # 0 = BUY, 1 = SELL — perspectiva do maker
    token_id: int
    maker_amount: int
    taker_amount: int


class ChainWatcher:
    """Deteta trades on-chain em tempo real via Polygon RPC.

    Substitui (ou complementa) o polling da Data API como fonte de sinais.
    Para cada `OrderFilled` em que `maker` ou `taker` é uma wallet seguida:
    1. Determina side (BUY/SELL) e `token_id` pelos asset IDs.
    2. Resolve `token_id → (market_id, outcome)` via CLOB API.
    3. Constrói `DetectedSignal` e invoca `signal_callback`.
    """

    def __init__(
        self,
        rpc_url: str,
        ws_url: str | None,
        followed_wallets: Iterable[FollowedWallet],
        signal_callback: SignalCallback,
        market_resolver: MarketResolver,
        http_session: aiohttp.ClientSession,
        exchange_addresses: Iterable[str] = (
            POLYMARKET_CTF_EXCHANGE,
            POLYMARKET_NEG_RISK_EXCHANGE,
        ),
        poll_interval_seconds: int = _HTTP_POLL_INTERVAL_SECONDS,
    ):
        self._rpc_url = rpc_url
        self._ws_url = ws_url
        self._callback = signal_callback
        self._resolver = market_resolver
        self._session = http_session
        self._exchanges = [a.lower() for a in exchange_addresses]
        self._poll_interval = poll_interval_seconds

        self._followed_map: dict[str, FollowedWallet] = {}
        self._followed_addresses: set[str] = set()
        self.update_followed_wallets(followed_wallets)

        self._stop = asyncio.Event()
        self._seen_logs: set[tuple[str, int]] = set()
        self._last_block: int | None = None
        self._rpc_id = 0

    # ------------------------------------------------------------------ public
    def update_followed_wallets(
        self, wallets: Iterable[FollowedWallet]
    ) -> None:
        """Substitui o universo seguido. Chamado após rebalanceamento.

        Endereços normalizados para lowercase em ambos os lados (mapa + set) —
        comparações com `parsed.maker`/`taker` são sempre case-insensitive.
        """
        self._followed_map = {w.address.lower(): w for w in wallets}
        self._followed_addresses = set(self._followed_map.keys())

    @property
    def followed_addresses(self) -> set[str]:
        return set(self._followed_addresses)

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        """Loop principal. Replay → WS (com fallback automático para polling)."""
        if not self._followed_map:
            logger.info(
                "chain_watcher: sem wallets seguidas — a aguardar update"
            )

        try:
            await self._replay_recent()
        except Exception as exc:  # noqa: BLE001 — replay best-effort
            logger.warning("chain_watcher: replay falhou — {}", exc)

        while not self._stop.is_set():
            used_ws = False
            if self._ws_url:
                try:
                    used_ws = True
                    await self._ws_loop()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "chain_watcher: WS falhou ({}) — fallback para polling",
                        exc,
                    )
            if self._stop.is_set():
                break

            try:
                await self._http_polling_loop(
                    fallback=used_ws,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "chain_watcher: polling levantou — {}", exc, exc_info=True
                )
                # Backoff antes de tentar de novo (WS ou polling).
                await self._sleep_or_stop(5.0)

        logger.info("chain_watcher: parado")

    # ------------------------------------------------------------------ replay
    async def _replay_recent(self) -> None:
        """Lê os últimos `_REPLAY_BLOCKS` blocos para apanhar trades durante downtime."""
        if not self._followed_map:
            return
        latest = await self._eth_block_number()
        from_block = max(0, latest - _REPLAY_BLOCKS)
        logger.info(
            "chain_watcher: replay {} → {} ({} blocos)",
            from_block, latest, latest - from_block,
        )
        await self._scan_range(from_block, latest)
        self._last_block = latest

    # ------------------------------------------------------------------ HTTP polling loop
    async def _http_polling_loop(self, *, fallback: bool) -> None:
        """Polling repetido via `eth_getLogs`. Sai se `_ws_url` ficar disponível
        e queremos retomar (não suportado — depois de cair no fallback ficamos cá)."""
        if fallback:
            logger.info("chain_watcher: a operar em modo polling")
        else:
            logger.info(
                "chain_watcher: a operar em modo polling (poll={}s)",
                self._poll_interval,
            )

        while not self._stop.is_set():
            try:
                latest = await self._eth_block_number()
                start = (
                    self._last_block + 1
                    if self._last_block is not None
                    else max(0, latest - 1)
                )
                if start <= latest:
                    await self._scan_range(start, latest)
                    self._last_block = latest
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("chain_watcher: poll round falhou — {}", exc)

            await self._sleep_or_stop(self._poll_interval)

    async def _scan_range(self, from_block: int, to_block: int) -> None:
        """Quebra `[from, to]` em chunks ≤ `_MAX_LOG_RANGE` e processa cada log.

        Adaptativo: se o RPC falha (timeout, "too many results", ...), divide o
        chunk em dois e tenta cada metade. Bottom-out a chunks de 10 blocos —
        abaixo disso, regista WARN e salta. Garante progresso mesmo em RPCs
        públicos lentos sem perder cobertura quando voltam a responder.
        """
        if from_block > to_block:
            return
        cursor = from_block
        while cursor <= to_block:
            chunk_end = min(cursor + _MAX_LOG_RANGE - 1, to_block)
            await self._scan_chunk_adaptive(cursor, chunk_end)
            cursor = chunk_end + 1

    async def _scan_chunk_adaptive(
        self, from_block: int, to_block: int, *, min_chunk: int = 10
    ) -> None:
        """Tenta `[from, to]`. Em caso de erro, divide e recursa até `min_chunk`.

        Excepção: erros de **rate limit** (429) NÃO são retried por halving —
        retentar mais rápido só piora. Logamos e voltamos; o próximo poll
        round vai apanhar este range com cursor preservado pelo caller.
        """
        try:
            logs = await self._eth_get_logs(from_block, to_block)
        except Exception as exc:  # noqa: BLE001 — qualquer erro RPC: tentar dividir
            msg = str(exc).lower()
            if "429" in msg or "too many requests" in msg or "rate" in msg:
                logger.warning(
                    "chain_watcher: rate-limited em {}-{} — não retentar agora "
                    "(próximo poll apanha)", from_block, to_block,
                )
                return
            span = to_block - from_block + 1
            if span <= min_chunk:
                logger.warning(
                    "chain_watcher: chunk {}-{} falhou ({} blocos) — skip: {}",
                    from_block, to_block, span, exc,
                )
                return
            mid = from_block + span // 2 - 1
            logger.debug(
                "chain_watcher: chunk {}-{} falhou ({}); a dividir em "
                "{}-{} e {}-{}",
                from_block, to_block, exc, from_block, mid, mid + 1, to_block,
            )
            await self._scan_chunk_adaptive(from_block, mid, min_chunk=min_chunk)
            await self._scan_chunk_adaptive(
                mid + 1, to_block, min_chunk=min_chunk
            )
            return

        for raw in logs:
            await self._handle_log(raw)

    # ------------------------------------------------------------------ WS loop
    async def _ws_loop(self) -> None:
        """`eth_subscribe('logs', filter)` + reconexão com backoff exponencial.

        Se o socket cair definitivamente (ex.: `ws_url` inválido), levanta para o
        caller decidir cair em polling.
        """
        # Import local — `websockets` é dep mas só carregamos se WS for usado.
        import websockets  # noqa: PLC0415

        backoff = _WS_RECONNECT_INITIAL_DELAY
        consecutive_failures = 0
        connection_attempt = 0

        while not self._stop.is_set():
            sub_id: str | None = None
            connection_attempt += 1
            logger.info(
                "chain_watcher: WS a {} (tentativa #{})",
                "reconectar" if connection_attempt > 1 else "conectar",
                connection_attempt,
            )
            try:
                async with websockets.connect(
                    self._ws_url,  # type: ignore[arg-type]
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    prior_failures = consecutive_failures
                    consecutive_failures = 0
                    backoff = _WS_RECONNECT_INITIAL_DELAY
                    sub_id = await self._ws_subscribe(ws)
                    if prior_failures > 0:
                        logger.info(
                            "chain_watcher: WS RECONECTADO após {} falha(s) "
                            "(sub_id={})",
                            prior_failures, sub_id,
                        )
                    else:
                        logger.info(
                            "chain_watcher: WS subscrito (sub_id={})", sub_id
                        )

                    # Apanha eventos publicados antes da subscrição entrar live.
                    try:
                        latest = await self._eth_block_number()
                        if (
                            self._last_block is not None
                            and self._last_block < latest
                        ):
                            await self._scan_range(self._last_block + 1, latest)
                            self._last_block = latest
                    except Exception as exc:  # noqa: BLE001 — nice to have
                        logger.warning(
                            "chain_watcher: catch-up pre-WS falhou — {}", exc
                        )

                    await self._ws_consume(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                logger.warning(
                    "chain_watcher: WS desconectou ({} falhas) — {}",
                    consecutive_failures, exc,
                )
                if consecutive_failures >= 5:
                    raise
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, _WS_RECONNECT_MAX_DELAY)

    async def _ws_subscribe(self, ws: Any) -> str:
        """Envia `eth_subscribe('logs', filter)` e devolve o subscription id."""
        topic_filter = self._build_topic_filter()
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_rpc_id(),
            "method": "eth_subscribe",
            "params": ["logs", topic_filter],
        }
        await ws.send(json.dumps(payload))
        raw = await ws.recv()
        msg = json.loads(raw)
        if "error" in msg:
            raise RuntimeError(f"eth_subscribe error: {msg['error']}")
        return str(msg.get("result") or "")

    async def _ws_consume(self, ws: Any) -> None:
        """Lê mensagens push até o socket fechar ou `stop()` ser chamado."""
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                # Sem actividade — apenas continua; ping_interval gere o keepalive.
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("method") != "eth_subscription":
                continue
            params = msg.get("params") or {}
            log = params.get("result")
            if isinstance(log, dict):
                await self._handle_log(log)

    def _build_topic_filter(self) -> dict[str, Any]:
        """Filter object para `eth_getLogs` / `eth_subscribe`.

        Usamos topic[0] = OrderFilled. Filtragem por maker/taker faz-se localmente
        em `_handle_log` — manter um único filtro simplifica WS subscriptions e
        evita reabrir o socket sempre que a lista de wallets seguidas muda.
        """
        return {
            "address": self._exchanges,
            "topics": [ORDER_FILLED_TOPIC],
        }

    # ------------------------------------------------------------------ log processing
    async def _handle_log(self, raw: dict[str, Any]) -> None:
        try:
            parsed = self._parse_log(raw)
        except (ValueError, KeyError, TypeError) as exc:
            logger.debug("chain_watcher: log não-parseável — {}", exc)
            return
        if parsed is None:
            return

        log_id = (parsed.tx_hash, parsed.log_index)
        if log_id in self._seen_logs:
            return

        followed_addr, side, token_id = self._resolve_followed(parsed)
        matched = followed_addr is not None
        logger.debug(
            "chain_watcher: OrderFilled recebido | maker={} taker={} match={}",
            parsed.maker.lower(), parsed.taker.lower(), matched,
        )
        if not matched:
            # Trade entre wallets não-seguidas — ignorar sem marcar como seen
            # (não faz sentido encher o set com lixo).
            return

        logger.info(
            "chain_watcher: sinal detetado | wallet={} maker={} tx={}",
            followed_addr, parsed.maker.lower(), parsed.tx_hash,
        )

        # Marca como visto antes da resolução; mesmo que o resolver falhe, evita
        # retentar o mesmo log indefinidamente.
        self._seen_logs.add(log_id)
        if len(self._seen_logs) > 5000:
            # Trim — descarta metade arbitrariamente; é seguro porque blocos
            # antigos não voltam (chain finalizada).
            self._seen_logs = set(list(self._seen_logs)[2500:])

        resolved = await self._resolver(token_id)
        if resolved is None:
            logger.debug(
                "chain_watcher: token_id {} não resolveu para market — drop",
                token_id,
            )
            return
        market_id, outcome = resolved

        price = self._compute_price(parsed)
        if price is None:
            return

        wallet = self._followed_map[followed_addr]
        signal = DetectedSignal(
            wallet=wallet,
            market_id=market_id,
            outcome=outcome,
            price=price,
            detected_at=datetime.now(timezone.utc),
            tx_hash=parsed.tx_hash,
            side=side,
        )

        try:
            await self._callback(signal)
        except Exception as exc:  # noqa: BLE001 — loop robusto
            logger.warning(
                "chain_watcher: callback levantou — {} (tx={}, market={})",
                exc, parsed.tx_hash, market_id,
            )

    def _resolve_followed(
        self, parsed: _ParsedLog
    ) -> tuple[str | None, TradeSide, str]:
        """Devolve `(followed_addr, side, token_id)` ou `(None, _, _)` se a wallet
        seguida não aparece como **maker** no log.

        Só matchamos como maker, NUNCA como taker. Razão: numa trade Polymarket,
        a tx batch dá origem a >=2 OrderFilled — uma para a ordem do utilizador
        (wallet=maker, ``side`` directo, ``tokenId`` correcto) e outra para a
        ordem da contraparte (wallet=taker, ``tokenId`` é o complementar YES↔NO,
        side é o oposto). Match-as-taker emitiria sinal com lado e tokenId
        invertidos. Como o operator de Polymarket emite sempre uma OrderFilled
        com a wallet como maker para a ordem que ela assinou, a regra
        "só maker" é completa e correcta.

        Comparação case-insensitive: `_followed_addresses` está em lowercase e
        forçamos `.lower()` no maker do log.
        """
        maker = parsed.maker.lower()
        if maker not in self._followed_addresses:
            return None, TradeSide.BUY, ""
        side = TradeSide.BUY if parsed.side == _SIDE_BUY else TradeSide.SELL
        return maker, side, str(parsed.token_id)

    @staticmethod
    def _compute_price(parsed: _ParsedLog) -> Decimal | None:
        """price = USDC amount / shares amount.

        side=BUY (0): maker paga USDC pelas shares → makerAmount=USDC,
            takerAmount=shares.
        side=SELL (1): maker entrega shares por USDC → makerAmount=shares,
            takerAmount=USDC.
        """
        if parsed.side == _SIDE_BUY:
            usdc, shares = parsed.maker_amount, parsed.taker_amount
        else:
            usdc, shares = parsed.taker_amount, parsed.maker_amount
        if shares <= 0:
            return None
        return Decimal(usdc) / Decimal(shares)

    @staticmethod
    def _parse_log(raw: dict[str, Any]) -> _ParsedLog | None:
        topics = raw.get("topics") or []
        if len(topics) < 4:
            return None
        if (topics[0] or "").lower() != ORDER_FILLED_TOPIC:
            return None

        maker = _decode_address_topic(topics[2])
        taker = _decode_address_topic(topics[3])
        data = raw.get("data") or "0x"
        # 7 slots: side(uint8), tokenId(uint256), makerAmount, takerAmount, fee,
        # builder(bytes32), metadata(bytes32). Cada slot = 32 bytes; uint8 vai
        # padded à esquerda. Fee/builder/metadata não são usados.
        slots = _decode_uint256_array(data, count=7)
        if slots is None:
            return None

        side = slots[0]
        token_id = slots[1]
        maker_amount = slots[2]
        taker_amount = slots[3]

        tx_hash = (raw.get("transactionHash") or "").lower()
        log_index = _to_int(raw.get("logIndex"), default=0)
        block_number = _to_int(raw.get("blockNumber"), default=0)

        return _ParsedLog(
            tx_hash=tx_hash,
            log_index=log_index,
            block_number=block_number,
            maker=maker,
            taker=taker,
            side=side,
            token_id=token_id,
            maker_amount=maker_amount,
            taker_amount=taker_amount,
        )

    # ------------------------------------------------------------------ JSON-RPC
    def _next_rpc_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_rpc_id(),
            "method": method,
            "params": params,
        }
        async with self._session.post(self._rpc_url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC {method} error: {data['error']}")
        return data.get("result")

    async def _eth_block_number(self) -> int:
        result = await self._rpc("eth_blockNumber", [])
        return int(result, 16) if isinstance(result, str) else int(result)

    async def _eth_get_logs(
        self, from_block: int, to_block: int
    ) -> list[dict[str, Any]]:
        params = [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": self._exchanges,
                "topics": [ORDER_FILLED_TOPIC],
            }
        ]
        result = await self._rpc("eth_getLogs", params)
        return list(result or [])

    # ------------------------------------------------------------------ misc
    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return


# --------------------------------------------------------------------- helpers
def _extract_outcome_for_token(market: dict[str, Any], token_id: str) -> str | None:
    """Mapeia ``token_id → outcome`` via os arrays paralelos da Gamma API.

    ``outcomes`` e ``clobTokenIds`` podem vir como JSON-encoded strings ou listas
    nativas. Devolve o nome do outcome em uppercase (ex.: ``"YES"``, ``"NO"``,
    ``"FENERBAHCE"``) ou ``None`` se o token não bate ou os arrays estão mal
    formados.
    """
    outcomes_raw = market.get("outcomes")
    tokens_raw = market.get("clobTokenIds") or market.get("clob_token_ids")
    outcomes = _parse_string_list(outcomes_raw)
    tokens = _parse_string_list(tokens_raw)
    if not outcomes or not tokens or len(outcomes) != len(tokens):
        return None
    for idx, tid in enumerate(tokens):
        if str(tid).strip() == token_id:
            name = outcomes[idx].strip()
            return name.upper() if name else None
    return None


def _parse_string_list(raw: Any) -> list[str]:
    """Aceita lista nativa ou JSON-encoded string (formato comum em Gamma)."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    return []


def _decode_address_topic(topic: str) -> str:
    """topic é 0x + 64 hex chars; address são os últimos 40 chars (lowercase)."""
    s = topic.lower()
    if s.startswith("0x"):
        s = s[2:]
    return "0x" + s[-40:]


def _decode_uint256_array(data: str, *, count: int) -> tuple[int, ...] | None:
    """Descodifica `count` uint256 packed em `data` (hex). `None` se demasiado curto."""
    s = data.lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) < count * 64:
        return None
    return tuple(int(s[i * 64 : (i + 1) * 64], 16) for i in range(count))


def _to_int(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    s = str(value)
    return int(s, 16) if s.startswith("0x") else int(s)


__all__ = [
    "ChainWatcher",
    "MarketIdResolver",
    "ORDER_FILLED_TOPIC",
    "POLYMARKET_CTF_EXCHANGE",
    "POLYMARKET_NEG_RISK_EXCHANGE",
]
