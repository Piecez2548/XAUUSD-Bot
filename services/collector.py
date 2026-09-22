"""Phase 1 market collection orchestration shared by CLI and observatory."""

from __future__ import annotations

import logging
from typing import Any

from config.settings import Settings
from models.market import MarketSnapshot
from mt5.account import read_account_state
from mt5.connection import MT5Connection
from mt5.market_data import read_all_candles
from mt5.positions import read_open_positions
from mt5.symbols import discover_symbol, read_symbol_specification, read_tick


def collect_market_snapshot(
    settings: Settings,
    *,
    logger: logging.Logger,
    mt5_module: Any | None = None,
) -> tuple[MarketSnapshot, tuple[str, ...]]:
    with MT5Connection(settings, module=mt5_module, logger=logger) as connection:
        api = connection.api
        symbol, candidates = discover_symbol(api, settings.trading_symbol)
        logger.info("Selected broker symbol %s from %d candidate(s)", symbol, len(candidates))
        snapshot = MarketSnapshot(
            account=read_account_state(api),
            symbol=read_symbol_specification(api, symbol),
            tick=read_tick(api, symbol),
            positions=read_open_positions(api, symbol),
            candles=read_all_candles(
                api,
                symbol,
                settings.candle_counts,
                minimum_ratio=settings.minimum_candle_ratio,
                logger=logger,
            ),
        )
        logger.info("Retrieved %d open position(s) for %s", len(snapshot.positions), symbol)
        return snapshot, candidates
