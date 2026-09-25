from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from config.settings import Settings
from models.market import MarketSnapshot
from mt5.bootstrap import MT5StartupResult
from persistence.database import Database
from persistence.orm import MarketSnapshotRecord, RiskSnapshotRecord
from persistence.repositories import SnapshotRepository
from services.control import TelegramControlService
from services.risk import calculate_risk_snapshot


def _service(tmp_path, *, supervisor_operation_timeout_seconds=30.0):
    database = Database.for_test(f"sqlite:///{(tmp_path / 'snapshot-check.db').as_posix()}")
    database.create_test_schema()
    service = TelegramControlService(
        Settings(
            data_stale_position_seconds=30,
            supervisor_operation_timeout_seconds=supervisor_operation_timeout_seconds,
        ),
        tmp_path,
        database=database,
        supervisor=SimpleNamespace(status=lambda: {}),
        mt5_bootstrap=SimpleNamespace(),
    )
    return service, database


def _persist_cycle(service, model_parts, timestamp):
    account, symbol, tick, candles, _position = model_parts
    snapshot = MarketSnapshot(
        account=account,
        symbol=symbol,
        tick=tick,
        candles=candles,
        positions=(),
        generated_at=timestamp,
    )
    risk = calculate_risk_snapshot(
        snapshot,
        max_trade_risk_percent=2,
        max_aggregate_risk_percent=6,
    )
    result = SnapshotRepository(service.database).persist(snapshot, risk)
    return str(result.market_snapshot_id), str(result.risk_snapshot_id)


def _add_unmatched_market(database, source_market_id, timestamp):
    with database.session() as session:
        source = session.get(MarketSnapshotRecord, source_market_id)
        assert source is not None
        row = MarketSnapshotRecord(
            symbol_id=source.symbol_id,
            account_snapshot_id=source.account_snapshot_id,
            timestamp=timestamp,
            bid=source.bid,
            ask=source.ask,
            spread=source.spread,
            positions_observed_successfully=True,
            open_position_count=0,
            candle_windows=source.candle_windows,
            latest_candles=source.latest_candles,
            technical_features=None,
            market_regime=None,
            session=None,
            relevant_news_ids=None,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def _add_unmatched_risk(database, source_risk_id, timestamp):
    with database.session() as session:
        source = session.get(RiskSnapshotRecord, source_risk_id)
        assert source is not None
        row = RiskSnapshotRecord(
            market_snapshot_id=None,
            timestamp=timestamp,
            equity=source.equity,
            balance=source.balance,
            open_risk_percent=source.open_risk_percent,
            open_risk_amount=source.open_risk_amount,
            remaining_risk_percent=source.remaining_risk_percent,
            risk_per_position=source.risk_per_position,
            daily_pnl=source.daily_pnl,
            daily_realized_loss=source.daily_realized_loss,
            drawdown_percent=source.drawdown_percent,
            max_trade_risk_percent=source.max_trade_risk_percent,
            max_aggregate_risk_percent=source.max_aggregate_risk_percent,
            open_positions_count=source.open_positions_count,
            unbounded_positions_count=source.unbounded_positions_count,
            margin_usage_percent=source.margin_usage_percent,
            free_margin=source.free_margin,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def test_pre_start_coherent_snapshot_does_not_satisfy_startup(tmp_path, model_parts):
    service, database = _service(tmp_path)
    try:
        boundary = datetime.now(UTC)
        _persist_cycle(service, model_parts, boundary - timedelta(seconds=1))
        risk, _observed, freshness, _rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
        )
        assert risk is None
        assert snapshot_id is None
        assert freshness == "UNKNOWN"
        assert "PRE_START_MARKET" in diagnostic["rejection_reasons"]
        assert "PRE_START_RISK" in diagnostic["rejection_reasons"]
        assert diagnostic["boundary_filter_applied"] is True
    finally:
        database.dispose()


def test_missing_snapshot_data_has_explicit_fail_closed_reasons(tmp_path):
    service, database = _service(tmp_path)
    try:
        _risk, observed, freshness, _rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(
                minimum_timestamp=datetime.now(UTC)
            )
        )
        assert observed is None
        assert freshness == "UNKNOWN"
        assert snapshot_id is None
        assert diagnostic["rejection_reasons"] == [
            "NO_MARKET_SNAPSHOT",
            "NO_RISK_SNAPSHOT",
        ]
    finally:
        database.dispose()


