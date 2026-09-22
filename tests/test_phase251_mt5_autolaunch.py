from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from config.settings import Settings
from mt5.bootstrap import (
    MT5AutoLauncher,
    MT5BootstrapError,
    MT5StartupResult,
    MT5Verification,
    TerminalProcess,
    verify_mt5_readiness,
)
from persistence.database import Database
from persistence.orm import ForwardValidationSessionRecord, SystemHealthRecord
from services.control import TelegramControlService


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'control.db').as_posix()}")
    database.create_schema()
    return database


def _verification(_settings: Settings, _database: Database) -> MT5Verification:
    return MT5Verification(
        account_login=123, account_server="Demo", symbol="XAUUSD", candle_count=2
    )


class FakePopen:
    next_pid = 9001

    def __init__(self, state: dict[str, object], *_args, **_kwargs) -> None:
        self.pid = FakePopen.next_pid
        FakePopen.next_pid += 1
        self.state = state
        state["pid"] = self.pid

    def poll(self) -> None:
        return None


def _fake_settings(path: Path, **kwargs) -> Settings:
    values = {
        "mt5_auto_launch": True,
        "mt5_terminal_path": str(path),
        "mt5_startup_timeout_seconds": 1.0,
    }
    values.update(kwargs)
    return Settings(**values)


def test_mt5_already_running_is_reused_without_launch(tmp_path: Path) -> None:
    path = tmp_path / "terminal64.exe"
    path.write_bytes(b"placeholder")
    calls = {"popen": 0}
    settings = _fake_settings(path)
    launcher = MT5AutoLauncher(
        settings,
        process_probe=lambda: (TerminalProcess(77),),
        popen_factory=lambda *_args, **_kwargs: calls.__setitem__("popen", calls["popen"] + 1),
        verifier=_verification,
    )
    result = asyncio.run(launcher.ensure_ready(_database(tmp_path)))
    assert result.ready is True
    assert result.launch_state == "REUSED"
    assert result.pid == 77
    assert calls["popen"] == 0


def test_mt5_absent_is_launched_once_and_repeated_start_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "terminal64.exe"
    path.write_bytes(b"placeholder")
    state: dict[str, object] = {}
    calls = {"popen": 0}

    def probe() -> tuple[TerminalProcess, ...]:
        return (TerminalProcess(int(state["pid"])),) if "pid" in state else ()

    def popen(*args, **kwargs):
        calls["popen"] += 1
        return FakePopen(state, *args, **kwargs)

    database = _database(tmp_path)
    launcher = MT5AutoLauncher(
        _fake_settings(path), process_probe=probe, popen_factory=popen, verifier=_verification
    )
    first = asyncio.run(launcher.ensure_ready(database))
    second = asyncio.run(launcher.ensure_ready(database))
    assert first.ready and second.ready
    assert first.launch_state == "STARTED"
    assert second.launch_state == "REUSED"
    assert calls["popen"] == 1
    database.dispose()


def test_mt5_launch_timeout_fails_closed_without_second_launch(tmp_path: Path) -> None:
    path = tmp_path / "terminal64.exe"
    path.write_bytes(b"placeholder")
    state: dict[str, object] = {}
    calls = {"popen": 0}

    async def fast_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    database = _database(tmp_path)
    launcher = MT5AutoLauncher(
        _fake_settings(path, mt5_startup_timeout_seconds=0.01),
        process_probe=lambda: (),
        popen_factory=lambda *_args, **_kwargs: (
            calls.__setitem__("popen", calls["popen"] + 1) or FakePopen(state)
        ),
        verifier=_verification,
        sleep=fast_sleep,
    )
    first = asyncio.run(launcher.ensure_ready(database))
    second = asyncio.run(launcher.ensure_ready(database))
    assert first.ready is False and second.ready is False
    assert "timed out" in (first.reason or "")
    assert calls["popen"] == 1
    database.dispose()


