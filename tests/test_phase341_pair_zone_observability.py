from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from config.settings import Settings
from models.market import (
    AccountState,
    Candle,
    MarketSnapshot,
    SymbolSpecification,
    Tick,
    Timeframe,
)
from models.observatory import RiskSnapshot
from persistence.database import Database
from persistence.orm import (
    ForwardValidationSessionRecord,
    PairZoneEvaluationRecord,
)
from services.control import TelegramControlService
from services.forward_shadow import (
    EXPECTED_PAIR_ZONE_FILE_SHA256,
    ForwardInput,
    ForwardShadowWorker,
    pair_zone_status,
)


def _db(tmp_path) -> Database:
    db = Database.for_test(f"sqlite:///{(tmp_path / 'pair-zone-observability.db').as_posix()}")
    db.create_test_schema()
    return db


def _session(db: Database, *, config_hash: str = EXPECTED_PAIR_ZONE_FILE_SHA256):
    with db.session() as session:
        row = ForwardValidationSessionRecord(
            session_id="forward-observability-test",
            strategy_id="pair_zone_v1",
            strategy_version="1.0.0",
            strategy_config_hash=config_hash,
            started_at=datetime.now(UTC) - timedelta(hours=1),
            source_identity="test",
            symbol="XAUUSDm",
            timeframes_json=["M5", "M15", "H1"],
            rr=2.0,
            cost_policy_json={},
            status="ACTIVE",
            execution_allowed=False,
        )
        session.add(row)
        session.flush()
        return row


def _snapshot():
    now = datetime.now(UTC)
    m5 = now - timedelta(minutes=5)
    m15 = now - timedelta(minutes=15)
    return SimpleNamespace(candles={
        # Detector persistence needs only the evaluated candle identities.
        Timeframe.M5: (SimpleNamespace(timestamp=m5),),
        Timeframe.M15: (SimpleNamespace(timestamp=m15),),
    })