def test_post_start_coherent_snapshot_is_accepted(tmp_path, model_parts):
    service, database = _service(tmp_path)
    try:
        boundary = datetime.now(UTC)
        market_id, risk_id = _persist_cycle(
            service, model_parts, boundary + timedelta(milliseconds=1)
        )
        risk, _observed, freshness, rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
        )
        assert risk is not None
        assert snapshot_id == market_id
        assert freshness == "LIVE"
        assert rows == ()
        candidate = diagnostic["selected_candidate"]
        assert candidate["market_snapshot_id"] == market_id
        assert candidate["risk_snapshot_id"] == risk_id
        assert candidate["positions_observed_successfully"] is True
        assert candidate["market_risk_count_match"] is True
        assert candidate["market_position_rows_match"] is True
        assert candidate["candidate_accepted"] is True
        assert candidate["rejection_reason"] == "CANDIDATE_ACCEPTED"
    finally:
        database.dispose()


def test_snapshot_timestamp_equal_to_startup_boundary_is_accepted(
    tmp_path, model_parts
):
    service, database = _service(tmp_path)
    try:
        boundary = datetime.now(UTC)
        market_id, _risk_id = _persist_cycle(service, model_parts, boundary)
        risk, _observed, freshness, _rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
        )
        assert risk is not None
        assert risk.timestamp == boundary
        assert snapshot_id == market_id
        assert freshness == "LIVE"
        assert datetime.fromisoformat(
            diagnostic["selected_candidate"]["market_timestamp"]
        ) == boundary
        assert diagnostic["selected_candidate"]["market_after_start_boundary"] is True
        assert diagnostic["selected_candidate"]["risk_after_start_boundary"] is True
    finally:
        database.dispose()


def test_stale_coherent_snapshot_is_not_reported_as_startup_accepted(
    tmp_path, model_parts
):
    service, database = _service(tmp_path)
    try:
        old_timestamp = datetime.now(UTC) - timedelta(minutes=2)
        _persist_cycle(service, model_parts, old_timestamp)
        _risk, _observed, freshness, _rows, _snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics()
        )
        assert freshness == "STALE"
        assert diagnostic["rejection_reason"] == "SNAPSHOT_STALE"
        assert diagnostic["selected_candidate"]["candidate_accepted"] is False
    finally:
        database.dispose()


def test_old_coherent_and_new_incomplete_market_remains_pending(tmp_path, model_parts):
    service, database = _service(tmp_path)
    try:
        boundary = datetime.now(UTC)
        market_id, _risk_id = _persist_cycle(
            service, model_parts, boundary - timedelta(seconds=1)
        )
        _add_unmatched_market(database, market_id, boundary + timedelta(milliseconds=1))
        _risk, _observed, freshness, _rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
        )
        assert snapshot_id is None
        assert freshness == "STATE_SYNC_PENDING"
        assert "PRE_START_RISK" in diagnostic["rejection_reasons"]
        assert "LATEST_MARKET_RISK_LINK_MISMATCH" in diagnostic["rejection_reasons"]
        assert diagnostic["latest_market_snapshot"]["market_after_start_boundary"] is True
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("newer_row", "expected_reason"),
    [
        ("market", "PRE_START_RISK"),
        ("risk", "PRE_START_MARKET"),
    ],
)
def test_one_side_after_boundary_fails_closed(
    tmp_path, model_parts, newer_row, expected_reason
):
    service, database = _service(tmp_path)
    try:
        boundary = datetime.now(UTC)
        market_id, risk_id = _persist_cycle(
            service, model_parts, boundary - timedelta(seconds=1)
        )
        if newer_row == "market":
            _add_unmatched_market(database, market_id, boundary + timedelta(milliseconds=1))
        else:
            _add_unmatched_risk(database, risk_id, boundary + timedelta(milliseconds=1))
        _risk, _observed, freshness, _rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
        )
        assert snapshot_id is None
        assert freshness == "STATE_SYNC_PENDING"
        assert expected_reason in diagnostic["rejection_reasons"]
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "newer_row", ["market", "risk"]
)
def test_newer_unmatched_row_rejection_is_explained(tmp_path, model_parts, newer_row):
    service, database = _service(tmp_path)
    try:
        boundary = datetime.now(UTC) - timedelta(milliseconds=5)
        market_id, risk_id = _persist_cycle(
            service, model_parts, boundary + timedelta(milliseconds=1)
        )
        if newer_row == "market":
            latest_id = _add_unmatched_market(
                database, market_id, boundary + timedelta(seconds=1)
            )
        else:
            latest_id = _add_unmatched_risk(
                database, risk_id, boundary + timedelta(seconds=1)
            )
        _risk, _observed, freshness, _rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
        )
        assert snapshot_id is None
        assert freshness == "STATE_SYNC_PENDING"
        assert diagnostic["selected_candidate"] is None
        assert "LATEST_MARKET_RISK_LINK_MISMATCH" in diagnostic["rejection_reasons"]
        latest = diagnostic[
            "latest_market_snapshot" if newer_row == "market" else "latest_risk_snapshot"
        ]
        assert latest["market_snapshot_id" if newer_row == "market" else "risk_snapshot_id"] == (
            latest_id
        )
        assert latest_id not in {market_id, risk_id}
    finally:
        database.dispose()


