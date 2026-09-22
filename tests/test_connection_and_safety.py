from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from config.settings import Settings
from mt5.connection import MT5Connection, MT5ConnectionError
from utils.logging import configure_logging


class FakeMT5:
    def __init__(self, *, initialize: bool = True, connected: bool = True) -> None:
        self.initialize_result = initialize
        self.connected = connected
        self.shutdown_calls = 0
        self.initialize_kwargs = None

    def initialize(self, **kwargs):
        self.initialize_kwargs = kwargs
        return self.initialize_result

    def terminal_info(self):
        return SimpleNamespace(connected=self.connected)

    def account_info(self):
        return SimpleNamespace(login=123)

    @staticmethod
    def last_error():
        return (1, "test error")

    def shutdown(self):
        self.shutdown_calls += 1


def test_connection_uses_current_terminal_by_default_and_shuts_down() -> None:
    module = FakeMT5()
    with MT5Connection(Settings(), module=module) as connection:
        assert connection.api is module
        assert module.initialize_kwargs == {}
    assert module.shutdown_calls == 1


def test_failed_terminal_verification_shuts_down() -> None:
    module = FakeMT5(connected=False)
    with pytest.raises(MT5ConnectionError, match="not connected"):
        MT5Connection(Settings(), module=module).connect()
    assert module.shutdown_calls == 1


def test_failed_initialization_shuts_down() -> None:
    module = FakeMT5(initialize=False)
    with pytest.raises(MT5ConnectionError, match="initialization failed"):
        MT5Connection(Settings(), module=module).connect()
    assert module.shutdown_calls == 1


def test_explicit_credentials_are_passed_without_being_in_settings_repr() -> None:
    settings = Settings(mt5_login=123, mt5_server="Demo", mt5_password="very-secret")
    module = FakeMT5()
    MT5Connection(settings, module=module).connect().shutdown()
    assert module.initialize_kwargs["password"] == "very-secret"
    assert "very-secret" not in repr(settings)


def test_structured_logs_redact_secrets_from_messages_and_tracebacks(tmp_path: Path) -> None:
    secret = "very-secret"
    logger = configure_logging(tmp_path, secrets=(secret,))
    try:
        raise RuntimeError(f"third-party failure contained {secret}")
    except RuntimeError:
        logger.exception("connection failed with %s", secret)

    output = (tmp_path / "phase1.jsonl").read_text(encoding="utf-8")
    assert secret not in output
    assert "[REDACTED]" in output


def test_production_code_contains_no_trade_execution_calls() -> None:
    project_root = Path(__file__).resolve().parents[1]
    production_files = [project_root / "main.py"]
    for package in ("config", "models", "mt5", "utils"):
        production_files.extend((project_root / package).glob("*.py"))

    forbidden_calls = (
        ".order_send(",
        ".order_check(",
        ".positions_close(",
        ".position_modify(",
    )
    for file_path in production_files:
        source = file_path.read_text(encoding="utf-8")
        for forbidden in forbidden_calls:
            assert forbidden not in source, f"{file_path} contains forbidden call {forbidden}"
