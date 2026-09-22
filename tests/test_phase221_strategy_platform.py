# ruff: noqa: E501

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config.settings import Settings
from models.market import Candle, MarketSnapshot, Timeframe
from persistence.database import Database
from persistence.repositories import StrategyActivationRepository
from services.shadow_engine import ShadowDecisionEngine
from services.strategy_platform import StrategyRegistry, config_hash


def test_registry_resolves_versioned_plugins_and_rejects_unknown():
    registry = StrategyRegistry(Settings())
    assert registry.identifiers() == ("baseline_v1", "trend_pullback_v1")
    assert registry.resolve("baseline_v1").metadata.strategy_version == "baseline_v1"
    assert registry.resolve("trend_pullback_v1").metadata.config_hash
    with pytest.raises(ValueError, match="unknown strategy"):
        registry.resolve("missing_strategy")


def test_config_hash_is_deterministic_and_order_independent():
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})
    assert config_hash({"a": 1}) != config_hash({"a": 2})


def test_baseline_adapter_preserves_known_engine_decision(model_parts):
    snapshot = _trend_snapshot(model_parts)
    risk = __import__("services.risk", fromlist=["calculate_risk_snapshot"]).calculate_risk_snapshot(
        snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6
    )
    baseline = StrategyRegistry(Settings()).resolve("baseline_v1")
    direct = ShadowDecisionEngine().evaluate(snapshot, risk=risk)
    adapted = baseline.evaluate(snapshot, risk=risk)
    assert adapted.decision == direct.decision
    assert adapted.entry_price == direct.entry_price
    assert adapted.execution_allowed is False
    assert adapted.strategy_version == "baseline_v1"
    assert adapted.config_hash


def test_activation_history_is_append_only_and_shadow_safe(tmp_path: Path):
    db = Database(f"sqlite:///{(tmp_path / 'activation.db').as_posix()}")
    db.create_schema()
    repo = StrategyActivationRepository(db)
    from datetime import UTC, datetime

    row = repo.record(
        strategy_id="trend_pullback", strategy_version="1.0.0",
        config_version="v1", config_hash="a" * 64,
        effective_from_m5=datetime(2026, 1, 1, tzinfo=UTC), previous_strategy="baseline",
        reason="operator research", source="OPERATOR",
    )
    assert repo.latest().id == row.id
    assert row.execution_allowed is False
    db.dispose()


def _trend_snapshot(model_parts):
    account, symbol, tick, _candles, _position = model_parts
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = {}
    for timeframe in Timeframe:
        values = []
        for index in range(31):
            opened = float(2000 + index)
            values.append(Candle(
                timestamp=start + timedelta(minutes=index * 5),
                raw_timestamp=int((start + timedelta(minutes=index * 5)).timestamp()),
                open=opened, high=opened + 2, low=opened - 1, close=opened + 1,
                tick_volume=100, spread=20, real_volume=0,
            ))
        candles[timeframe] = tuple(values)
    latest = candles[Timeframe.M5][-2].close
    adjusted_symbol = symbol.model_copy(update={"bid": latest - 0.1, "ask": latest + 0.1})
    return MarketSnapshot(
        account=account, symbol=adjusted_symbol,
        tick=tick.model_copy(update={"bid": adjusted_symbol.bid, "ask": adjusted_symbol.ask}),
        candles=candles, positions=(), generated_at=start + timedelta(minutes=200),
    )