def test_incident_shaped_repeated_zero_position_cycles_are_accepted(tmp_path, model_parts):
    service, database = _service(tmp_path)
    try:
        boundary = datetime.now(UTC) - timedelta(seconds=20)
        ids = [
            _persist_cycle(
                service,
                model_parts,
                boundary + timedelta(seconds=offset),
            )
            for offset in (1, 6, 11, 16)
        ]
        risk, observed, freshness, rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
        )
        assert risk is not None
        assert snapshot_id == ids[-1][0]
        assert observed == risk.timestamp
        assert freshness == "LIVE"
        assert rows == ()
        assert diagnostic["candidate_evaluations_total"] == 1
        assert diagnostic["selected_candidate"]["candidate_accepted"] is True
        assert diagnostic["market_snapshot_generation_link"] == "ABSENT"
    finally:
        database.dispose()


def test_new_pair_visible_in_verification_view_is_selected(tmp_path, model_parts):
    service, database = _service(tmp_path)
    try:
        boundary = datetime.now(UTC) - timedelta(seconds=5)
        pair_a = _persist_cycle(service, model_parts, boundary + timedelta(seconds=1))
        pair_b = _persist_cycle(service, model_parts, boundary + timedelta(seconds=2))

        risk, _observed, freshness, _rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
        )

        assert risk is not None
        assert snapshot_id == pair_b[0]
        assert freshness == "LIVE"
        assert diagnostic["candidate_evaluations_total"] == 1
        assert diagnostic["selected_candidate"]["market_snapshot_id"] == pair_b[0]
        assert snapshot_id != pair_a[0]
    finally:
        database.dispose()


def test_new_pair_after_atomic_selection_does_not_reject_selected_pair(
    tmp_path, model_parts, monkeypatch
):
    service, database = _service(tmp_path)
    try:
        boundary = datetime.now(UTC) - timedelta(seconds=5)
        pair_a = _persist_cycle(service, model_parts, boundary + timedelta(seconds=1))
        original_loader = service._load_latest_snapshot_candidate
        inserted = False

        def insert_pair_after_selection(session, *, minimum_timestamp):
            nonlocal inserted
            selected = original_loader(session, minimum_timestamp=minimum_timestamp)
            if not inserted:
                inserted = True
                _persist_cycle(service, model_parts, boundary + timedelta(seconds=2))
            return selected

        monkeypatch.setattr(
            service, "_load_latest_snapshot_candidate", insert_pair_after_selection
        )
        risk, _observed, freshness, _rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
        )

        assert inserted is True
        assert risk is not None
        assert snapshot_id == pair_a[0]
        assert freshness == "LIVE"
        assert diagnostic["selected_candidate"]["market_snapshot_id"] == pair_a[0]
        assert diagnostic["selected_candidate"]["candidate_accepted"] is True
    finally:
        database.dispose()


