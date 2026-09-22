"""One-shot read-only observation, persistence, and event publication workflow."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from config.settings import Settings
from domain.events import (
    AccountSnapshotCreatedPayload,
    DomainEvent,
    ErrorPayload,
    EventSeverity,
    EventType,
    RiskSnapshotCreatedPayload,
    SnapshotCreatedPayload,
    SystemStatusPayload,
)
from events.bus import EventBus
from models.observatory import PersistenceResult
from persistence.repositories import SnapshotRepository
from services.collector import collect_market_snapshot
from services.risk import calculate_risk_snapshot


class ObservatoryService:
    def __init__(
        self,
        settings: Settings,
        snapshots: SnapshotRepository,
        event_bus: EventBus,
        *,
        logger: logging.Logger,
        mt5_module: Any | None = None,
    ) -> None:
        self._settings = settings
        self._snapshots = snapshots
        self._event_bus = event_bus
        self._logger = logger
        self._mt5_module = mt5_module

    async def observe_once(self) -> PersistenceResult:
        correlation_id = uuid4()
        await self._event_bus.publish(
            DomainEvent(
                event_type=EventType.SYSTEM_STARTED,
                source="observatory",
                correlation_id=correlation_id,
                payload=SystemStatusPayload(
                    message="Read-only market observation started",
                    component="observatory",
                    status="running",
                ),
            )
        )
        try:
            snapshot, _candidates = collect_market_snapshot(
                self._settings,
                logger=self._logger,
                mt5_module=self._mt5_module,
            )
        except Exception as exc:
            await self._event_bus.publish(
                DomainEvent(
                    event_type=EventType.MT5_DISCONNECTED,
                    source="mt5",
                    severity=EventSeverity.ERROR,
                    correlation_id=correlation_id,
                    payload=SystemStatusPayload(
                        message="MT5 read-only collection failed",
                        component="mt5",
                        status="disconnected",
                    ),
                )
            )
            await self._event_bus.publish(
                DomainEvent(
                    event_type=EventType.SYSTEM_ERROR,
                    source="observatory",
                    severity=EventSeverity.ERROR,
                    correlation_id=correlation_id,
                    payload=ErrorPayload(
                        error_code="OBSERVATION_FAILED",
                        message=str(exc),
                        recoverable=True,
                        component="observatory",
                    ),
                )
            )
            raise
        risk = calculate_risk_snapshot(
            snapshot,
            max_trade_risk_percent=self._settings.max_trade_risk_percent,
            max_aggregate_risk_percent=self._settings.max_aggregate_risk_percent,
        )
        result = self._snapshots.persist(snapshot, risk)
        events = (
            DomainEvent(
                event_type=EventType.MT5_CONNECTED,
                source="mt5",
                correlation_id=correlation_id,
                payload=SystemStatusPayload(
                    message="MT5 read-only collection completed",
                    component="mt5",
                    status="connected",
                ),
            ),
            DomainEvent(
                event_type=EventType.ACCOUNT_SNAPSHOT_CREATED,
                source="observatory",
                correlation_id=correlation_id,
                payload=AccountSnapshotCreatedPayload(
                    account_snapshot_id=result.account_snapshot_id,
                    currency=snapshot.account.currency,
                ),
            ),
            DomainEvent(
                event_type=EventType.MARKET_SNAPSHOT_CREATED,
                source="observatory",
                correlation_id=correlation_id,
                payload=SnapshotCreatedPayload(
                    snapshot_id=result.market_snapshot_id,
                    symbol=snapshot.symbol.name,
                    positions_count=len(snapshot.positions),
                ),
            ),
            DomainEvent(
                event_type=EventType.RISK_SNAPSHOT_CREATED,
                source="risk",
                correlation_id=correlation_id,
                severity=(
                    EventSeverity.WARNING
                    if risk.unbounded_positions_count > 0
                    else EventSeverity.INFO
                ),
                payload=RiskSnapshotCreatedPayload(
                    risk_snapshot_id=result.risk_snapshot_id,
                    open_risk_percent=risk.open_risk_percent,
                    remaining_risk_percent=risk.remaining_risk_percent,
                    unbounded_positions=risk.unbounded_positions_count,
                ),
            ),
            DomainEvent(
                event_type=EventType.SYSTEM_STOPPED,
                source="observatory",
                correlation_id=correlation_id,
                payload=SystemStatusPayload(
                    message="Read-only market observation completed",
                    component="observatory",
                    status="idle",
                ),
            ),
        )
        for event in events:
            await self._event_bus.publish(event)
        return result
