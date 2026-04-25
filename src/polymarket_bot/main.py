# Ponto de entrada único. Para correr: python -m polymarket_bot.main
"""Arranca o bot: configura logging, instancia módulos, corre o monitor.

Modo de operação:
- Sem argumentos: arranca o loop principal (`WalletMonitor.run_forever`).
- Com `--dry-run-check`: valida configuração e sai com exit 0/1 (útil para CI).
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp
from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from polymarket_bot.api.polymarket_clob import build_clob_client, load_api_creds
from polymarket_bot.api.polymarket_data import PolymarketDataClient
from polymarket_bot.config import Settings, get_settings
from polymarket_bot.db.models import Base
from polymarket_bot.execution.order_manager import OrderManager
from polymarket_bot.execution.pipeline import ExecutionPipeline
from polymarket_bot.monitoring.chain_watcher import (
    ChainWatcher,
    MarketIdResolver,
)
from polymarket_bot.monitoring.exit_manager import ExitManager
from polymarket_bot.monitoring.market_builder import MarketBuilder
from polymarket_bot.monitoring.signal_reader import SignalReader
from polymarket_bot.monitoring.wallet_monitor import WalletMonitor
from polymarket_bot.notifications.telegram_commander import TelegramCommander
from polymarket_bot.notifications.telegram_notifier import TelegramNotifier
from polymarket_bot.portfolio.exposure_guard import ExposureGuard
from polymarket_bot.portfolio.portfolio_manager import PortfolioManager
from polymarket_bot.risk.circuit_breaker import CircuitBreaker
from polymarket_bot.scheduler import RebalancingScheduler


def _configure_logging(settings: Settings) -> None:
    logger.remove()
    serialize = settings.log_format == "json"
    logger.add(
        sys.stderr,
        level=settings.log_level,
        serialize=serialize,
        backtrace=True,
        diagnose=False,
    )


def _validate_settings(settings: Settings) -> list[str]:
    """Valida que credenciais mínimas estão presentes. Não toca em rede."""
    errors: list[str] = []

    pk = settings.polygon_private_key.get_secret_value()
    if not pk or len(pk.removeprefix("0x")) != 64:
        errors.append("POLYGON_PRIVATE_KEY inválida (esperado 64 hex chars)")

    if not settings.telegram_bot_token.get_secret_value():
        errors.append("TELEGRAM_BOT_TOKEN em falta")
    if not settings.telegram_chat_id:
        errors.append("TELEGRAM_CHAT_ID em falta")

    if settings.live_mode and not settings.has_clob_credentials:
        errors.append(
            "LIVE_MODE=true mas credenciais CLOB "
            "(CLOB_API_KEY/SECRET/PASSPHRASE) em falta"
        )

    return errors


async def _dry_run_check() -> int:
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 — CI precisa de output legível
        print(f"FALHA: {exc}", file=sys.stderr)
        return 1

    errors = _validate_settings(settings)
    if errors:
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(f"FAIL: {len(errors)} erro(s) de configuração", file=sys.stderr)
        return 1

    print("OK")
    return 0


async def main() -> None:
    settings = get_settings()
    _configure_logging(settings)

    errors = _validate_settings(settings)
    if errors:
        for err in errors:
            logger.error("config: {}", err)
        raise SystemExit(f"configuração inválida ({len(errors)} erros)")

    mode = "LIVE" if settings.live_mode else "PAPER"
    logger.info("bot: a arrancar (modo={})", mode)

    engine: AsyncEngine = create_async_engine(
        settings.database_url, echo=False, future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        headers={"Accept": "application/json"},
    )

    notifier = TelegramNotifier(
        bot_token=settings.telegram_bot_token.get_secret_value(),
        chat_id=settings.telegram_chat_id,
        session=http_session,
    )
    data_client = PolymarketDataClient(session=http_session)
    portfolio = PortfolioManager(
        sessionmaker, initial_capital_usd=Decimal("1000")
    )
    circuit_breaker = CircuitBreaker(sessionmaker, notifier=notifier)
    market_builder = MarketBuilder(session=http_session)
    # ClobClient instanciado uma única vez — só em live mode (paper não precisa
    # de credenciais L2). `None` em paper garante que qualquer caminho live
    # falha explicitamente via guards, não silenciosamente.
    clob_client = (
        build_clob_client(settings, load_api_creds(settings))
        if settings.live_mode and settings.has_clob_credentials
        else None
    )
    order_manager = OrderManager(
        settings=settings,
        sessionmaker=sessionmaker,
        clob_client=clob_client,
    )
    exposure_guard = ExposureGuard(
        session_factory=sessionmaker, initial_capital=Decimal("1000")
    )
    pipeline = ExecutionPipeline(
        order_manager=order_manager, exposure_guard=exposure_guard
    )
    # Reader partilhado entre o WalletMonitor (poll_once para BUYs) e o
    # ExitManager (poll_sells para wallet exits). O cursor + dedup é único.
    signal_reader = SignalReader(
        wallets=[], data_client=data_client, source=settings.signal_source
    )
    # ChainWatcher: deteção on-chain via Polygon RPC (CLAUDE.md §13). Substitui
    # o polling à Data API quando `signal_source` é "chain" ou "both".
    chain_watcher: ChainWatcher | None = None
    if settings.signal_source in ("chain", "both"):
        market_resolver = MarketIdResolver(
            clob_url=settings.clob_api_url, http_session=http_session
        )

        async def _on_chain_signal(sig):  # noqa: ANN001 — local closure
            signal_reader.inject_signal(sig)

        chain_watcher = ChainWatcher(
            rpc_url=settings.polygon_rpc_url,
            ws_url=settings.polygon_ws_url,
            followed_wallets=[],
            signal_callback=_on_chain_signal,
            market_resolver=market_resolver.resolve,
            http_session=http_session,
        )
    exit_manager = ExitManager(
        session_factory=sessionmaker,
        clob_client_factory=lambda: clob_client,
        market_builder=market_builder,
        notifier=notifier,
        circuit_breaker=circuit_breaker,
        live_mode=settings.live_mode,
        signal_reader=signal_reader,
    )
    # Em modo "chain", o lag não vem do poll interval — vem do RPC. Encurtamos
    # o tick para 5s para drenar o buffer rapidamente sem martelar a Data API.
    monitor_poll_interval = 5 if settings.signal_source == "chain" else 30
    monitor = WalletMonitor(
        pipeline=pipeline,
        data_client=data_client,
        db_session_factory=sessionmaker,
        poll_interval_seconds=monitor_poll_interval,
        market_builder=market_builder,
        signal_reader=signal_reader,
        bankroll_provider=lambda _sm: portfolio.get_bankroll(),
        exit_manager=exit_manager,
        notifier=notifier,
        live_mode=settings.live_mode,
        chain_watcher=chain_watcher,
    )
    scheduler = RebalancingScheduler(
        monitor=monitor,
        portfolio=portfolio,
        circuit_breaker=circuit_breaker,
        notifier=notifier,
        data_client=data_client,
        session_factory=sessionmaker,
    )

    await monitor.reload_followed_wallets()
    n_followed = len(monitor.reader.followed_addresses)
    if n_followed == 0:
        logger.warning(
            "bot: nenhuma wallet seguida — aguardar rebalanceamento de domingo"
        )

    scheduler.start()

    commander = TelegramCommander(
        bot_token=settings.telegram_bot_token.get_secret_value(),
        allowed_chat_id=settings.telegram_chat_id,
        session=http_session,
        session_factory=sessionmaker,
        monitor=monitor,
        circuit_breaker=circuit_breaker,
        started_at=datetime.now(timezone.utc),
        live_mode=settings.live_mode,
        market_builder=market_builder,
    )
    commander_task = asyncio.create_task(
        commander.run_forever(), name="telegram_commander"
    )

    try:
        await notifier.send(
            f"🤖 Bot iniciado | modo: {mode} | wallets: {n_followed}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("bot: falha a notificar arranque — {}", exc)

    _install_shutdown_handlers(monitor)

    try:
        await monitor.run_forever()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("bot: sinal de shutdown recebido")
    finally:
        commander.stop()
        commander_task.cancel()
        try:
            await commander_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        scheduler.stop()
        try:
            await notifier.send("🔴 Bot encerrado")
        except Exception as exc:  # noqa: BLE001
            logger.warning("bot: falha a notificar encerramento — {}", exc)
        await http_session.close()
        await engine.dispose()
        logger.info("bot: encerrado")


def _install_shutdown_handlers(monitor: WalletMonitor) -> None:
    """SIGINT/SIGTERM → `monitor.stop()` para sair do loop com grace."""
    loop = asyncio.get_running_loop()

    def _handler() -> None:
        logger.info("bot: sinal recebido — a parar monitor")
        monitor.stop()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            # Windows: add_signal_handler não existe; fallback no-op —
            # KeyboardInterrupt é apanhado no try/except do `main()`.
            continue


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="polymarket-bot",
        description="Polymarket copytrade bot — CLAUDE.md",
    )
    parser.add_argument(
        "--dry-run-check",
        action="store_true",
        help="Valida configuração e sai sem arrancar o loop.",
    )
    return parser.parse_args(argv)


def _run() -> None:
    args = _parse_args()
    if args.dry_run_check:
        sys.exit(asyncio.run(_dry_run_check()))
    asyncio.run(main())


if __name__ == "__main__":
    _run()
