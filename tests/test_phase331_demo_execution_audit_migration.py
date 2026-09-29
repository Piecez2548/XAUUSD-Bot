from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect

from alembic import command
from persistence.database import Database
from persistence.orm import (
    DemoExecutionRecord,
    ForwardSignalRecord,
    ForwardValidationSessionRecord,
)

REVISION_0022 = "20260928_0022"
REVISION_0023 = "20260929_0023"


def _config(database_path: Path) -> Config:
    config = Config(str(Path.cwd() / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


def _insert_legacy_execution(database: Database) -> str:
    now = datetime.now(UTC)
    with database.session() as session:
        forward_session = ForwardValidationSessionRecord(
            session_id="forward-migration-legacy",
            strategy_id="pair_zone_v1",
            strategy_version="1.0.0",
            strategy_config_hash="a" * 64,
            started_at=now - timedelta(minutes=10),
            source_identity="migration-test",
            symbol="XAUUSDm",
            timeframes_json=["M5", "M15"],
            rr=2.0,
            cost_policy_json={},
            status="ACTIVE",
            execution_allowed=False,
        )
        session.add(forward_session)
        session.flush()
        signal = ForwardSignalRecord(
            signal_id="forward-migration-legacy-signal",
            session_id=forward_session.id,
            timestamp=now - timedelta(minutes=1),
            symbol="XAUUSDm",
            decision="BUY",
            zone_id="pz-migration-legacy",
            entry_price=2000.2,
            stop_loss=1990.0,
            risk_distance=10.2,
            rr=2.0,
            take_profit=2020.6,
            strategy_hash="b" * 64,
            execution_allowed=False,
        )
        session.add(signal)
        session.flush()
        record = DemoExecutionRecord(
            forward_signal_id=signal.id,
            forward_session_id=forward_session.id,
            intelligence_candidate_id="candidate-migration-legacy",
            pair_zone_event_id="pz-migration-legacy",
            setup_type="PAIR_ZONE_REJECTION",
            setup_event_id="pair-event-migration-legacy",
            symbol="XAUUSDm",
            direction="BUY",
            execution_mode="DEMO",
            status="REJECTED",
            rejection_reason="TEST_LEGACY_ROW",
            gate_reasons_json=["TEST_LEGACY_ROW"],
            planned_entry=2000.2,
            stop_loss=1990.0,
            take_profit=2020.6,
            request_json={},
        )
        session.add(record)
        session.flush()
        return record.id


def _column(database: Database, name: str) -> dict:
    return next(
        column
        for column in inspect(database.engine).get_columns("demo_execution_records")
        if column["name"] == name
    )


def test_0023_makes_pair_zone_identity_nullable_and_preserves_legacy_rows(tmp_path) -> None:
    database_path = tmp_path / "demo-execution-audit.db"
    config = _config(database_path)
    command.upgrade(config, REVISION_0022)
    database = Database(f"sqlite:///{database_path.as_posix()}")
    record_id = _insert_legacy_execution(database)
    database.dispose()

    command.upgrade(config, REVISION_0023)
    migrated = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        assert _column(migrated, "pair_zone_event_id")["nullable"] is True
        indexes = {
            index["name"]
            for index in inspect(migrated.engine).get_indexes("demo_execution_records")
        }
        assert "ix_demo_execution_records_pair_zone_event_id" in indexes
        with migrated.session() as session:
            record = session.get(DemoExecutionRecord, record_id)
            assert record is not None
            assert record.pair_zone_event_id == "pz-migration-legacy"
            assert record.rejection_reason == "TEST_LEGACY_ROW"
    finally:
        migrated.dispose()

    command.downgrade(config, REVISION_0022)
    rolled_back = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        assert _column(rolled_back, "pair_zone_event_id")["nullable"] is False
        with rolled_back.session() as session:
            assert session.get(DemoExecutionRecord, record_id) is not None
    finally:
        rolled_back.dispose()


def test_0023_downgrade_refuses_null_momentum_records(tmp_path) -> None:
    database_path = tmp_path / "demo-execution-audit-null.db"
    config = _config(database_path)
    command.upgrade(config, REVISION_0022)
    database = Database(f"sqlite:///{database_path.as_posix()}")
    _insert_legacy_execution(database)
    database.dispose()
    command.upgrade(config, REVISION_0023)

    migrated = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with migrated.session() as session:
            signal = session.query(ForwardSignalRecord).filter_by(
                signal_id="forward-migration-legacy-signal"
            ).one()
            momentum_signal = ForwardSignalRecord(
                signal_id="momentum-migration-null-signal",
                session_id=signal.session_id,
                timestamp=datetime.now(UTC),
                symbol="XAUUSDm",
                decision="SELL",
                zone_id=None,
                setup_type="MOMENTUM_BREAKOUT_V1",
                setup_event_id="momentum-migration-event",
                setup_provenance_json={
                    "setup_type": "MOMENTUM_BREAKOUT_V1",
                    "setup_event_id": "momentum-migration-event",
                    "m15_context": {},
                    "m5_trigger_candle": {},
                    "breakout_level": 2000.0,
                    "structural_sl_source": {},
                },
                entry_price=2000.0,
                stop_loss=2010.0,
                risk_distance=10.0,
                rr=2.0,
                take_profit=1980.0,
                strategy_hash="c" * 64,
                execution_allowed=False,
            )
            session.add(momentum_signal)
            session.flush()
            row = DemoExecutionRecord(
                forward_signal_id=momentum_signal.id,
                forward_session_id=signal.session_id,
                intelligence_candidate_id="momentum-migration-candidate",
                pair_zone_event_id=None,
                setup_type="MOMENTUM_BREAKOUT_V1",
                setup_event_id="momentum-migration-event",
                symbol="XAUUSDm",
                direction="SELL",
                execution_mode="DEMO",
                status="REJECTED",
                rejection_reason="TEST_MOMENTUM_ROW",
                gate_reasons_json=["TEST_MOMENTUM_ROW"],
                request_json={},
            )
            session.add(row)
            session.flush()
    finally:
        migrated.dispose()

    with pytest.raises(RuntimeError, match="Cannot downgrade"):
        command.downgrade(config, REVISION_0022)

    still_current = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        assert _column(still_current, "pair_zone_event_id")["nullable"] is True
    finally:
        still_current.dispose()
