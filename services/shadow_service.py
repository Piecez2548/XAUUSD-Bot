"""Bounded, restartable shadow decision worker for read-only analysis."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import desc, select

from domain.events import AiDecisionEventPayload, DomainEvent, EventSeverity, EventType
from models.market import MarketSnapshot
from models.observatory import RiskSnapshot
from persistence.orm import SystemHealthRecord
from persistence.repositories import ShadowDecisionRepository, SystemHealthRepository
from services.shadow_engine import ShadowDecisionEngine
from services.shadow_replay import load_persisted_snapshots


@dataclass(frozen=True, slots=True)
class ShadowInput:
    snapshot: MarketSnapshot
    market_snapshot_id: UUID | None
    risk: RiskSnapshot | None
    candles_are_closed: bool = False


class ShadowDecisionWorker:
    """Process every eligible candle without blocking MT5 observation."""

    def __init__(self, settings, database, event_bus, *, logger: logging.Logger) -> None:
        self.settings = settings
        self.database = database
        self.logger = logger
        self.events = event_bus
        self.repository = ShadowDecisionRepository(database)
        self.health = SystemHealthRepository(database)
        self.engine = ShadowDecisionEngine(
            target_risk_percent=min(settings.max_trade_risk_percent, 2.0),
            max_aggregate_risk_percent=min(settings.max_aggregate_risk_percent, 6.0),
            min_rr=settings.shadow_min_rr,
            max_spread_points=settings.shadow_max_spread_points,
        )
        self._queue: asyncio.Queue[ShadowInput] = asyncio.Queue(maxsize=8)
        self._deferred_keys: set[tuple[str, datetime, str]] = set()
        self._queued_keys: set[tuple[str, datetime, str]] = set()
        self._processing_keys: set[tuple[str, datetime, str]] = set()
        self._completed_keys: set[tuple[str, datetime, str]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_received_candle: datetime | None = None
        self._last_processed_candle: datetime | None = None
        self._last_decision_candle: datetime | None = None
        self._latest_available_m5: datetime | None = None
        self._latest_received_m5: datetime | None = None
        self._latest_processed_m5: datetime | None = None
        self._latest_decision_m5: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_failed_at: datetime | None = None
        self._failure_count = 0
        self._error_category: str | None = None
        self._last_health_write: datetime | None = None
        self._needs_catchup = True
        self.stall_ttl_seconds = max(60.0, settings.live_account_interval_seconds * 4)
        self._catchup_pending_keys: set[tuple[str, datetime, str]] = set()
        self._restore_prior_health()

    @property
    def queue_depth(self) -> int:
        # Only the bounded in-memory queue belongs in x/8. Deferred and
        # durable catch-up work are reported separately.
        return self._queue.qsize()

    @property
    def deferred_count(self) -> int:
        return len(self._deferred_keys)

    @property
    def catchup_pending_count(self) -> int:
        return len(self._catchup_pending_keys)

    @property
    def total_backlog(self) -> int:
        return self.queue_depth + self.deferred_count + self.catchup_pending_count

    @property
    def state(self) -> str:
        if not self.settings.shadow_engine_enabled:
            return "DISABLED"
        if self._failure_count >= 3:
            return "ERROR"
        if self._last_failed_at is not None and self._failure_count:
            return "DEGRADED"
        if self.total_backlog:
            reference = self._last_success_at or self._last_received_candle
            if reference is not None and (
                datetime.now(UTC) - reference
            ).total_seconds() <= self.stall_ttl_seconds:
                return "CATCHING_UP"
            return "DEGRADED"
        if (
            self._last_received_candle is not None
            and self._last_processed_candle != self._last_received_candle
            and (
                datetime.now(UTC)
                - (self._last_success_at or self._last_received_candle)
            ).total_seconds()
            > self.stall_ttl_seconds
        ):
            return "DEGRADED"
        return "CONNECTED" if self._task is not None else "UNKNOWN"

    def start(self) -> None:
        if not self.settings.shadow_engine_enabled:
            self._record_health("DISABLED", "Shadow decision worker disabled")
            return
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="shadow-decision-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def submit(self, item: ShadowInput) -> None:
        if not self.settings.shadow_engine_enabled:
            return
        key = self._key(item)
        self._set_max("_last_received_candle", key[1])
        self._set_max("_latest_received_m5", key[1])
        if key in self._queued_keys or key in self._processing_keys or key in self._completed_keys:
            return
        try:
            self._queue.put_nowait(item)
            self._queued_keys.add(key)
        except asyncio.QueueFull:
            # The snapshot is already durable. Defer it and let catch-up read
            # the persisted snapshot instead of silently dropping work.
            self._deferred_keys.add(key)
            self._needs_catchup = True
            self._record_health(
                "DEGRADED",
                "Shadow queue is full; persisted snapshots will be caught up",
                error_category="QueueFull",
            )

    async def _run(self) -> None:
        self._record_health("CONNECTED", "Shadow decision worker started", force=True)
        try:
            await self._catch_up()
            while not self._stop.is_set():
                self._record_health(self.state, "Shadow worker heartbeat")
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except TimeoutError:
                    if self._needs_catchup:
                        await self._catch_up()
                    continue
                key = self._key(item)
                self._queued_keys.discard(key)
                await self._process(item)
                if self._needs_catchup:
                    await self._catch_up()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_failure(exc)
            self.logger.exception("Shadow decision worker terminated")
        finally:
            self._record_health("DEGRADED", "Shadow decision worker stopped")

    async def _process(self, item: ShadowInput) -> None:
        key = self._key(item)
        self._processing_keys.add(key)
        completed = False
        try:
            decision = self.engine.evaluate(
                item.snapshot,
                market_snapshot_id=item.market_snapshot_id,
                risk=item.risk,
                candles_are_closed=item.candles_are_closed,
            )
            record = self.repository.persist(decision)
            now = datetime.now(UTC)
            self._set_max("_last_processed_candle", decision.m5_candle_timestamp)
            self._set_max("_last_decision_candle", record.m5_candle_timestamp)
            self._set_max("_latest_processed_m5", decision.m5_candle_timestamp)
            self._set_max("_latest_decision_m5", record.m5_candle_timestamp)
            self._last_success_at = now
            self._failure_count = 0
            self._error_category = None
            self._record_health(
                "CONNECTED",
                "Shadow decision persisted",
                force=True,
                metadata_extra={"decision": record.decision},
            )
            completed = True
            notify = (
                self.settings.shadow_notify_no_trade
                if record.decision == "NO_TRADE"
                else self.settings.shadow_notify_signals
            )
            if notify:
                with contextlib.suppress(Exception):
                    await self.events.publish(
                        DomainEvent(
                            event_type=EventType.SHADOW_DECISION_CREATED,
                            source="shadow_engine",
                            severity=(
                                EventSeverity.INFO
                                if record.decision == "NO_TRADE"
                                else EventSeverity.WARNING
                            ),
                            payload=AiDecisionEventPayload(
                                decision_id=UUID(record.id),
                                symbol=record.symbol,
                                action=record.decision,
                                confidence=record.confidence,
                                validation_status="SHADOW_ONLY",
                                execution_status="DISABLED",
                            ),
                        )
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_failure(exc)
            self.logger.warning("Shadow decision worker failed (%s)", type(exc).__name__)
        finally:
            self._processing_keys.discard(key)
            self._deferred_keys.discard(key)
            if completed:
                self._completed_keys.add(key)

    async def _catch_up(self) -> None:
        if not self._needs_catchup:
            return
        if self._queue.full() and not self._deferred_keys and not self._catchup_pending_keys:
            self._needs_catchup = False
            return
        try:
            snapshots = await asyncio.to_thread(
                load_persisted_snapshots, self.database, limit=None
            )
        except Exception as exc:
            self._record_failure(exc)
            return
        ordered = sorted(
            snapshots,
            key=lambda item: ShadowDecisionEngine._m5_timestamp(
                item[0], candles_are_closed=True
            ),
        )
        if ordered:
            self._set_max(
                "_latest_available_m5",
                max(
                    ShadowDecisionEngine._m5_timestamp(item[0], candles_are_closed=True)
                    for item in ordered
                ),
            )
        existing = await asyncio.to_thread(self._existing_decision_keys)
        candidates: list[tuple[tuple[str, datetime, str], ShadowInput]] = []
        candidate_keys: set[tuple[str, datetime, str]] = set()
        for snapshot, snapshot_id, risk in ordered:
            key = (
                snapshot.symbol.name,
                ShadowDecisionEngine._m5_timestamp(snapshot, candles_are_closed=True),
                self.engine.strategy_version,
            )
            if (
                key in self._queued_keys
                or key in self._processing_keys
                or key in self._completed_keys
                or key in candidate_keys
            ):
                continue
            if key in existing:
                self._completed_keys.add(key)
                continue
            self._catchup_pending_keys.add(key)
            candidate_keys.add(key)
            candidates.append((key, ShadowInput(snapshot, snapshot_id, risk, True)))
        # Add a small historical batch so live items retain priority and the
        # scheduler cannot be monopolized by replay.
        for index, (key, item) in enumerate(candidates):
            if self._queue.full() or index >= 2:
                break
            self._set_max("_last_received_candle", key[1])
            self._set_max("_latest_received_m5", key[1])
            self._queued_keys.add(key)
            self._catchup_pending_keys.discard(key)
            self._queue.put_nowait(item)
        self._needs_catchup = bool(self._catchup_pending_keys or self._deferred_keys)

    def _key(self, item: ShadowInput) -> tuple[str, datetime, str]:
        return (
            item.snapshot.symbol.name,
            ShadowDecisionEngine._m5_timestamp(
                item.snapshot, candles_are_closed=item.candles_are_closed
            ),
            self.engine.strategy_version,
        )

    def _record_failure(self, exc: Exception) -> None:
        self._needs_catchup = True
        self._failure_count += 1
        self._last_failed_at = datetime.now(UTC)
        self._error_category = type(exc).__name__
        self._record_health(
            "DEGRADED",
            "Shadow decision worker cycle failed",
            force=True,
            error_category=self._error_category,
        )

    def _record_health(
        self,
        state: str,
        message: str,
        *,
        force: bool = False,
        error_category: str | None = None,
        metadata_extra: dict[str, object] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        if (
            not force
            and self._last_health_write
            and (now - self._last_health_write).total_seconds() < 5
        ):
            return
        self._last_health_write = now
        metadata: dict[str, object] = {
            "state": state,
            "heartbeat_at": now.isoformat(),
            "last_received_candle_at": self._iso(self._last_received_candle),
            "last_processed_candle_at": self._iso(self._last_processed_candle),
            "last_decision_at": self._iso(self._last_decision_candle),
            "latest_available_m5": self._iso(self._latest_available_m5),
            "latest_received_m5": self._iso(self._latest_received_m5),
            "latest_processed_m5": self._iso(self._latest_processed_m5),
            "latest_decision_m5": self._iso(self._latest_decision_m5),
            "last_success_at": self._iso(self._last_success_at),
            "last_failed_at": self._iso(self._last_failed_at),
            "failure_count": self._failure_count,
            "queue_depth": self.queue_depth,
            "queue_capacity": self._queue.maxsize,
            "deferred_count": self.deferred_count,
            "catchup_pending_count": self.catchup_pending_count,
            "total_backlog": self.total_backlog,
            "processing_lag_seconds": self._processing_lag_seconds(),
            "last_failure": self._error_category,
            "error_category": error_category or self._error_category,
            "execution_allowed": False,
        }
        if metadata_extra:
            metadata.update(metadata_extra)
        self.health.record("worker:shadow", state, message=message, metadata=metadata)

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    def _set_max(self, attribute: str, value: datetime | None) -> None:
        if value is None:
            return
        current = getattr(self, attribute)
        if current is None or value > current:
            setattr(self, attribute, value)

    def _processing_lag_seconds(self) -> float | None:
        if self._latest_available_m5 is None or self._latest_processed_m5 is None:
            return None
        return max(0.0, (self._latest_available_m5 - self._latest_processed_m5).total_seconds())

    def _existing_decision_keys(self) -> set[tuple[str, datetime, str]]:
        from persistence.orm import ShadowDecisionRecord

        with self.database.session() as session:
            rows = session.execute(
                select(
                    ShadowDecisionRecord.symbol,
                    ShadowDecisionRecord.m5_candle_timestamp,
                    ShadowDecisionRecord.strategy_version,
                )
            ).all()
        return {(symbol, timestamp, version) for symbol, timestamp, version in rows}

    def _restore_prior_health(self) -> None:
        try:
            with self.database.session() as session:
                row = session.scalar(
                    select(SystemHealthRecord)
                    .where(SystemHealthRecord.component == "worker:shadow")
                    .order_by(desc(SystemHealthRecord.timestamp), desc(SystemHealthRecord.id))
                    .limit(1)
                )
            metadata = row.metadata_json if row else {}
            for field, key in (
                ("_latest_available_m5", "latest_available_m5"),
                ("_latest_received_m5", "latest_received_m5"),
                ("_latest_processed_m5", "latest_processed_m5"),
                ("_latest_decision_m5", "latest_decision_m5"),
                ("_last_received_candle", "last_received_candle_at"),
                ("_last_processed_candle", "last_processed_candle_at"),
                ("_last_decision_candle", "last_decision_at"),
            ):
                raw = metadata.get(key)
                if raw:
                    setattr(self, field, datetime.fromisoformat(str(raw)))
        except Exception:
            self.logger.debug("Unable to restore prior shadow health", exc_info=True)