def test_mt5_connection_failure_fails_closed() -> None:
    settings = Settings(mt5_auto_launch=False, mt5_startup_timeout_seconds=0.01)
    launcher = MT5AutoLauncher(
        settings,
        process_probe=lambda: (TerminalProcess(1),),
        verifier=lambda *_args: (_ for _ in ()).throw(
            MT5BootstrapError("terminal is not connected")
        ),
    )
    result = asyncio.run(launcher.ensure_ready(object()))
    assert result.ready is False
    assert result.launch_state == "REUSED"
    assert result.reason == "terminal is not connected"


class FakeMT5:
    TIMEFRAME_M5 = 5

    def __init__(self, *, login: int = 123, symbols: bool = True, candles: bool = True) -> None:
        self.login = login
        self.symbols = symbols
        self.candles = candles
        self.shutdown_calls = 0

    def initialize(self, **_kwargs):
        return True

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def account_info(self):
        return SimpleNamespace(
            login=self.login, server="Demo", balance=1, equity=1, margin=0,
            margin_free=1, margin_level=0, profit=0, leverage=100, currency="USD",
        )

    def symbols_get(self):
        return (
            SimpleNamespace(
                name="XAUUSD", currency_base="XAU", currency_profit="USD", description="Gold"
            ),
        ) if self.symbols else ()

    def symbol_info(self, _symbol):
        return SimpleNamespace(
            name="XAUUSD", visible=True, bid=2000, ask=2000.2, spread=20, digits=2,
            point=0.01, trade_tick_size=0.01, trade_tick_value=1,
            trade_tick_value_profit=1, trade_tick_value_loss=1,
            trade_contract_size=100, volume_min=0.01, volume_max=100,
            volume_step=0.01, trade_mode=4,
        )

    def symbol_info_tick(self, _symbol):
        return SimpleNamespace(time=int(datetime.now(UTC).timestamp()), bid=2000, ask=2000.2,
                               last=2000.1, volume=1, flags=0)

    def copy_rates_from_pos(self, _symbol, _timeframe, _position, _count):
        if not self.candles:
            return None
        start = int(datetime.now(UTC).timestamp()) - 600
        return [
            {"time": start, "open": 2000, "high": 2001, "low": 1999, "close": 2000.5,
             "tick_volume": 1, "spread": 20, "real_volume": 1},
            {"time": start + 300, "open": 2000.5, "high": 2002, "low": 2000,
             "close": 2001, "tick_volume": 1, "spread": 20, "real_volume": 1},
        ]

    @staticmethod
    def last_error():
        return (1, "fake error")

    def shutdown(self):
        self.shutdown_calls += 1


def test_expected_account_mismatch_fails_closed() -> None:
    from unittest.mock import patch

    settings = Settings(mt5_login=999, mt5_server="Demo", mt5_password="secret")
    with (
        patch("mt5.connection.importlib.import_module", return_value=FakeMT5(login=123)),
        pytest.raises(MT5BootstrapError, match="expected account validation failed"),
    ):
        verify_mt5_readiness(settings, SimpleNamespace(healthcheck=lambda: True))


def test_symbol_unavailable_and_market_data_failure_fail_closed() -> None:
    database = SimpleNamespace(healthcheck=lambda: True)
    with pytest.raises(MT5BootstrapError, match="gold symbols"):
        from unittest.mock import patch

        with patch("mt5.connection.importlib.import_module", return_value=FakeMT5(symbols=False)):
            verify_mt5_readiness(Settings(), database)
    with pytest.raises(MT5BootstrapError, match="candle"):
        from unittest.mock import patch

        with patch("mt5.connection.importlib.import_module", return_value=FakeMT5(candles=False)):
            verify_mt5_readiness(Settings(), database)


class FakeBootstrap:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def ensure_ready(self, _database):
        self.calls += 1
        return self.result


def test_start_fails_closed_before_supervised_workers_when_mt5_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    settings = Settings()
    supervisor = FakeSupervisor(healthy=False)
    failed = MT5StartupResult(
        ready=False, launch_state="FAILED", pid=None,
        checks={"mt5_process": "FAILED"}, reason="MT5 terminal startup timed out",
    )
    bootstrap = FakeBootstrap(failed)
    service = TelegramControlService(
        settings, tmp_path, database=database, supervisor=supervisor, mt5_bootstrap=bootstrap
    )
    monkeypatch.setattr("services.control.migrate_database", lambda *_args: None)
    response = asyncio.run(service._start_infrastructure_locked())
    assert "SYSTEM NOT READY" in response
    assert "MT5 terminal startup timed out" in response
    assert supervisor.started == []
    database.dispose()