def test_same_timestamp_candidates_have_deterministic_tie_order(tmp_path, model_parts):
    service, database = _service(tmp_path)
    try:
        timestamp = datetime.now(UTC) - timedelta(seconds=1)
        pair_ids = [
            _persist_cycle(service, model_parts, timestamp),
            _persist_cycle(service, model_parts, timestamp),
        ]
        # Equal timestamps are resolved by the stable SQLite insertion rowid.
        expected_market_id, expected_risk_id = pair_ids[-1]

        risk, _observed, freshness, _rows, snapshot_id, _diagnostic = (
            service._snapshot_context_with_diagnostics()
        )

        assert risk is not None
        assert risk.id == expected_risk_id
        assert snapshot_id == expected_market_id
        assert freshness == "LIVE"
    finally:
        database.dispose()


def test_diagnostic_failure_uses_type_only_and_does_not_change_selection(
    tmp_path, model_parts, monkeypatch, caplog
):
    service, database = _service(tmp_path)
    secret = "secret-bearing diagnostic exception"
    try:
        boundary = datetime.now(UTC) - timedelta(seconds=1)
        _persist_cycle(service, model_parts, boundary + timedelta(milliseconds=1))

        def fail_diagnostic(**_kwargs):
            raise RuntimeError(secret)

        monkeypatch.setattr(service, "_snapshot_candidate_diagnostic", fail_diagnostic)
        with caplog.at_level(logging.DEBUG):
            risk, _observed, freshness, _rows, snapshot_id, diagnostic = (
                service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
            )
        serialized = json.dumps(diagnostic)
        assert risk is not None
        assert snapshot_id is not None
        assert freshness == "LIVE"
        assert diagnostic["diagnostic_collection_error_type"] == "RuntimeError"
        assert secret not in serialized
        assert "Traceback" not in serialized
        assert secret not in caplog.text
        assert "Traceback" not in caplog.text
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "formatter_name", ["_latest_market_diagnostic", "_latest_risk_diagnostic"]
)
def test_formatter_failure_does_not_make_valid_startup_incomplete(
    tmp_path, model_parts, monkeypatch, formatter_name
):
    service, database = _service(tmp_path)
    live = SimpleNamespace(
        state="RUNNING",
        pid=123,
        process_create_time="verified-create-time",
        process_identities=((123, "verified-create-time"),),
    )

    class Supervisor:
        def status(self):
            return {
                "api": SimpleNamespace(state="RUNNING"),
                "live": live,
            }

    secret = "formatter secret must not escape"

    def fail_formatter(*_args):
        raise RuntimeError(secret)

    try:
        boundary = datetime.now(UTC) - timedelta(seconds=3)
        _persist_cycle(service, model_parts, boundary + timedelta(seconds=1))
        monkeypatch.setattr(service, formatter_name, fail_formatter)
        service.supervisor = Supervisor()
        service._startup_started_at = boundary
        service._api_responsive = lambda: _async_true()
        service._latest_service_state = lambda *_args, **_kwargs: "CONNECTED"
        service._forward_worker_state = lambda: "CONNECTED"
        service.database.healthcheck = lambda: True

        result = asyncio.run(service._wait_for_startup_verification())

        assert result["complete"] is True
        assert result["checks"]["snapshot"] == "COMPLETE"
        assert result["snapshot_diagnostic"]["diagnostic_collection_error_type"] == (
            "RuntimeError"
        )
        assert secret not in json.dumps(result["snapshot_diagnostic"])
    finally:
        database.dispose()


