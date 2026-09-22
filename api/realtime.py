"""Database-backed realtime event fan-out for local and multi-process operation."""
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from fastapi import WebSocket
from sqlalchemy import or_, select

from persistence.database import Database
from persistence.orm import SystemEventRecord, SystemHealthRecord
from services.worker_health import derive_worker_state


class RealtimeHub:
    def __init__(
        self,
        database: Database,
        logger: logging.Logger | None = None,
        history_interval_seconds: float = 30.0,
    ) -> None:
        self._database = database
        self._logger = logger or logging.getLogger(__name__)
        self._history_interval_seconds = history_interval_seconds
        self._clients: set[WebSocket] = set()
        self._task: asyncio.Task[None] | None = None
        self._last_timestamp = datetime.min.replace(tzinfo=UTC)
        self._last_event_id = ""
        self._last_health_timestamp = datetime.min.replace(tzinfo=UTC)
        self._last_health_id = ""

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._poll(), name="event-realtime-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        await websocket.send_json(
            {
                "type": "connected",
                "timestamp": datetime.now(UTC).isoformat(),
                "read_only": True,
            }
        )

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def _fetch_new_events(self) -> list[dict[str, object]]:
        with self._database.session() as session:
            rows = session.scalars(
                select(SystemEventRecord)
                .where(
                    or_(
                        SystemEventRecord.timestamp > self._last_timestamp,
                        (
                            (SystemEventRecord.timestamp == self._last_timestamp)
                            & (SystemEventRecord.event_id > self._last_event_id)
                        ),
                    )
                )
                .order_by(SystemEventRecord.timestamp, SystemEventRecord.event_id)
                .limit(200)
            ).all()
            return [
                {
                    "event_id": row.event_id,
                    "event_type": row.event_type,
                    "timestamp": row.timestamp.isoformat(),
                    "source": row.source,
                    "severity": row.severity,
                    "correlation_id": row.correlation_id,
                    "payload": row.payload,
                    "schema_version": row.schema_version,
                }
                for row in rows
            ]

    def _fetch_new_health(self) -> list[dict[str, object]]:
        with self._database.session() as session:
            rows = session.scalars(
                select(SystemHealthRecord)
                .where(
                    or_(
                        SystemHealthRecord.timestamp > self._last_health_timestamp,
                        (
                            (SystemHealthRecord.timestamp == self._last_health_timestamp)
                            & (SystemHealthRecord.id > self._last_health_id)
                        ),
                    )
                )
                .order_by(SystemHealthRecord.timestamp, SystemHealthRecord.id)
                .limit(100)
            ).all()
            return [
                {
                    "id": row.id,
                    "component": row.component,
                    "status": (
                        derive_worker_state(
                            status=row.status,
                            timestamp=row.timestamp,
                            interval_seconds=self._history_interval_seconds,
                        )
                        if row.component
                        in {"worker:history", "worker:shadow", "worker:shadow_outcome"}
                        else row.status
                    ),
                    "timestamp": row.timestamp.isoformat(),
                    "latency_ms": row.latency_ms,
                    "metadata": row.metadata_json or {},
                }
                for row in rows
            ]

    async def _poll(self) -> None:
        while True:
            try:
                events = await asyncio.to_thread(self._fetch_new_events)
                for event in events:
                    self._last_timestamp = datetime.fromisoformat(str(event["timestamp"]))
                    self._last_event_id = str(event["event_id"])
                    await self._broadcast({"type": "domain_event", "event": event})
                health_updates = await asyncio.to_thread(self._fetch_new_health)
                for health in health_updates:
                    self._last_health_timestamp = datetime.fromisoformat(str(health["timestamp"]))
                    self._last_health_id = str(health["id"])
                    await self._broadcast({"type": "system_health", "health": health})
            except Exception:
                self._logger.exception("Realtime event polling failed")
            await asyncio.sleep(1)

    async def _broadcast(self, message: dict[str, object]) -> None:
        disconnected: list[WebSocket] = []
        for client in tuple(self._clients):
            try:
                await client.send_json(message)
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            self.disconnect(client)
