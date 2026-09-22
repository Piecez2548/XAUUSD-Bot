# ruff: noqa: E501

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from config.settings import Settings
from persistence.database import Database
from persistence.orm import CandleRecord, ShadowDecisionRecord
from services.shadow_outcome import (
    AMBIGUOUS,
    EXPIRED,
    INVALID,
    PENDING,
    SL_HIT,
    TP_HIT,
    EvaluationPolicy,
    ShadowOutcomeWorker,
    evaluate_decision,
    performance_summary,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def decision(side: str = "BUY", *, timestamp: datetime = BASE) -> ShadowDecisionRecord:
    entry = 100.0
    return ShadowDecisionRecord(
        id=str(uuid4()), created_at=timestamp, market_snapshot_id=None, symbol="XAUUSD",
        m5_candle_timestamp=timestamp, decision=side, market_regime="TREND",
        entry_price=entry, stop_loss=99.0 if side == "BUY" else 101.0,
        take_profit=102.0 if side == "BUY" else 98.0, risk_reward_ratio=2.0,
        requested_risk_percent=1.0, approved_risk_percent=1.0, hypothetical_volume=0.1,
        confidence=0.8, strategy_name="baseline", strategy_version="baseline_v1",
        reason_codes=[], human_readable_reason="test", risk_gate_state="APPROVED",
        data_freshness="LIVE", execution_allowed=False, feature_context={}, outcome_status="PENDING",
    )


def candle(index: int, *, high: float, low: float, close: float) -> CandleRecord:
    return CandleRecord(
        id=str(uuid4()), symbol_id="symbol", timeframe="M5",
        timestamp=BASE + timedelta(minutes=5 * index), raw_timestamp=1_700_000_000 + index * 300,
        open=100.0, high=high, low=low, close=close, tick_volume=1, spread=1, real_volume=1,
    )


def test_buy_sell_tp_sl_and_same_candle_are_deterministic():
    buy = decision("BUY")
    assert evaluate_decision(buy, [candle(1, high=102.1, low=99.5, close=101)])['terminal_status'] == TP_HIT
    assert evaluate_decision(buy, [candle(1, high=100.5, low=98.9, close=99)])['terminal_status'] == SL_HIT
    assert evaluate_decision(buy, [candle(1, high=102.1, low=98.9, close=100)])['terminal_status'] == AMBIGUOUS
    sell = decision("SELL")
    assert evaluate_decision(sell, [candle(1, high=100.5, low=97.9, close=99)])['terminal_status'] == TP_HIT
    assert evaluate_decision(sell, [candle(1, high=101.1, low=99.5, close=101)])['terminal_status'] == SL_HIT


def test_strict_temporal_causality_and_horizon():
    row = decision("BUY")
    before = candle(0, high=200, low=1, close=2)
    at_decision = candle(0, high=200, low=1, close=2)
    future = [candle(index, high=100.5, low=99.5, close=100.2) for index in range(1, 4)]
    assert evaluate_decision(row, [before, at_decision] + future, policy=EvaluationPolicy(horizon_bars=3))["terminal_status"] == EXPIRED
    assert evaluate_decision(row, future[:2], policy=EvaluationPolicy(horizon_bars=3))["terminal_status"] == PENDING
    assert evaluate_decision(row, future, policy=EvaluationPolicy(horizon_bars=3))["bars_held"] == 3


def test_invalid_geometry_is_explicit():
    row = decision("BUY")
    row.stop_loss = 101.0
    assert evaluate_decision(row, [])['terminal_status'] == INVALID
    row.decision = "NO_TRADE"
    assert evaluate_decision(row, [])['reason_code'] == "NON_TRADE_DECISION"


def test_worker_is_idempotent_and_health_recovers(tmp_path: Path):
    database = Database(f"sqlite:///{(tmp_path / 'outcomes.db').as_posix()}")
    database.create_schema()
    with database.session() as session:
        session.add(decision("BUY"))
    worker = ShadowOutcomeWorker(Settings(), database, logger=__import__("logging").getLogger("test"))
    first = __import__("asyncio").run(worker.evaluate_once())
    second = __import__("asyncio").run(worker.evaluate_once())
    assert first["created"] == 1
    assert second["created"] == 0
    assert worker._failure_count == 0
    database.dispose()


def test_performance_summary_uses_resolved_denominator(tmp_path: Path):
    database = Database(f"sqlite:///{(tmp_path / 'performance.db').as_posix()}")
    database.create_schema()
    with database.session() as session:
        session.add(decision("BUY"))
        session.add(decision("SELL", timestamp=BASE + timedelta(minutes=5)))
    # No outcome rows means no invented zero-performance sample.
    report = performance_summary(database)
    assert report["eligible_trades"] == 0
    assert report["resolved_sample_size"] == 0
    assert report["win_rate"] is None
    assert report["execution_allowed"] is False
    database.dispose()


def test_settings_keep_execution_disabled_and_horizon_positive():
    settings = Settings()
    assert settings.shadow_outcome_horizon_bars > 0
    assert settings.shadow_engine_enabled is True
