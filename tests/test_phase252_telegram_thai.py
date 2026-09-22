from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from config.settings import Settings
from notifications.telegram_th import (
    execution_disabled,
    format_thai_datetime,
    thai_help,
    translate_health_state,
)
from persistence.database import Database
from persistence.orm import Base, ForwardValidationSessionRecord, SystemHealthRecord
from services.control import COMMAND_HELP, TelegramControlService


def _service(tmp_path: Path, *, forward: bool = True) -> tuple[TelegramControlService, Database]:
    database = Database(f"sqlite:///{(tmp_path / 'thai.db').as_posix()}")
    Base.metadata.create_all(database.engine)
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'thai.db').as_posix()}",
        forward_shadow_enabled=forward,
        telegram_bot_token="never-render-this-token",
        telegram_chat_id="never-render-this-chat",
    )
    return TelegramControlService(settings, tmp_path, database=database), database


def test_health_state_translation_is_consistent() -> None:
    expected = {
        "CONNECTED": "🟢 ปกติ",
        "DEGRADED": "🟡 ทำงานได้ แต่มีบางส่วนผิดปกติ",
        "UNKNOWN": "⚪ ยังไม่ทราบสถานะ",
        "ERROR": "🔴 เกิดข้อผิดพลาด",
        "DISABLED": "⚫ ปิดใช้งาน",
    }
    for state, text in expected.items():
        rendered = translate_health_state(state)
        assert text in rendered
        assert f"({state})" in rendered


def test_help_lists_every_registered_english_command_in_thai() -> None:
    rendered = thai_help()
    assert "📖 คำสั่ง XAUUSD BOT" in rendered
    assert "🔒 การเทรดเงินจริง: ปิดอยู่" in rendered
    for command in COMMAND_HELP:
        assert command in rendered


def test_health_uses_authoritative_forward_worker_and_hides_secrets(tmp_path: Path) -> None:
    service, database = _service(tmp_path)
    now = datetime.now(UTC)
    with database.session() as session:
        for component in (
            "worker:history",
            "worker:shadow",
            "worker:shadow_outcome",
            "worker:forward_shadow",
        ):
            session.add(SystemHealthRecord(
                component=component,
                timestamp=now,
                status="CONNECTED",
                message="ok",
                metadata_json={},
            ))
    rendered = service._health()
    assert "Forward Shadow" in rendered
    assert "🟢 ปกติ (CONNECTED)" in rendered
    assert "never-render-this-token" not in rendered
    assert "never-render-this-chat" not in rendered
    assert execution_disabled() in rendered


def test_forwardhealth_preserves_session_and_converts_display_time(tmp_path: Path) -> None:
    service, database = _service(tmp_path)
    started = datetime(2026, 9, 22, 15, 43, tzinfo=UTC)
    with database.session() as session:
        session.add(ForwardValidationSessionRecord(
            session_id="forward_thai_test",
            strategy_id="pair_zone_v1",
            strategy_version="1.0.0",
            strategy_config_hash="a" * 64,
            started_at=started,
            source_identity="test",
            symbol="XAUUSD",
            timeframes_json=["M5", "M15", "H1"],
            rr=2.0,
            cost_policy_json={},
            status="ACTIVE",
            execution_allowed=False,
        ))
        session.add(SystemHealthRecord(
            component="worker:forward_shadow",
            timestamp=started,
            status="CONNECTED",
            message="ok",
            metadata_json={},
        ))
    rendered = service._forwardhealth()
    assert "forward_thai_test" in rendered
    assert "22 ก.ย. 2026 22:43 น." in format_thai_datetime(started)
    assert started == datetime(2026, 9, 22, 15, 43, tzinfo=UTC)


def test_empty_forward_trades_are_operator_friendly(tmp_path: Path) -> None:
    service, _database = _service(tmp_path)
    rendered = service._forwardtrades()
    assert "ยังไม่มีสัญญาณ" in rendered
    assert "นี่ไม่ใช่ข้อผิดพลาด" in rendered
    assert execution_disabled() in rendered