def test_formatter_failure_on_incomplete_start_uses_live_only_rollback(
    tmp_path, model_parts, monkeypatch
):
    from services import control as control_module

    class Supervisor:
        def __init__(self):
            self.records = {
                "api": SimpleNamespace(state="STOPPED", pid=None, last_error=None),
                "live": None,
            }
            self.started = []
            self.stopped = []

        def status(self):
            return dict(self.records)

        def start_component(self, component, _command):
            self.started.append(component)
            record = SimpleNamespace(
                state="RUNNING",
                desired_state="RUNNING",
                pid=100 if component == "api" else 200,
                last_error=None,
                parent_pid=10,
                process_tree=(),
                process_identities=(),
            )
            self.records[component] = record
            return record

        def stop_component(self, component):
            self.stopped.append(component)
            record = SimpleNamespace(
                state="STOPPED",
                desired_state="STOPPED",
                pid=None,
                last_error=None,
            )
            self.records[component] = record
            return record

    service, database = _service(
        tmp_path, supervisor_operation_timeout_seconds=0.01
    )
    supervisor = Supervisor()
    secret = "private formatter failure"

    def fail_formatter(*_args):
        raise RuntimeError(secret)

    try:
        # This coherent snapshot is deliberately before the START boundary.
        _persist_cycle(service, model_parts, datetime.now(UTC) - timedelta(seconds=2))
        service.supervisor = supervisor
        service._reconcile_supervisor = lambda: None
        service.mt5_bootstrap.ensure_ready = _async_ready_result
        service._monitoring_is_healthy = _async_false
        service._start_spawned_by_request = lambda component, *_args: component == "live"
        service._api_responsive = lambda: _async_true()
        service._latest_service_state = lambda *_args, **_kwargs: "CONNECTED"
        service._forward_worker_state = lambda: "CONNECTED"
        service.database.healthcheck = lambda: True
        monkeypatch.setattr(service, "_latest_market_diagnostic", fail_formatter)
        monkeypatch.setattr(control_module, "migrate_database", lambda *_args: None)
        monkeypatch.setattr(
            control_module, "resolve_supervised_python", lambda: "python.exe"
        )

        response = asyncio.run(service._start_infrastructure_locked())

        assert response.startswith("🟠 START INCOMPLETE")
        assert supervisor.started == ["api", "live"]
        assert supervisor.stopped == ["live"]
        assert supervisor.records["api"].state == "RUNNING"
        assert supervisor.records["live"].state == "STOPPED"
        diagnostics = (
            tmp_path / "logs" / "supervisor_control_diagnostics.jsonl"
        ).read_text(encoding="utf-8")
        assert '"diagnostic_collection_error_type": "RuntimeError"' in diagnostics
        assert secret not in diagnostics
    finally:
        database.dispose()


@pytest.mark.parametrize("mismatch", ["counts", "position_rows"])
def test_candidate_coherence_rejection_has_explicit_reason(
    tmp_path, model_parts, mismatch
):
    service, database = _service(tmp_path)
    try:
        boundary = datetime.now(UTC) - timedelta(seconds=1)
        market_id, risk_id = _persist_cycle(
            service, model_parts, boundary + timedelta(milliseconds=1)
        )
        with database.session() as session:
            market = session.get(MarketSnapshotRecord, market_id)
            risk = session.get(RiskSnapshotRecord, risk_id)
            assert market is not None and risk is not None
            if mismatch == "counts":
                risk.open_positions_count = 1
            else:
                market.open_position_count = 1
                risk.open_positions_count = 1
        _risk, _observed, freshness, _rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
        )
        assert snapshot_id is None
        assert freshness == "STATE_SYNC_PENDING"
        candidate = diagnostic["selected_candidate"]
        expected = (
            "MARKET_RISK_POSITION_COUNT_MISMATCH"
            if mismatch == "counts"
            else "POSITION_ROW_COUNT_MISMATCH"
        )
        assert expected in candidate["rejection_reasons"]
    finally:
        database.dispose()


def test_unverified_positions_are_explicitly_rejected(tmp_path, model_parts):
    service, database = _service(tmp_path)
    try:
        boundary = datetime.now(UTC) - timedelta(seconds=1)
        market_id, _risk_id = _persist_cycle(
            service, model_parts, boundary + timedelta(milliseconds=1)
        )
        with database.session() as session:
            session.get(MarketSnapshotRecord, market_id).positions_observed_successfully = False
        _risk, _observed, freshness, _rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(minimum_timestamp=boundary)
        )
        assert snapshot_id is None
        assert freshness == "STATE_SYNC_PENDING"
        assert diagnostic["rejection_reason"] == "POSITIONS_NOT_VERIFIED"
    finally:
        database.dispose()


def test_candidate_diagnostic_history_is_bounded(tmp_path, model_parts):
    service, database = _service(tmp_path)
    try:
        now = datetime.now(UTC)
        for index in range(10):
            _persist_cycle(
                service,
                model_parts,
                now - timedelta(seconds=20 - index),
            )
        _risk, _observed, freshness, _rows, snapshot_id, diagnostic = (
            service._snapshot_context_with_diagnostics(
                minimum_timestamp=now + timedelta(seconds=1)
            )
        )
        assert snapshot_id is None
        assert freshness == "UNKNOWN"
        assert diagnostic["candidate_evaluations_total"] == 0
        assert len(diagnostic["candidate_evaluations"]) == 0
        assert diagnostic["rejection_reason"] in {"PRE_START_MARKET", "PRE_START_RISK"}
    finally:
        database.dispose()


