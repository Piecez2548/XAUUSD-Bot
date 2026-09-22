"""Gold symbol discovery and symbol/tick read operations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from models.market import SymbolSpecification, Tick


class SymbolDiscoveryError(RuntimeError):
    def __init__(self, message: str, candidates: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.candidates = candidates


class SymbolAmbiguityError(SymbolDiscoveryError):
    """Raised when more than one candidate is equally plausible."""


@dataclass(frozen=True, slots=True)
class SymbolCandidate:
    name: str
    score: int


def _text(value: object) -> str:
    return str(value or "").upper()


def _candidate_score(symbol: Any) -> int:
    name = _text(getattr(symbol, "name", ""))
    compact = re.sub(r"[^A-Z0-9]", "", name)
    base = _text(getattr(symbol, "currency_base", ""))
    profit = _text(getattr(symbol, "currency_profit", ""))
    description = _text(getattr(symbol, "description", ""))

    if name == "XAUUSD":
        return 1_000
    if base == "XAU" and profit == "USD":
        return 900
    if compact.startswith("XAUUSD"):
        return 800
    if name == "GOLD":
        return 700
    if compact.startswith("GOLD") and ("USD" in compact or profit == "USD"):
        return 600
    if "XAU" in compact and "USD" in compact:
        return 500
    if "GOLD" in name and ("USD" in description or profit == "USD"):
        return 400
    return 0


def rank_gold_candidates(symbols: Iterable[Any]) -> tuple[SymbolCandidate, ...]:
    """Return deterministic likely XAUUSD candidates ordered by quality and name."""

    candidates = [
        SymbolCandidate(name=str(symbol.name), score=score)
        for symbol in symbols
        if (score := _candidate_score(symbol)) > 0
    ]
    return tuple(
        sorted(candidates, key=lambda candidate: (-candidate.score, candidate.name.upper()))
    )


def select_symbol_name(symbols: Iterable[Any], override: str | None = None) -> str:
    """Select a symbol only when the result is exact or uniquely highest-ranked."""

    symbol_list = tuple(symbols)
    if override:
        exact = [
            str(item.name)
            for item in symbol_list
            if str(item.name).casefold() == override.casefold()
        ]
        if len(exact) == 1:
            return exact[0]
        candidates = tuple(candidate.name for candidate in rank_gold_candidates(symbol_list))
        raise SymbolDiscoveryError(
            f"TRADING_SYMBOL={override!r} was not found exactly in the MT5 symbol list",
            candidates,
        )

    ranked = rank_gold_candidates(symbol_list)
    if not ranked:
        raise SymbolDiscoveryError("No likely XAUUSD/gold symbols were found")
    highest = ranked[0].score
    leaders = tuple(candidate.name for candidate in ranked if candidate.score == highest)
    if len(leaders) != 1:
        raise SymbolAmbiguityError(
            "Multiple equally likely gold symbols were found; set TRADING_SYMBOL explicitly",
            tuple(candidate.name for candidate in ranked),
        )
    return leaders[0]


def discover_symbol(api: Any, override: str | None = None) -> tuple[str, tuple[str, ...]]:
    symbols = api.symbols_get()
    if symbols is None:
        raise SymbolDiscoveryError(f"MT5 symbols_get failed: {api.last_error()!r}")
    candidates = tuple(candidate.name for candidate in rank_gold_candidates(symbols))
    selected = select_symbol_name(symbols, override)
    info = api.symbol_info(selected)
    if info is None:
        raise SymbolDiscoveryError(
            f"MT5 symbol_info failed for {selected}: {api.last_error()!r}", candidates
        )
    if not bool(getattr(info, "visible", False)) and not api.symbol_select(selected, True):
        raise SymbolDiscoveryError(
            f"MT5 could not enable {selected} in Market Watch: {api.last_error()!r}", candidates
        )
    return selected, candidates


SYMBOL_TRADE_MODES = {
    0: "disabled",
    1: "long_only",
    2: "short_only",
    3: "close_only",
    4: "full",
}


def read_symbol_specification(api: Any, symbol: str) -> SymbolSpecification:
    info = api.symbol_info(symbol)
    if info is None:
        raise SymbolDiscoveryError(f"MT5 symbol_info failed for {symbol}: {api.last_error()!r}")
    mode = int(info.trade_mode)
    return SymbolSpecification(
        name=str(info.name),
        bid=float(info.bid),
        ask=float(info.ask),
        spread=int(info.spread),
        digits=int(info.digits),
        point=float(info.point),
        trade_tick_size=float(info.trade_tick_size),
        trade_tick_value=float(info.trade_tick_value),
        trade_tick_value_profit=float(info.trade_tick_value_profit),
        trade_tick_value_loss=float(info.trade_tick_value_loss),
        contract_size=float(info.trade_contract_size),
        volume_min=float(info.volume_min),
        volume_max=float(info.volume_max),
        volume_step=float(info.volume_step),
        trade_mode=mode,
        trade_mode_name=SYMBOL_TRADE_MODES.get(mode, "unknown"),
    )


def read_tick(api: Any, symbol: str) -> Tick:
    tick = api.symbol_info_tick(symbol)
    if tick is None:
        raise SymbolDiscoveryError(
            f"MT5 symbol_info_tick failed for {symbol}: {api.last_error()!r}"
        )
    raw_time = int(tick.time)
    return Tick(
        timestamp=datetime.fromtimestamp(raw_time, tz=UTC),
        raw_timestamp=raw_time,
        bid=float(tick.bid),
        ask=float(tick.ask),
        last=float(tick.last),
        volume=float(tick.volume),
        flags=int(tick.flags),
    )