def _canonical_snapshot():
    now = datetime.now(UTC)
    m5_open = now.replace(
        minute=(now.minute // 5) * 5, second=0, microsecond=0
    ) - timedelta(minutes=5)
    m15_open = now.replace(
        minute=(now.minute // 15) * 15, second=0, microsecond=0
    ) - timedelta(minutes=15)

    def candle(timestamp, open_price=100.0, high=101.0, low=99.0, close=100.0):
        return Candle(
            timestamp=timestamp,
            raw_timestamp=int(timestamp.timestamp()),
            open=open_price,
            high=high,
            low=low,
            close=close,
            tick_volume=100,
            spread=20,
            real_volume=0,
        )

    m15 = tuple(
        candle(m15_open - timedelta(minutes=15 * index))
        for index in reversed(range(26))
    )
    h1_open = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    h1 = tuple(
        candle(
            h1_open - timedelta(hours=20 - index),
            100 + index - 0.2,
            101 + index,
            99 + index,
            100 + index,
        )
        for index in range(21)
    )
    m5 = (candle(m5_open),)
    h4 = (candle(h1_open - timedelta(hours=3), 100, 122, 98, 120),)
    account = AccountState(
        balance=10_000,
        equity=10_000,
        margin=0,
        free_margin=10_000,
        margin_level=0,
        profit=0,
        leverage=100,
        currency="USD",
        server="test",
        trade_mode=0,
        trade_mode_name="demo",
    )
    symbol = SymbolSpecification(
        name="XAUUSDm",
        bid=100,
        ask=100.2,
        spread=20,
        digits=2,
        point=0.01,
        trade_tick_size=0.01,
        trade_tick_value=1,
        trade_tick_value_profit=1,
        trade_tick_value_loss=1,
        contract_size=100,
        volume_min=0.01,
        volume_max=100,
        volume_step=0.01,
        trade_mode=4,
        trade_mode_name="test",
    )
    return MarketSnapshot(
        account=account,
        symbol=symbol,
        tick=Tick(
            timestamp=m5_open,
            raw_timestamp=int(m5_open.timestamp()),
            bid=100,
            ask=100.2,
            last=100.1,
            volume=1,
            flags=0,
        ),
        positions=(),
        candles={
            Timeframe.H4: h4,
            Timeframe.H1: h1,
            Timeframe.M15: m15,
            Timeframe.M5: m5,
        },
        generated_at=now,
    )


def _health(worker: ForwardShadowWorker, session_id: str, snapshot, *, state="CONNECTED"):
    from models.market import Timeframe

    return {
        "state": state,
        "session_id": session_id,
        "runtime_generation_id": worker.runtime_generation_id,
        "pid": os.getpid(),
        "last_closed_m5": snapshot.candles[Timeframe.M5][-1].timestamp.isoformat(),
        "last_closed_m15": snapshot.candles[Timeframe.M15][-1].timestamp.isoformat(),
    }


def _verified_process_identities():
    return ((os.getpid(), "test-process-create-time"),)


def _persist(worker, snapshot, state, *, reason="TEST_EVALUATION"):
    decision = SimpleNamespace(
        m5_candle_timestamp=snapshot.candles[Timeframe.M5][-1].timestamp,
        feature_context={"pair_zone_observation": {
            "state": state,
            "reason": reason,
            "direction": "BUY" if state == "ACTIVE_ZONE" else None,
            "zone_id": "pz-test" if state == "ACTIVE_ZONE" else None,
            "zone_lower": 2300.0 if state == "ACTIVE_ZONE" else None,
            "zone_upper": 2301.0 if state == "ACTIVE_ZONE" else None,
        }},
    )
    worker._persist_pair_zone_evaluation(decision, snapshot)


def test_active_zone_and_healthy_no_zone_are_authoritative_and_bounded(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    worker = ForwardShadowWorker(Settings(), db, logger=logging.getLogger("pz-state"))
    worker.session = session
    snapshot = _snapshot()
    health = _health(worker, session.session_id, snapshot)

    _persist(worker, snapshot, "ACTIVE_ZONE")
    active = pair_zone_status(
        db, Settings(), session_id=session.session_id,
        forward_health_payload=health,
        verified_live_process_identities=_verified_process_identities(),
    )
    assert active["state"] == "ACTIVE_ZONE"
    assert active["current_direction"] == "BUY"
    assert active["zone_id"] == "pz-test"
    assert active["zone_lower"] == 2300.0

    _persist(worker, snapshot, "HEALTHY_NO_ACTIVE_ZONE", reason="NO_VALID_ZONE")
    empty = pair_zone_status(
        db, Settings(), session_id=session.session_id,
        forward_health_payload=health,
        verified_live_process_identities=_verified_process_identities(),
    )
    assert empty["state"] == "HEALTHY_NO_ACTIVE_ZONE"
    assert empty["current_direction"] == "NONE"
    assert empty["zone_id"] is None
    with db.session() as db_session:
        assert len(list(db_session.scalars(select(PairZoneEvaluationRecord)))) == 1
    db.dispose()


@pytest.mark.parametrize(
    "failure",
    [
        "missing", "stale", "stale_candle", "config", "session_config",
        "session", "generation", "worker",
    ],
)
def test_pair_zone_state_fails_closed_on_unverified_provenance(tmp_path, failure):
    db = _db(tmp_path)
    session = _session(db)
    worker = ForwardShadowWorker(Settings(), db, logger=logging.getLogger("pz-state"))
    worker.session = session
    snapshot = _snapshot()
    _persist(worker, snapshot, "HEALTHY_NO_ACTIVE_ZONE", reason="NO_VALID_ZONE")
    health = _health(worker, session.session_id, snapshot)
    now = datetime.now(UTC)

    if failure == "missing":
        with db.session() as db_session:
            db_session.execute(delete(PairZoneEvaluationRecord))
    elif failure == "stale":
        with db.session() as db_session:
            record = db_session.get(PairZoneEvaluationRecord, session.id)
            record.evaluation_at = now - timedelta(minutes=16)
    elif failure == "stale_candle":
        old_m5 = now - timedelta(hours=2)
        old_m15 = now - timedelta(hours=2)
        with db.session() as db_session:
            record = db_session.get(PairZoneEvaluationRecord, session.id)
            record.evaluated_m5_timestamp = old_m5
            record.evaluated_m15_timestamp = old_m15
        health["last_closed_m5"] = old_m5.isoformat()
        health["last_closed_m15"] = old_m15.isoformat()
    elif failure == "config":
        with db.session() as db_session:
            record = db_session.get(PairZoneEvaluationRecord, session.id)
            record.config_hash = "0" * 64
    elif failure == "session_config":
        with db.session() as db_session:
            record = db_session.get(ForwardValidationSessionRecord, session.id)
            record.strategy_config_hash = "0" * 64
    elif failure == "session":
        health["session_id"] = "another-forward-session"
    elif failure == "generation":
        health["runtime_generation_id"] = "another-generation"
    elif failure == "worker":
        health["state"] = "DEGRADED"

    result = pair_zone_status(
        db, Settings(), session_id=session.session_id,
        forward_health_payload=health,
        verified_live_process_identities=_verified_process_identities(),
        now=now,
    )
    assert result["state"] == "UNKNOWN"
    db.dispose()


def test_new_worker_generation_does_not_hydrate_old_evaluation(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    old_worker = ForwardShadowWorker(Settings(), db, logger=logging.getLogger("pz-old"))
    old_worker.session = session
    snapshot = _snapshot()
    _persist(old_worker, snapshot, "HEALTHY_NO_ACTIVE_ZONE", reason="NO_VALID_ZONE")

    restarted_worker = ForwardShadowWorker(
        Settings(), db, logger=logging.getLogger("pz-new")
    )
    restarted_worker.session = session
    assert restarted_worker.runtime_generation_id != old_worker.runtime_generation_id
    result = pair_zone_status(
        db, Settings(), session_id=session.session_id,
        forward_health_payload=_health(restarted_worker, session.session_id, snapshot),
        verified_live_process_identities=_verified_process_identities(),
    )
    assert result["state"] == "UNKNOWN"
    assert result["reason"] == "SESSION_GENERATION_OR_CONFIG_MISMATCH"
    db.dispose()


def test_control_status_exposes_only_verified_persisted_pair_zone_state(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    settings = Settings(forward_shadow_enabled=True)
    worker = ForwardShadowWorker(settings, db, logger=logging.getLogger("pz-control"))
    worker.session = session
    snapshot = _snapshot()
    _persist(worker, snapshot, "HEALTHY_NO_ACTIVE_ZONE", reason="NO_VALID_ZONE")
    worker._last_closed_m5 = snapshot.candles[Timeframe.M5][-1].timestamp
    worker._last_closed_m15 = snapshot.candles[Timeframe.M15][-1].timestamp
    worker._record_health("CONNECTED", "test worker", force=True)

    class Supervisor:
        def status(self):
            return {
                "api": SimpleNamespace(state="RUNNING"),
                "live": SimpleNamespace(
                    state="RUNNING",
                    process_identities=_verified_process_identities(),
                ),
            }

    service = TelegramControlService(
        settings,
        tmp_path,
        database=db,
        supervisor=Supervisor(),
        mt5_bootstrap=SimpleNamespace(),
    )
    status = service._operator_status_payload()
    assert status["strategy"]["pair_zone_state"] == "HEALTHY_NO_ACTIVE_ZONE"
    assert status["strategy"]["current_direction"] == "NONE"
    assert status["strategy"]["pair_zone_reason"] == "NO_VALID_ZONE"
    db.dispose()


def test_detector_failure_persists_unknown_then_valid_evaluation_recovers(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    worker = ForwardShadowWorker(Settings(), db, logger=logging.getLogger("pz-failure"))
    worker.session = session
    snapshot = _snapshot()
    worker._persist_pair_zone_evaluation(
        None, snapshot, reason_override="DETECTOR_EXCEPTION"
    )
    health = _health(worker, session.session_id, snapshot)
    failed = pair_zone_status(
        db, Settings(), session_id=session.session_id,
        forward_health_payload=health,
        verified_live_process_identities=_verified_process_identities(),
    )
    assert failed["state"] == "UNKNOWN"
    assert failed["reason"] == "DETECTOR_EXCEPTION"

    _persist(worker, snapshot, "HEALTHY_NO_ACTIVE_ZONE", reason="NO_VALID_ZONE")
    recovered = pair_zone_status(
        db, Settings(), session_id=session.session_id,
        forward_health_payload=health,
        verified_live_process_identities=_verified_process_identities(),
    )
    assert recovered["state"] == "HEALTHY_NO_ACTIVE_ZONE"
    db.dispose()


def test_process_records_detector_exception_as_unknown(tmp_path, monkeypatch):
    db = _db(tmp_path)
    session = _session(db)
    worker = ForwardShadowWorker(Settings(), db, logger=logging.getLogger("pz-exception"))
    worker.session = session
    snapshot = _snapshot()
    snapshot.symbol = SimpleNamespace(name="XAUUSDm")
    monkeypatch.setattr(worker, "_ensure_session_symbol", lambda _symbol: None)

    def fail_detector(*_args, **_kwargs):
        raise RuntimeError("test detector failure")

    monkeypatch.setattr(worker.strategy, "evaluate", fail_detector)
    with pytest.raises(RuntimeError, match="test detector failure"):
        asyncio.run(worker._process(ForwardInput(snapshot, "snapshot-1", risk=None)))

    with db.session() as db_session:
        row = db_session.get(PairZoneEvaluationRecord, session.id)
        assert row is not None
        assert row.state == "UNKNOWN"
        assert row.reason == "DETECTOR_EXCEPTION"
        assert row.runtime_generation_id == worker.runtime_generation_id
    db.dispose()


def test_forward_process_persists_actual_canonical_no_zone_evaluation(tmp_path, monkeypatch):
    db = _db(tmp_path)
    session = _session(db)
    worker = ForwardShadowWorker(Settings(), db, logger=logging.getLogger("pz-canonical"))
    worker.session = session
    snapshot = _canonical_snapshot()
    worker._ensure_session_symbol = lambda _symbol: None

    async def no_open_trade_evaluation():
        return None

    worker._evaluate_open_trades = no_open_trade_evaluation
    worker._record_health = lambda *_args, **_kwargs: None

    class NoopIntelligence:
        def __init__(self, _settings):
            pass

        def evaluate(self, *_args, **_kwargs):
            return SimpleNamespace(candidate=SimpleNamespace(execution_allowed=False))

    monkeypatch.setattr("services.intelligence.StrategyIntelligenceEngine", NoopIntelligence)
    monkeypatch.setattr(
        "services.intelligence.persist_intelligence_record",
        lambda *_args, **_kwargs: None,
    )
    risk = RiskSnapshot(
        equity=10_000,
        balance=10_000,
        open_risk_percent=0,
        open_risk_amount=0,
        remaining_risk_percent=6,
        risk_per_position=(),
        max_trade_risk_percent=2,
        max_aggregate_risk_percent=6,
        open_positions_count=0,
        unbounded_positions_count=0,
        free_margin=10_000,
    )

    asyncio.run(worker._process(ForwardInput(snapshot, str(uuid4()), risk)))

    with db.session() as db_session:
        row = db_session.get(PairZoneEvaluationRecord, session.id)
        assert row is not None
        assert row.state == "HEALTHY_NO_ACTIVE_ZONE"
        assert row.reason == "NO_VALID_ZONE"
        assert row.evaluated_m15_timestamp == snapshot.candles[Timeframe.M15][-1].timestamp
        assert row.runtime_generation_id == worker.runtime_generation_id
    db.dispose()