def test_startup_verification_persists_one_bounded_snapshot_summary(
    tmp_path, monkeypatch
):
    from services import control as control_module

    class Supervisor:
        def status(self):
            return {"api": SimpleNamespace(state="STOPPED"), "live": None}

        def start_component(self, component, _command):
            return SimpleNamespace(state="RUNNING", last_error=None, pid=123)

    service, database = _service(tmp_path)
    service.supervisor = Supervisor()
    service.mt5_bootstrap.ensure_ready = _async_ready_result
    service._monitoring_is_healthy = _async_false
    service._start_spawned_by_request = lambda *_args: True
    service._stop_failed_start = lambda *_args, **_kwargs: None
    diagnostic = {
        "rejection_reason": "MARKET_RISK_POSITION_COUNT_MISMATCH",
        "candidate_evaluations": [{"market_snapshot_id": "safe-id"}],
    }
    service._wait_for_startup_verification = _async_verification_incomplete(diagnostic)
    monkeypatch.setattr(control_module, "migrate_database", lambda *_args: None)
    monkeypatch.setattr(control_module, "resolve_supervised_python", lambda: "python.exe")
    try:
        response = asyncio.run(service._start_infrastructure_locked())
        assert response.startswith("🟠 START INCOMPLETE")
        rows = [
            json.loads(line)
            for line in (
                tmp_path / "logs" / "supervisor_control_diagnostics.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        verification = [row for row in rows if row["stage"] == "startup_verification"]
        assert len(verification) == 1
        assert verification[0]["details"]["snapshot_verification"] == diagnostic
        assert verification[0]["details"]["snapshot_verification"]["rejection_reason"] == (
            "MARKET_RISK_POSITION_COUNT_MISMATCH"
        )
    finally:
        database.dispose()


async def _async_ready_result(_database):
    return MT5StartupResult(ready=True, launch_state="VERIFIED", pid=None, checks={})


async def _async_false():
    return False


async def _async_true():
    return True


def _async_verification_incomplete(diagnostic):
    async def result():
        return {
            "complete": False,
            "checks": {
                "api": "OK",
                "live_runtime": "CONNECTED",
                "mt5": "CONNECTED",
                "snapshot": "STATE_SYNC_PENDING",
                "database": "CONNECTED",
                "forward": "CONNECTED",
            },
            "snapshot_diagnostic": diagnostic,
        }

    return result


def test_startup_verifier_passes_boundary_and_returns_latest_diagnostic(
    tmp_path,
):
    live = SimpleNamespace(
        state="RUNNING",
        pid=123,
        process_create_time="verified-create-time",
        process_identities=((123, "verified-create-time"),),
    )

    class Supervisor:
        def status(self):
            return {
                "api": SimpleNamespace(state="RUNNING"),
                "live": live,
            }

    service, database = _service(tmp_path)
    boundary = datetime.now(UTC)
    calls = []
    diagnostic = {"rejection_reason": "CANDIDATE_ACCEPTED"}

    async def api_ready():
        return True

    def snapshot_context(*, minimum_timestamp):
        calls.append(minimum_timestamp)
        return None, boundary, "LIVE", (), "market-id", dict(diagnostic)

    service.supervisor = Supervisor()
    service._startup_started_at = boundary
    service._api_responsive = api_ready
    service._latest_service_state = lambda *_args, **_kwargs: "CONNECTED"
    service._snapshot_context_with_diagnostics = snapshot_context
    service._forward_worker_state = lambda: "CONNECTED"
    service.database.healthcheck = lambda: True
    try:
        result = asyncio.run(service._wait_for_startup_verification())
        assert calls == [boundary]
        assert result["complete"] is True
        assert result["snapshot_diagnostic"]["supervisor_live_identity_present"] is True
        assert result["snapshot_diagnostic"]["supervisor_live_identity_verified"] is True
    finally:
        database.dispose()
