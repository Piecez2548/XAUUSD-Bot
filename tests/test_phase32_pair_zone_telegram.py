from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from domain.events import EventType
from events.bus import EventBus
from notifications.telegram import TelegramNotifier
from notifications.templates import format_telegram_event
from persistence.database import Database
from persistence.repositories import EventRepository
from services.pair_zone_notifications import PairZoneNotificationService


def _observation(zone_id: str = "pz-1", lifecycle: str = "ACTIVE") -> dict[str, object]:
    return {
        "state": "ACTIVE_ZONE",
        "direction": "BUY",
        "zone_id": zone_id,
        "zone_lower": 4149.846,
        "zone_upper": 4151.276,
        "zone_lifecycle_state": lifecycle,
    }


def _service(tmp_path, *, failing: bool = False):
    database = Database(f"sqlite:///{(tmp_path / 'pair-zone-telegram.db').as_posix()}")
    database.create_schema()
    events = []
    bus = EventBus()

    async def capture(event):
        events.append(event)
        if failing:
            raise RuntimeError("telegram unavailable")

    bus.subscribe("database", EventRepository(database).handle, critical=True)
    bus.subscribe("capture", capture)
    return (
        PairZoneNotificationService(database, bus, logger=logging.getLogger("test")),
        events,
        database,
    )


@pytest.mark.asyncio
async def test_pair_zone_transitions_are_deduplicated(tmp_path) -> None:
    service, events, database = _service(tmp_path)
    try:
        await service.observe_pair_zone(
            session_id="session-1", symbol="XAUUSD", previous_zone_id=None,
            observation=_observation(), timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            demo_execution_enabled=True, demo_kill_switch_armed=True,
        )
        await service.observe_pair_zone(
            session_id="session-1", symbol="XAUUSD", previous_zone_id=None,
            observation=_observation(), timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await service.observe_pair_zone(
            session_id="session-1", symbol="XAUUSD", previous_zone_id=None,
            observation=_observation(lifecycle="TOUCHED"),
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await service.observe_pair_zone(
            session_id="session-1", symbol="XAUUSD", previous_zone_id=None,
            observation=_observation(lifecycle="CONFIRMED"),
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert [event.event_type for event in events] == [
            EventType.PAIR_ZONE_ACTIVE,
            EventType.ZONE_TOUCHED,
        ]
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_invalidation_and_replacement_are_distinct(tmp_path) -> None:
    service, events, database = _service(tmp_path)
    try:
        evidence = {
            "zone_id": "pz-old",
            "direction": "SELL",
            "zone_lower": 4180.0,
            "zone_upper": 4184.0,
            "m5_candle_timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "m5_close": 4185.0,
            "reason": "M5 close above upper zone boundary",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        }
        await service.observe_pair_zone(
            session_id="session-1", symbol="XAUUSD", previous_zone_id="pz-old",
            observation=_observation("pz-new"), invalidations=(evidence,),
        )
        assert [event.event_type for event in events] == [
            EventType.ZONE_INVALIDATED,
            EventType.PAIR_ZONE_ACTIVE,
        ]

        events.clear()
        await service.observe_pair_zone(
            session_id="session-2", symbol="XAUUSD", previous_zone_id="pz-old",
            observation=_observation("pz-new"),
        )
        assert [event.event_type for event in events] == [
            EventType.ZONE_REPLACED,
            EventType.PAIR_ZONE_ACTIVE,
        ]
        assert "INVALIDATED" not in format_telegram_event(events[0])
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_signal_execution_and_outcome_are_each_idempotent(tmp_path) -> None:
    service, events, database = _service(tmp_path)
    try:
        signal = SimpleNamespace(
            id="signal-row-1", signal_id="signal-1", symbol="XAUUSD", decision="BUY",
            zone_id="pz-1", timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await service.canonical_signal(session_id="session-1", signal=signal)
        await service.canonical_signal(session_id="session-1", signal=signal)
        accepted = SimpleNamespace(
            id="demo-1", status="ACKNOWLEDGED", symbol="XAUUSD", direction="BUY",
            forward_signal_id="signal-row-1", submitted_entry=4150.0, planned_entry=4150.0,
            stop_loss=4140.0, take_profit=4170.0, volume=0.01, risk_percent=2.0,
            broker_position_ticket=123, broker_deal_ticket=None, rejection_reason=None,
            submitted_at=datetime(2026, 1, 1, tzinfo=UTC), updated_at=None,
        )
        await service.demo_execution_result(accepted)
        await service.demo_execution_result(accepted)
        blocked = SimpleNamespace(
            id="demo-2", status="REJECTED", symbol="XAUUSD", direction="BUY",
            forward_signal_id="signal-row-2", submitted_entry=None, planned_entry=4150.0,
            stop_loss=4140.0, take_profit=4170.0, volume=None, risk_percent=None,
            broker_position_ticket=None, broker_deal_ticket=None,
            rejection_reason="KILL_SWITCH_DISABLED", submitted_at=None, updated_at=None,
        )
        await service.demo_execution_result(blocked)
        outcome = {
            "identity": "demo-1:2026-01-01T00:10:00+00:00:TP",
            "symbol": "XAUUSD", "direction": "BUY", "ticket": 123,
            "entry": 4150.0, "exit": 4170.0, "result": "TP", "pnl": 20.0,
            "signal_id": "signal-row-1",
            "terminal_outcome_at": datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
        }
        await service.demo_position_closed(outcome)
        await service.demo_position_closed(outcome)
        assert [event.event_type for event in events] == [
            EventType.CANONICAL_SIGNAL_CREATED,
            EventType.DEMO_ORDER_ACCEPTED,
            EventType.DEMO_ORDER_BLOCKED,
            EventType.DEMO_POSITION_CLOSED,
        ]
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_notification_failure_isolated_from_processing(tmp_path) -> None:
    service, _events, database = _service(tmp_path, failing=True)
    try:
        await service.observe_pair_zone(
            session_id="session-1", symbol="XAUUSD", previous_zone_id=None,
            observation=_observation(),
        )
    finally:
        database.dispose()


def test_pair_zone_events_are_not_shadow_or_execution_authority() -> None:
    assert EventType.PAIR_ZONE_ACTIVE not in {
        EventType.TRADE_REQUESTED,
        EventType.TRADE_APPROVED,
        EventType.TRADE_REJECTED,
    }
    assert {
        EventType.PAIR_ZONE_ACTIVE,
        EventType.DEMO_ORDER_BLOCKED,
    } <= TelegramNotifier.NOTIFIABLE_EVENT_TYPES