class FakeSupervisor:
    def __init__(self, *, healthy: bool):
        self.healthy = healthy
        self.started: list[str] = []
        self.stopped: list[str] = []

    def status(self):
        state = "RUNNING" if self.healthy else "STOPPED"
        return {"api": SimpleNamespace(state=state), "live": SimpleNamespace(state=state)}

    def start_component(self, component, _command):
        self.started.append(component)
        return SimpleNamespace(component=component, state="RUNNING")

    def stop_component(self, component):
        self.stopped.append(component)
        return SimpleNamespace(component=component, state="STOPPED")


def _ready_result() -> MT5StartupResult:
    return MT5StartupResult(
        ready=True, launch_state="REUSED", pid=11,
        checks={"mt5_process": "REUSED", "terminal": "CONNECTED", "account": "VERIFIED",
                "symbol": "AVAILABLE", "market_data": "READY", "database": "CONNECTED"},
        verification=_verification(Settings(), object()),
    )


def test_healthy_monitoring_start_is_idempotent_and_stop_does_not_touch_mt5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    settings = Settings(telegram_control_enabled=True, forward_shadow_enabled=False)
    supervisor = FakeSupervisor(healthy=True)
    bootstrap = FakeBootstrap(_ready_result())
    service = TelegramControlService(
        settings, tmp_path, database=database, supervisor=supervisor, mt5_bootstrap=bootstrap
    )
    monkeypatch.setattr("services.control.migrate_database", lambda *_args: None)
    now = datetime.now(UTC)
    with database.session() as session:
        session.add(SystemHealthRecord(
            component="live_runtime", timestamp=now, status="CONNECTED", message="ok"
        ))
    monkeypatch.setattr(service, "_api_responsive", lambda: asyncio.sleep(0, result=True))
    response = asyncio.run(service._start_infrastructure_locked())
    assert "ALREADY RUNNING" in response
    assert supervisor.started == []
    asyncio.run(service._stop_infrastructure_locked())
    assert supervisor.stopped == ["live", "api"]
    assert bootstrap.calls == 1
    database.dispose()


def test_forward_session_record_is_not_reset_by_stop_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    settings = Settings(forward_shadow_enabled=False)
    supervisor = FakeSupervisor(healthy=False)
    bootstrap = FakeBootstrap(_ready_result())
    service = TelegramControlService(
        settings, tmp_path, database=database, supervisor=supervisor, mt5_bootstrap=bootstrap
    )
    monkeypatch.setattr("services.control.migrate_database", lambda *_args: None)
    session_id = "forward_preserved"
    started = datetime(2026, 9, 22, tzinfo=UTC)
    with database.session() as session:
        session.add(ForwardValidationSessionRecord(
            session_id=session_id, strategy_id="pair_zone_v1", strategy_version="1.0.0",
            strategy_config_hash="a" * 64, started_at=started, source_identity="test",
            symbol="XAUUSD", timeframes_json=["M5", "M15", "H1"], rr=2.0,
            cost_policy_json={}, status="ACTIVE", execution_allowed=False,
        ))
    monkeypatch.setattr(service, "_wait_for_startup_verification", lambda: asyncio.sleep(0, result={
        "complete": True,
        "checks": {"api": "OK", "live_runtime": "CONNECTED", "mt5": "CONNECTED",
                    "snapshot": "COMPLETE", "database": "CONNECTED", "forward": "DISABLED"},
    }))
    asyncio.run(service._stop_infrastructure_locked())
    asyncio.run(service._start_infrastructure_locked())
    with database.session() as session:
        row = session.scalar(select(ForwardValidationSessionRecord).where(
            ForwardValidationSessionRecord.session_id == session_id
        ))
        assert row is not None
        assert row.session_id == session_id
        assert row.started_at == started
        assert row.execution_allowed is False
    database.dispose()
