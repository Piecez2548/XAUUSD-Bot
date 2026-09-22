from __future__ import annotations

import pytest

from mt5.symbols import (
    SymbolAmbiguityError,
    SymbolDiscoveryError,
    rank_gold_candidates,
    select_symbol_name,
)


def test_exact_xauusd_is_preferred_over_suffix(symbol_factory) -> None:
    symbols = [symbol_factory("XAUUSD.a"), symbol_factory("XAUUSD")]
    assert select_symbol_name(symbols) == "XAUUSD"


def test_unique_broker_suffix_is_selected(symbol_factory) -> None:
    symbols = [symbol_factory("EURUSD"), symbol_factory("XAUUSDm")]
    assert select_symbol_name(symbols) == "XAUUSDm"


def test_equally_ranked_candidates_require_override(symbol_factory) -> None:
    symbols = [symbol_factory("XAUUSD.a"), symbol_factory("XAUUSD.b")]
    with pytest.raises(SymbolAmbiguityError) as captured:
        select_symbol_name(symbols)
    assert captured.value.candidates == ("XAUUSD.a", "XAUUSD.b")


def test_override_requires_case_insensitive_exact_match(symbol_factory) -> None:
    symbols = [symbol_factory("XAUUSD.a"), symbol_factory("GOLD")]
    assert select_symbol_name(symbols, "xauusd.a") == "XAUUSD.a"
    with pytest.raises(SymbolDiscoveryError, match="was not found exactly"):
        select_symbol_name(symbols, "XAUUSD")


def test_gold_currency_metadata_is_a_strong_candidate(symbol_factory) -> None:
    symbols = [
        symbol_factory("BrokerGold", currency_base="XAU", currency_profit="USD"),
        symbol_factory("GOLDUSD"),
    ]
    ranked = rank_gold_candidates(symbols)
    assert ranked[0].name == "BrokerGold"
