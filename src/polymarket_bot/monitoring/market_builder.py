"""Construção de `MarketSnapshot` a partir das APIs Gamma + CLOB.

Dado `(market_id, outcome)` devolve um snapshot pronto para o pipeline. A Gamma
API fornece metadata (pergunta, categoria, volume, data de resolução) e o
campo `clobTokenIds` — array JSON indexado [YES, NO] — que identifica o
`token_id` usado pela CLOB `/book` para obter o orderbook do lado pretendido.

Cache in-memory TTL=30s por `(market_id, outcome)` para absorver rajadas de
sinais de múltiplas wallets sobre o mesmo mercado. Retry idêntico ao
`PolymarketDataClient` (3 tentativas, exponential 5–60s) conforme §9.1 do CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from polymarket_bot.config import get_settings
from polymarket_bot.market.models import (
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
)


class MarketBuildError(Exception):
    """Falha ao construir `MarketSnapshot` — caller deve skippar este mercado."""


class _TransientMarketError(Exception):
    """Erro transitório (rede / 5xx / 429) que dispara retry."""


@dataclass(frozen=True)
class _CacheEntry:
    snapshot: MarketSnapshot
    cached_at: datetime


class MarketBuilder:
    """Carrega metadata (Gamma) + orderbook (CLOB) e devolve um `MarketSnapshot`."""

    CACHE_TTL_SECONDS: int = 30

    def __init__(
        self,
        gamma_url: str | None = None,
        clob_url: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ):
        settings = get_settings()
        self._gamma_url = (gamma_url or settings.polymarket_gamma_api_url).rstrip("/")
        self._clob_url = (clob_url or settings.clob_api_url).rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._cache_lock = asyncio.Lock()

    async def __aenter__(self) -> "MarketBuilder":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"Accept": "application/json"},
            )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def build(self, market_id: str, outcome: str) -> MarketSnapshot:
        """Devolve snapshot completo para `(market_id, outcome)`.

        Lança `MarketBuildError` se o mercado não existe, o outcome não consta
        na lista de outcomes do mercado, ou o orderbook está vazio.

        Aceita binários (``"YES"``/``"NO"``) e NegRisk multi-outcome
        (``"FENERBAHCE"``, ``"TEXAS RANGERS"``, ...). O matching com a Gamma
        API é case-insensitive — basta que o outcome bata com algum dos
        ``outcomes`` reportados pelo Gamma para esse mercado.
        """
        if not outcome or not outcome.strip():
            raise MarketBuildError("outcome vazio")
        outcome_up = outcome.strip().upper()

        key = (market_id, outcome_up)
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None and self._is_fresh(cached):
                return cached.snapshot

        log = logger.bind(market_id=market_id, outcome=outcome_up)
        log.debug("market_builder: cache miss — a buscar Gamma + book")

        market_raw = await self._fetch_market(market_id)
        token_id = self._extract_token_id(market_raw, outcome_up)
        book_raw = await self._fetch_book(token_id)
        orderbook = self._parse_orderbook(market_id, outcome_up, book_raw)

        if not orderbook.bids and not orderbook.asks:
            raise MarketBuildError(
                f"orderbook vazio para market={market_id} outcome={outcome_up}"
            )

        snapshot = MarketSnapshot(
            market_id=str(market_id),
            slug=self._opt_str(market_raw.get("slug")),
            question=str(market_raw.get("question") or market_raw.get("title") or ""),
            category=self._opt_str(market_raw.get("category")),
            outcome=outcome_up,
            volume_usd=self._to_decimal(market_raw.get("volume"), default=Decimal("0")),
            resolves_at=self._parse_datetime(
                market_raw.get("endDate") or market_raw.get("end_date")
            ),
            orderbook=orderbook,
            token_id=token_id,
        )

        async with self._cache_lock:
            self._cache[key] = _CacheEntry(
                snapshot=snapshot, cached_at=datetime.now(timezone.utc)
            )
        return snapshot

    # ------------------------------------------------------------------ HTTP
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=5, max=60),
        retry=retry_if_exception_type((aiohttp.ClientError, _TransientMarketError)),
        reraise=True,
    )
    async def _fetch_market(self, market_id: str) -> dict[str, Any]:
        """Resolve um market via Gamma API.

        ``market_id`` aceita formato bytes32 (``conditionId`` 0x...) ou o id
        numérico interno do Gamma. O endpoint path ``/markets/{id}`` só aceita
        o id numérico — para ``conditionId`` usa-se o query ``?condition_ids=``
        que devolve uma lista. Tentamos a query primeiro (cobre os dois casos
        em produção, já que `chain_watcher` passa o ``conditionId``); só se
        falhar caímos no path.
        """
        if str(market_id).lower().startswith("0x"):
            url = f"{self._gamma_url}/markets"
            payload = await self._get(url, params={"condition_ids": market_id})
            if not isinstance(payload, list) or not payload:
                raise MarketBuildError(
                    f"market não encontrado em Gamma para condition_id={market_id!r}"
                )
            data = payload[0]
        else:
            url = f"{self._gamma_url}/markets/{market_id}"
            data = await self._get(url)

        if not isinstance(data, dict):
            raise MarketBuildError(f"resposta Gamma inesperada para {market_id!r}")
        return data

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=5, max=60),
        retry=retry_if_exception_type((aiohttp.ClientError, _TransientMarketError)),
        reraise=True,
    )
    async def _fetch_book(self, token_id: str) -> dict[str, Any]:
        url = f"{self._clob_url}/book"
        data = await self._get(url, params={"token_id": token_id})
        if not isinstance(data, dict):
            raise MarketBuildError(f"resposta CLOB /book inesperada para token={token_id!r}")
        return data

    async def _get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> Any:
        assert self._session is not None, "Use MarketBuilder como async context manager"
        async with self._session.get(url, params=params) as resp:
            if resp.status == 404:
                raise MarketBuildError(f"404 em {url}")
            if resp.status == 429 or resp.status >= 500:
                raise _TransientMarketError(f"{resp.status} em {url}")
            resp.raise_for_status()
            return await resp.json()

    # ------------------------------------------------------------------ parsing
    @classmethod
    def _extract_token_id(cls, market_raw: dict[str, Any], outcome: str) -> str:
        """Encontra o ``token_id`` para um ``outcome`` no formato do Gamma.

        Para binários: ``outcomes=["Yes","No"]``, ``clobTokenIds=[t0,t1]``.
        Para NegRisk multi-outcome: ``outcomes=["Fenerbahce","Galatasaray",...]``
        com ``clobTokenIds`` na mesma ordem.

        Match por nome case-insensitive contra a posição correspondente em
        ``clobTokenIds``. Antes assumia binário com índice 0=YES/1=else, o que
        falhava em mercados NegRisk multi-outcome.
        """
        token_ids = cls._parse_string_array(
            market_raw.get("clobTokenIds"), field="clobTokenIds"
        )
        outcomes = cls._parse_string_array(
            market_raw.get("outcomes"), field="outcomes"
        )

        if len(token_ids) < 2:
            raise MarketBuildError(
                f"`clobTokenIds` com menos de 2 entradas: {token_ids!r}"
            )
        if len(outcomes) != len(token_ids):
            raise MarketBuildError(
                f"`outcomes` ({len(outcomes)}) e `clobTokenIds` ({len(token_ids)}) "
                f"com tamanhos diferentes"
            )

        outcome_up = outcome.strip().upper()
        for idx, name in enumerate(outcomes):
            if name.strip().upper() == outcome_up:
                token_id = token_ids[idx]
                if not token_id:
                    raise MarketBuildError(
                        f"token_id vazio para outcome={outcome!r}"
                    )
                return str(token_id)

        raise MarketBuildError(
            f"outcome {outcome!r} não consta em outcomes={outcomes}"
        )

    @staticmethod
    def _parse_string_array(raw: Any, *, field: str) -> list[str]:
        """Aceita lista nativa ou JSON-encoded string (formato comum no Gamma)."""
        if raw is None:
            raise MarketBuildError(f"campo `{field}` ausente na resposta Gamma")
        if isinstance(raw, list):
            return [str(x) for x in raw]
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MarketBuildError(
                    f"`{field}` não é JSON válido: {raw!r}"
                ) from exc
            if not isinstance(parsed, list):
                raise MarketBuildError(
                    f"`{field}` JSON não é lista: {parsed!r}"
                )
            return [str(x) for x in parsed]
        raise MarketBuildError(
            f"`{field}` em formato inesperado: {type(raw).__name__}"
        )

    @classmethod
    def _parse_orderbook(
        cls, market_id: str, outcome: str, book_raw: dict[str, Any]
    ) -> OrderBook:
        bids = cls._parse_levels(book_raw.get("bids") or [])
        asks = cls._parse_levels(book_raw.get("asks") or [])
        # Garantir ordenação canónica (bids desc, asks asc) independentemente do servidor.
        bids = tuple(sorted(bids, key=lambda lvl: lvl.price, reverse=True))
        asks = tuple(sorted(asks, key=lambda lvl: lvl.price))
        return OrderBook(
            market_id=str(market_id),
            outcome=outcome,
            bids=bids,
            asks=asks,
        )

    @staticmethod
    def _parse_levels(raw: list[Any]) -> list[OrderBookLevel]:
        levels: list[OrderBookLevel] = []
        for entry in raw:
            price_raw: Any
            size_raw: Any
            if isinstance(entry, dict):
                price_raw = entry.get("price")
                size_raw = entry.get("size") or entry.get("amount")
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price_raw, size_raw = entry[0], entry[1]
            else:
                continue
            try:
                price = Decimal(str(price_raw))
                size = Decimal(str(size_raw))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if price <= 0 or size <= 0:
                continue
            levels.append(OrderBookLevel(price=price, size=size))
        return levels

    @staticmethod
    def _parse_datetime(value: Any) -> datetime:
        if value is None:
            raise MarketBuildError("mercado sem `endDate`")
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise MarketBuildError(f"`endDate` inválido: {value!r}") from exc
        raise MarketBuildError(f"`endDate` em formato inesperado: {type(value).__name__}")

    @staticmethod
    def _to_decimal(value: Any, default: Decimal) -> Decimal:
        if value is None:
            return default
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return default

    @staticmethod
    def _opt_str(value: Any) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        return s or None

    def _is_fresh(self, entry: _CacheEntry) -> bool:
        age = (datetime.now(timezone.utc) - entry.cached_at).total_seconds()
        return age < self.CACHE_TTL_SECONDS
