"""Minimal, durable Pair Zone lifecycle notifications.

Notifications are advisory operator telemetry.  They do not participate in
strategy decisions, risk gates, or broker execution.  Event identities are
derived from stable domain identifiers and persisted in the existing
``system_events`` ledger before delivery, so replaying a cycle or restarting
the worker cannot create a notification storm.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from domain.events import DomainEvent, EventSeverity, EventType, SystemStatusPayload
from persistence.orm import SystemEventRecord

PAIR_ZONE_NOTIFICATION_NAMESPACE = UUID("5b7f2e10-6b44-4b55-ae95-1a78f3f77d63")
PAIR_ZONE_NOTIFICATION_SOURCE = "pair_zone_observability"


class PairZoneNotificationService:
    """Publish one best-effort operator event for each stable transition."""

    def __init__(self, database: Any, event_bus: Any, *, logger: logging.Logger) -> None:
        self.database = database
        self.event_bus = event_bus
        self.logger = logger
        self._lock = asyncio.Lock()

    @staticmethod
    def event_id(event_type: EventType, identity: str) -> UUID:
        return uuid5(PAIR_ZONE_NOTIFICATION_NAMESPACE, f"{event_type.value}:{identity}")

    def _already_published(self, event_id: UUID) -> bool:
        with self.database.session() as session:
            return session.get(SystemEventRecord, str(event_id)) is not None

    async def _publish(
        self,
        event_type: EventType,
        identity: str,
        *,
        message: str,
        diagnostics: dict[str, Any],
        timestamp: datetime | None = None,
    ) -> bool:
        event_id = self.event_id(event_type, identity)
        async with self._lock:
            if self._already_published(event_id):
                return False
            event = DomainEvent(
                event_id=event_id,
                event_type=event_type,
                timestamp=timestamp or datetime.now(UTC),
                source=PAIR_ZONE_NOTIFICATION_SOURCE,
                severity=EventSeverity.INFO,
                payload=SystemStatusPayload(
                    message=message,
                    component=PAIR_ZONE_NOTIFICATION_SOURCE,
                    status=event_type.value,
                    diagnostics=diagnostics,
                ),
            )
            try:
                await self.event_bus.publish(event)
            except Exception:
                # Telegram delivery is optional and must never affect the
                # strategy, Forward Shadow, or execution path.
                self.logger.exception("Pair Zone notification failed: %s", event_type.value)
                return False
            return True

    async def observe_pair_zone(
        self,
        *,
        session_id: str,
        symbol: str,
        previous_zone_id: str | None,
        observation: dict[str, Any] | None,
        invalidations: tuple[dict[str, Any], ...] = (),
        timestamp: datetime | None = None,
        demo_execution_enabled: bool = False,
        demo_kill_switch_armed: bool = False,
    ) -> None:
        for invalidation in invalidations:
            zone_id = str(invalidation.get("zone_id") or "unknown")
            await self._publish(
                EventType.ZONE_INVALIDATED,
                f"{session_id}:{zone_id}",
                message="Canonical Pair Zone invalidated by the existing strategy rule",
                diagnostics={"symbol": symbol, **invalidation},
                timestamp=invalidation.get("timestamp") or timestamp,
            )

        if not isinstance(observation, dict) or observation.get("state") != "ACTIVE_ZONE":
            return
        zone_id = observation.get("zone_id")
        direction = observation.get("direction")
        if not isinstance(zone_id, str) or not zone_id or direction not in {"BUY", "SELL"}:
            return
        base = {
            "symbol": symbol,
            "direction": direction,
            "zone_id": zone_id,
            "zone_lower": observation.get("zone_lower"),
            "zone_upper": observation.get("zone_upper"),
            "time": timestamp,
        }
        if previous_zone_id and previous_zone_id != zone_id:
            invalidated_id = self.event_id(
                EventType.ZONE_INVALIDATED, f"{session_id}:{previous_zone_id}"
            )
            if not self._already_published(invalidated_id):
                await self._publish(
                    EventType.ZONE_REPLACED,
                    f"{session_id}:{previous_zone_id}:{zone_id}",
                    message="Current Pair Zone changed to a newer canonical zone",
                    diagnostics={
                        "symbol": symbol,
                        "previous_zone_id": previous_zone_id,
                        "current_zone_id": zone_id,
                        "previous_status": "NOT_CLAIMED_INVALIDATED",
                        "reason": "detector selected newer canonical zone",
                    },
                    timestamp=timestamp,
                )
        await self._publish(
            EventType.PAIR_ZONE_ACTIVE,
            f"{session_id}:{zone_id}",
            message="New canonical Pair Zone selected",
            diagnostics={
                **base,
                "status": "WAITING FOR TOUCH",
                "execution": (
                    "DEMO ARMED"
                    if demo_execution_enabled and demo_kill_switch_armed
                    else "DEMO OFF"
                ),
            },
            timestamp=timestamp,
        )
        if observation.get("zone_lifecycle_state") in {"TOUCHED", "CONFIRMED"}:
            await self._publish(
                EventType.ZONE_TOUCHED,
                f"{session_id}:{zone_id}",
                message="Pair Zone received its first meaningful touch",
                diagnostics={
                    **base,
                    "status": "WAITING FOR M5 REJECTION",
                    "m5_candle_timestamp": timestamp,
                },
                timestamp=timestamp,
            )

    async def canonical_signal(
        self,
        *,
        session_id: str,
        signal: Any,
        timestamp: datetime | None = None,
    ) -> None:
        await self._publish(
            EventType.CANONICAL_SIGNAL_CREATED,
            f"{session_id}:{signal.id}",
            message="Canonical Pair Zone signal is being sent to risk and Demo safety gates",
            diagnostics={
                "symbol": signal.symbol,
                "direction": signal.decision,
                "zone_id": signal.zone_id,
                "signal_id": signal.signal_id,
                "forward_signal_id": signal.id,
                "status": "SENDING TO RISK / DEMO SAFETY GATES",
            },
            timestamp=timestamp or signal.timestamp,
        )

    async def momentum_setup(
        self, *, session_id: str, signal: Any, decision: Any, timestamp: datetime
    ) -> None:
        context = getattr(decision, "feature_context", {}) or {}
        await self._publish(
            EventType.MOMENTUM_SETUP,
            f"{session_id}:{signal.setup_event_id}",
            message="Deterministic closed-candle Momentum Breakout setup confirmed",
            diagnostics={
                "symbol": signal.symbol,
                "direction": signal.decision,
                "setup_type": signal.setup_type,
                "setup_event_id": signal.setup_event_id,
                "breakout_level": context.get("breakout_level"),
                "m5_candle_timestamp": timestamp,
            },
            timestamp=timestamp,
        )

    async def momentum_signal(
        self, *, session_id: str, signal: Any, decision: Any, timestamp: datetime
    ) -> None:
        context = getattr(decision, "feature_context", {}) or {}
        await self._publish(
            EventType.MOMENTUM_SIGNAL,
            f"{session_id}:{signal.id}",
            message=(
                "Canonical Momentum Breakout signal is being sent to existing "
                "risk and Demo gates"
            ),
            diagnostics={
                "symbol": signal.symbol,
                "direction": signal.decision,
                "setup_type": signal.setup_type,
                "setup_event_id": signal.setup_event_id,
                "signal_id": signal.signal_id,
                "forward_signal_id": signal.id,
                "entry": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "planned_rr": signal.rr,
                "breakout_level": context.get("breakout_level"),
                "m5_candle_timestamp": timestamp,
            },
            timestamp=timestamp,
        )

    async def demo_execution_result(self, record: Any) -> None:
        if record.status not in {"ACKNOWLEDGED", "REJECTED"}:
            return
        event_type = (
            EventType.DEMO_ORDER_ACCEPTED
            if record.status == "ACKNOWLEDGED"
            else EventType.DEMO_ORDER_BLOCKED
        )
        diagnostics = {
            "symbol": record.symbol,
            "direction": record.direction,
            "signal_id": record.forward_signal_id,
            "status": record.status,
            "entry": record.submitted_entry or record.planned_entry,
            "stop_loss": record.stop_loss,
            "take_profit": record.take_profit,
            "lot": record.volume,
            "risk_percent": record.risk_percent,
            "ticket": record.broker_position_ticket or record.broker_deal_ticket,
            "reason": record.rejection_reason,
            "real_money": "DISABLED",
        }
        if record.rejection_reason == "ENTRY_DEVIATION_EXCEEDED":
            point = getattr(record, "symbol_point", None)
            max_points = getattr(record, "max_deviation_points", None)
            diagnostics["preflight"] = {
                "planned_entry": getattr(record, "planned_entry", None),
                "executable_price": getattr(record, "executable_price", None),
                "deviation_price": getattr(record, "deviation_price", None),
                "deviation_points": getattr(record, "deviation_points", None),
                "max_deviation_points": max_points,
                "max_deviation_price": (
                    float(max_points) * float(point)
                    if max_points is not None and point is not None
                    else None
                ),
                "broker_bid": getattr(record, "broker_bid", None),
                "broker_ask": getattr(record, "broker_ask", None),
                "symbol_point": point,
                "symbol_digits": getattr(record, "symbol_digits", None),
            }
        await self._publish(
            event_type,
            f"{record.id}:{record.status}",
            message=(
                "Existing DemoExecutionService accepted the Demo order"
                if event_type is EventType.DEMO_ORDER_ACCEPTED
                else "Existing DemoExecutionService blocked the Demo order"
            ),
            diagnostics=diagnostics,
            timestamp=getattr(record, "submitted_at", None) or getattr(record, "updated_at", None),
        )

    async def demo_position_closed(self, outcome: dict[str, Any]) -> None:
        await self._publish(
            EventType.DEMO_POSITION_CLOSED,
            str(outcome["identity"]),
            message="Broker reconciliation proved a terminal Demo position outcome",
            diagnostics={key: value for key, value in outcome.items() if key != "identity"},
            timestamp=outcome.get("terminal_outcome_at"),
        )
