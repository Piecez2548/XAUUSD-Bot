"""Read-only CLI for Phase 1 diagnostics and Phase 1.5 observability services."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from config.settings import ConfigurationError, load_settings
from events.bus import EventBus
from models.market import MarketSnapshot, Timeframe
from mt5.symbols import SymbolDiscoveryError
from notifications.telegram import TelegramNotifier
from persistence.database import Database
from persistence.migrations import migrate_database
from persistence.repositories import EventRepository, SnapshotRepository, SystemHealthRepository
from services.backup import create_database_backup
from services.collector import collect_market_snapshot
from services.control import TelegramControlService
from services.live import LiveDataEngine
from services.observatory import ObservatoryService
from services.shadow_outcome import ShadowOutcomeWorker
from services.shadow_replay import load_persisted_snapshots, replay_snapshots
from utils.logging import configure_logging

PROJECT_ROOT = Path(__file__).resolve().parent


def print_report(snapshot: MarketSnapshot) -> None:
    latest = snapshot.candles[Timeframe.M5][-1]
    lines = [
        "=" * 40,
        "XAUUSD AI TRADER - PHASE 1",
        "=" * 40,
        "",
        "MT5",
        "Status: CONNECTED",
        "",
        "ACCOUNT",
        f"Balance: {snapshot.account.balance:.2f}",
        f"Equity: {snapshot.account.equity:.2f}",
        f"Currency: {snapshot.account.currency}",
        f"Server: {snapshot.account.server}",
        f"Trade mode: {snapshot.account.trade_mode_name or 'unavailable'}",
        "",
        "SYMBOL",
        f"Broker symbol: {snapshot.symbol.name}",
        f"Bid: {snapshot.tick.bid:.{snapshot.symbol.digits}f}",
        f"Ask: {snapshot.tick.ask:.{snapshot.symbol.digits}f}",
        f"Spread: {snapshot.symbol.spread} points",
        f"Trade mode: {snapshot.symbol.trade_mode_name}",
        "",
        "DATA",
        *[
            f"{timeframe.value:<4}: {len(snapshot.candles[timeframe])} candles"
            for timeframe in Timeframe
        ],
        "",
        "Latest M5:",
        f"Time: {latest.timestamp.isoformat()}",
        f"O: {latest.open}",
        f"H: {latest.high}",
        f"L: {latest.low}",
        f"C: {latest.close}",
        "",
        "POSITIONS",
        f"Open positions: {len(snapshot.positions)}",
        "",
        "PHASE 1 STATUS: PASS",
        "",
        "READ-ONLY MODE",
        "ORDER EXECUTION DISABLED",
        "=" * 40,
    ]
    print("\n".join(lines))


def print_failure(message: str, candidates: tuple[str, ...] = ()) -> None:
    print("=" * 40)
    print("XAUUSD AI TRADER - PHASE 1")
    print("=" * 40)
    print(f"ERROR: {message}")
    if candidates:
        print("Gold symbol candidates:")
        for candidate in candidates:
            print(f"  - {candidate}")
        print("Set TRADING_SYMBOL in .env to the exact broker symbol.")
    print("\nPHASE 1 STATUS: FAIL")
    print("\nREAD-ONLY MODE")
    print("ORDER EXECUTION DISABLED")
    print("=" * 40)


def _load_runtime():
    settings = load_settings()
    log_path = settings.log_directory
    if not log_path.is_absolute():
        log_path = PROJECT_ROOT / log_path
    logger = configure_logging(
        log_path,
        settings.log_level,
        secrets=(settings.mt5_password or "", settings.telegram_bot_token or ""),
    )
    return settings, logger


def run_phase1() -> int:
    try:
        settings, logger = _load_runtime()
    except ConfigurationError as exc:
        print_failure(str(exc))
        return 1
    logger.info("Phase 1 read-only diagnostic startup")

    try:
        snapshot, _candidates = collect_market_snapshot(settings, logger=logger)
        print_report(snapshot)
        logger.info("Phase 1 diagnostic completed successfully")
        return 0
    except SymbolDiscoveryError as exc:
        logger.error("Symbol discovery failed: %s", exc)
        print_failure(str(exc), exc.candidates)
        return 1
    except Exception as exc:
        logger.exception("Phase 1 diagnostic failed")
        print_failure(str(exc))
        return 1
    finally:
        logger.info("Phase 1 diagnostic shutdown")


async def _observe() -> int:
    settings, logger = _load_runtime()
    migrate_database(settings.database_url, PROJECT_ROOT)
    database = Database(settings.database_url, project_root=PROJECT_ROOT)
    event_repository = EventRepository(database)
    health_repository = SystemHealthRepository(database)
    telegram = TelegramNotifier(
        enabled=settings.telegram_enabled,
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        timeout_seconds=settings.telegram_timeout_seconds,
        logger=logger,
        health_reporter=health_repository,
    )
    bus = EventBus(logger)
    bus.subscribe("database", event_repository.handle, critical=True)
    bus.subscribe("telegram", telegram.handle)
    try:
        result = await ObservatoryService(
            settings,
            SnapshotRepository(database),
            bus,
            logger=logger,
        ).observe_once()
        print("READ-ONLY OBSERVATION PERSISTED")
        print(f"Market snapshot: {result.market_snapshot_id}")
        print(f"Account snapshot: {result.account_snapshot_id}")
        print(f"Risk snapshot: {result.risk_snapshot_id}")
        return 0
    finally:
        database.dispose()


async def _telegram_test() -> int:
    settings, logger = _load_runtime()
    migrate_database(settings.database_url, PROJECT_ROOT)
    database = Database(settings.database_url, project_root=PROJECT_ROOT)
    notifier = TelegramNotifier(
        enabled=settings.telegram_enabled,
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        timeout_seconds=settings.telegram_timeout_seconds,
        logger=logger,
        health_reporter=SystemHealthRepository(database),
    )
    try:
        success = await notifier.send_test()
        print(
            "Telegram test sent successfully."
            if success
            else "Telegram is disabled or unavailable."
        )
        return 0 if success else 1
    finally:
        database.dispose()


async def _live() -> int:
    """Run the continuous Phase 1.6 read-only data engine until interrupted."""

    settings, logger = _load_runtime()
    migrate_database(settings.database_url, PROJECT_ROOT)
    database = Database(settings.database_url, project_root=PROJECT_ROOT)
    event_repository = EventRepository(database)
    health_repository = SystemHealthRepository(database)
    telegram = TelegramNotifier(
        enabled=settings.telegram_enabled,
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        timeout_seconds=settings.telegram_timeout_seconds,
        logger=logger,
        health_reporter=health_repository,
    )
    bus = EventBus(logger)
    bus.subscribe("database", event_repository.handle, critical=True)
    bus.subscribe("telegram", telegram.handle)
    engine = LiveDataEngine(settings, database, bus, logger=logger)
    try:
        await engine.run()
        return 0
    except KeyboardInterrupt:
        await engine.stop()
        return 0
    finally:
        database.dispose()


async def _control() -> int:
    """Run the persistent Telegram control plane; it never enables execution."""

    settings, logger = _load_runtime()
    migrate_database(settings.database_url, PROJECT_ROOT)
    service = TelegramControlService(settings, PROJECT_ROOT, logger=logger)
    try:
        await service.run_forever()
        return 0
    except KeyboardInterrupt:
        await service.stop()
        return 0


def run_server() -> int:
    import uvicorn

    settings, _logger = _load_runtime()
    migrate_database(settings.database_url, PROJECT_ROOT)
    uvicorn.run(
        "api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )
    return 0


def run_backup() -> int:
    settings, _logger = _load_runtime()
    target = create_database_backup(
        settings.database_url,
        settings.backup_directory,
        project_root=PROJECT_ROOT,
    )
    print(f"Backup created: {target}")
    return 0


def run_shadow_replay() -> int:
    settings, _logger = _load_runtime()
    migrate_database(settings.database_url, PROJECT_ROOT)
    database = Database(settings.database_url, project_root=PROJECT_ROOT)
    try:
        report = replay_snapshots(load_persisted_snapshots(database))
        print("PHASE 2 SHADOW REPLAY")
        print(f"Candles processed: {report.candles_processed}")
        print(f"Decisions generated: {report.decisions_generated}")
        print(f"BUY: {report.buy_count}")
        print(f"SELL: {report.sell_count}")
        print(f"NO_TRADE: {report.no_trade_count}")
        print(f"Errors: {report.errors}")
        print(f"Duplicates: {report.duplicates}")
        print("Execution: DISABLED")
        return 0 if report.errors == 0 else 1
    finally:
        database.dispose()


def run_shadow_evaluate() -> int:
    settings, logger = _load_runtime()
    migrate_database(settings.database_url, PROJECT_ROOT)
    database = Database(settings.database_url, project_root=PROJECT_ROOT)
    worker = ShadowOutcomeWorker(settings, database, logger=logger)

    async def evaluate() -> dict[str, int]:
        return await worker.evaluate_once()

    try:
        report = asyncio.run(evaluate())
        print("PHASE 2.1 SHADOW OUTCOME EVALUATION")
        for key, value in report.items():
            print(f"{key.replace('_', ' ').title()}: {value}")
        print("Execution: DISABLED")
        return 0 if report.get("errors", 0) == 0 else 1
    finally:
        database.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="XAUUSD AI Trader read-only tools")
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "phase1",
            "observe",
            "live",
            "control",
            "server",
            "telegram-test",
            "backup",
            "migrate",
            "shadow-replay",
            "shadow-evaluate",
        ),
        default="phase1",
        help="phase1 is the unchanged default diagnostic",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    command = build_parser().parse_args(argv).command
    if command == "phase1":
        return run_phase1()
    if command == "observe":
        return asyncio.run(_observe())
    if command == "live":
        return asyncio.run(_live())
    if command == "control":
        return asyncio.run(_control())
    if command == "server":
        return run_server()
    if command == "telegram-test":
        return asyncio.run(_telegram_test())
    if command == "backup":
        return run_backup()
    if command == "shadow-replay":
        return run_shadow_replay()
    if command == "shadow-evaluate":
        return run_shadow_evaluate()
    settings, _logger = _load_runtime()
    migrate_database(settings.database_url, PROJECT_ROOT)
    print("Database migrations applied.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
