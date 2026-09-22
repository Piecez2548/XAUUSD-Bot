"""Continuous, serialized, read-only MT5 Live Data Engine."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from config.settings import Settings
from domain.events import (
    AccountSnapshotCreatedPayload,
    CandleClosedPayload,
    DomainEvent,
    EventSeverity,
    EventType,
    RiskSnapshotCreatedPayload,
    SnapshotCreatedPayload,
    SystemStatusPayload,
    TradeLifecyclePayload,
)
from events.bus import EventBus
from models.live import Freshness, FreshnessState, RuntimeState, WorkerHealth
from models.market import (
    AccountState,
    Candle,
    MarketSnapshot,
    Position,
    SymbolSpecification,
    Tick,
    Timeframe,
)
from mt5.account import read_account_state
from mt5.gateway import MT5Gateway
from mt5.market_data import read_all_candles, read_completed_candles
from mt5.positions import read_open_positions
from mt5.symbols import discover_symbol, read_symbol_specification, read_tick
from persistence.repositories import HistoryRepository, SnapshotRepository, SystemHealthRepository
from services.risk import (
    calculate_risk_snapshot,
    normalize_risk_state,
    risk_state_changed,
)
from services.shadow_service import ShadowDecisionWorker, ShadowInput
from services.worker_health import worker_health_ttl


@dataclass
class _LiveState:
    symbol: str | None = None
    specification: SymbolSpecification | None = None
    tick: Tick | None = None
    account: AccountState | None = None
    positions: tuple[Position, ...] = ()
    candles: dict[Timeframe, tuple[Candle, ...]] = field(default_factory=dict)
    last_tick_observed: datetime | None = None
    last_account_observed: datetime | None = None
    last_position_observed: datetime | None = None
    last_history_sync: datetime | None = None
    last_deals_synchronized: int = 0
    risk_unbounded: bool = False


class LiveDataEngine:
    """Own the live runtime workers and one MT5 access boundary."""

    def __init__(
        self,
        settings: Settings,
        database,
        event_bus: EventBus,
        *,
        logger: logging.Logger,
        mt5_module: Any | None = None,
        gateway: MT5Gateway | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.events = event_bus
        self.logger = logger
        self.gateway = gateway or MT5Gateway(settings, module=mt5_module, logger=logger)
        self.snapshots = SnapshotRepository(database)
        self.history = HistoryRepository(database)
        self.health = SystemHealthRepository(database)
        self.shadow = ShadowDecisionWorker(settings, database, event_bus, logger=logger)
        self.state = _LiveState()
        self.runtime_state = RuntimeState.STARTING
        self.started_at = datetime.now(UTC)
        self._stop = asyncio.Event()
        self._reconnect_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self._workers: dict[str, WorkerHealth] = {}
        self._last_event_by_key: dict[str, datetime] = {}
        self._stale_components: set[str] = set()
        self._last_risk_state = None
        self._history_health_state = "HEALTHY"

    async def run(self) -> None:
        self.started_at = datetime.now(UTC)
        self._set_runtime_state(RuntimeState.STARTING, "Live Data Engine starting")
        try:
            try:
                await self._synchronize(initial=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    "Initial MT5 synchronization failed; entering reconnect loop (%s)",
                    type(exc).__name__,
                )
                self._set_runtime_state(
                    RuntimeState.DISCONNECTED, "Initial MT5 synchronization failed"
                )
                with contextlib.suppress(Exception):
                    await self.gateway.shutdown()
                await self._reconnect()
            self._set_runtime_state(RuntimeState.CONNECTED, "Live Data Engine synchronized")
            self._tasks = [
                asyncio.create_task(self._worker("tick", self._fast_loop), name="live-tick"),
                asyncio.create_task(
                    self._worker("account", self._account_loop), name="live-account"
                ),
                asyncio.create_task(
                    self._worker("candles", self._candle_loop), name="live-candles"
                ),
                asyncio.create_task(
                    self._worker("history", self._history_loop), name="live-history"
                ),
                asyncio.create_task(self._watchdog_loop(), name="live-watchdog"),
            ]
            self.shadow.start()
            await self._publish(EventType.SYSTEM_LIVE_STARTED, "Live Data Engine started")
            await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.exception("Live Data Engine failed during startup")
            self._set_runtime_state(RuntimeState.ERROR, "Live Data Engine startup failed")
            await self._publish(
                EventType.SYSTEM_ERROR,
                "Live Data Engine startup failed",
                severity=EventSeverity.ERROR,
                payload=SystemStatusPayload(
                    message="Live Data Engine startup failed",
                    component="live_runtime",
                    status=type(exc).__name__,
                ),
            )
            raise
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._stop.is_set() and not self._tasks and self.runtime_state == RuntimeState.STOPPED:
            return
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.shadow.stop()
        await self.gateway.shutdown()
        self._set_runtime_state(RuntimeState.STOPPED, "Live Data Engine stopped")
        with contextlib.suppress(Exception):
            await self._publish(EventType.SYSTEM_LIVE_STOPPED, "Live Data Engine stopped")

    async def _synchronize(self, *, initial: bool = False) -> None:
        await self.gateway.connect()
        symbol, _candidates = await self.gateway.call(
            lambda value: discover_symbol(value, self.settings.trading_symbol)
        )
        specification, tick, account, positions, candles = await self.gateway.call(
            lambda value: (
                read_symbol_specification(value, symbol),
                read_tick(value, symbol),
                read_account_state(value),
                read_open_positions(value, symbol),
                read_all_candles(
                    value,
                    symbol,
                    self.settings.candle_counts,
                    minimum_ratio=self.settings.minimum_candle_ratio,
                    logger=self.logger,
                ),
            )
        )
        closed_candles = {
            timeframe: series[:-1] if len(series) > 1 else series
            for timeframe, series in candles.items()
        }
        snapshot = MarketSnapshot(
            account=account,
            symbol=specification,
            tick=tick,
            positions=positions,
            candles=closed_candles,
            generated_at=datetime.now(UTC),
        )
        risk = calculate_risk_snapshot(
            snapshot,
            max_trade_risk_percent=self.settings.max_trade_risk_percent,
            max_aggregate_risk_percent=self.settings.max_aggregate_risk_percent,
        )
        result = self.snapshots.persist(snapshot, risk)
        self.health.record(
            "mt5",
            "CONNECTED",
            message="Verified MT5 synchronization completed",
            metadata={"symbol": symbol, "open_position_count": len(positions)},
        )
        self.state.symbol = symbol
        self.state.specification = specification
        self.state.tick = tick
        self.state.account = account
        self.state.positions = positions
        # The shadow path receives closed-only windows.  The initial MT5
        # synchronization includes the forming bar for other observatory
        # consumers, so keep it out of the live shadow state explicitly.
        self.state.candles = closed_candles
        now = datetime.now(UTC)
        self.state.last_tick_observed = now
        self.state.last_account_observed = now
        self.state.last_position_observed = now
        self.state.risk_unbounded = risk.unbounded_positions_count > 0
        self._last_risk_state = normalize_risk_state(risk)
        for timeframe, series in candles.items():
            completed = series[:-1] if len(series) > 1 else series
            if completed:
                self.history.set_candle_cursor(symbol, timeframe.value, completed[-1].timestamp)
        await self._publish(
            EventType.MT5_RECONNECTED if not initial else EventType.MT5_CONNECTED,
            "MT5 read-only synchronization completed",
            source="mt5",
            payload=SystemStatusPayload(
                message="MT5 read-only synchronization completed",
                component="mt5",
                status="connected",
            ),
        )
        await self._publish(
            EventType.ACCOUNT_SNAPSHOT_CREATED,
            "Initial account snapshot persisted",
            payload=AccountSnapshotCreatedPayload(
                account_snapshot_id=result.account_snapshot_id,
                currency=account.currency,
            ),
        )
        await self._publish(
            EventType.MARKET_SNAPSHOT_CREATED,
            "Initial market snapshot persisted",
            payload=SnapshotCreatedPayload(
                snapshot_id=result.market_snapshot_id,
                symbol=symbol,
                positions_count=len(positions),
            ),
        )
        await self._publish(
            EventType.RISK_SNAPSHOT_CREATED,
            "Initial risk snapshot persisted",
            source="risk",
            severity=EventSeverity.WARNING
            if risk.unbounded_positions_count
            else EventSeverity.INFO,
            payload=RiskSnapshotCreatedPayload(
                risk_snapshot_id=result.risk_snapshot_id,
                open_risk_percent=risk.open_risk_percent,
                remaining_risk_percent=risk.remaining_risk_percent,
                unbounded_positions=risk.unbounded_positions_count,
            ),
        )

    async def _fast_loop(self) -> None:
        if self.state.symbol is None:
            return
        tick, positions = await self.gateway.call(
            lambda api: (
                read_tick(api, self.state.symbol),
                read_open_positions(api, self.state.symbol),
            )
        )
        previous = {item.ticket: item for item in self.state.positions}
        current = {item.ticket: item for item in positions}
        self.state.tick = tick
        self.state.positions = positions
        now = datetime.now(UTC)
        self.state.last_tick_observed = now
        self.state.last_position_observed = now
        for ticket, position in current.items():
            if ticket not in previous:
                await self._publish(
                    EventType.POSITION_OBSERVED_OPEN,
                    f"Position {ticket} observed open",
                    payload=TradeLifecyclePayload(
                        symbol=position.symbol,
                        broker_ticket=ticket,
                        direction=position.type_name
                        if position.type_name in {"BUY", "SELL"}
                        else None,
                        price=position.open_price,
                        volume=position.volume,
                        stop_loss=position.stop_loss,
                        take_profit=position.take_profit,
                    ),
                )
            elif self._position_changed(previous[ticket], position):
                await self._publish(
                    EventType.POSITION_OBSERVED_CHANGED,
                    f"Position {ticket} observed changed",
                    payload=TradeLifecyclePayload(
                        symbol=position.symbol,
                        broker_ticket=ticket,
                        direction=position.type_name
                        if position.type_name in {"BUY", "SELL"}
                        else None,
                        price=position.current_price,
                        volume=position.volume,
                        stop_loss=position.stop_loss,
                        take_profit=position.take_profit,
                    ),
                )
        for ticket, _position in previous.items():
            if ticket not in current:
                await self._publish(
                    EventType.POSITION_NO_LONGER_OPEN,
                    f"Position {ticket} is no longer open; history reconciliation required",
                    payload=SystemStatusPayload(
                        message="Position is no longer open; exit reason is not inferred",
                        component="position_monitor",
                        status="reconcile_required",
                    ),
                )
        self._record_worker_success("tick")

    async def _account_loop(self) -> None:
        if self.state.symbol is None or self.state.specification is None or self.state.tick is None:
            return
        account, positions, specification = await self.gateway.call(
            lambda api: (
                read_account_state(api),
                read_open_positions(api, self.state.symbol),
                read_symbol_specification(api, self.state.symbol),
            )
        )
        now = datetime.now(UTC)
        snapshot = MarketSnapshot(
            account=account,
            symbol=specification,
            tick=self.state.tick,
            positions=positions,
            candles=self.state.candles,
            generated_at=now,
        )
        risk = calculate_risk_snapshot(
            snapshot,
            max_trade_risk_percent=self.settings.max_trade_risk_percent,
            max_aggregate_risk_percent=self.settings.max_aggregate_risk_percent,
        )
        # Persist account, current open positions, and derived risk in one
        # transaction.  This closes the stale-position gap that previously
        # updated risk without updating the current-position tables.
        result = self.snapshots.persist(snapshot, risk)
        risk_id = str(result.risk_snapshot_id)
        self.health.record(
            "mt5",
            "CONNECTED",
            message="Verified MT5 account and position cycle completed",
            metadata={"symbol": self.state.symbol, "open_position_count": len(positions)},
        )
        changed = self.state.account is None or self._account_changed(self.state.account, account)
        was_unbounded = self.state.risk_unbounded
        self.state.account = account
        self.state.specification = specification
        self.state.positions = positions
        self.state.last_account_observed = now
        self.state.last_position_observed = now
        self.state.risk_unbounded = risk.unbounded_positions_count > 0
        if changed:
            await self._publish(
                EventType.ACCOUNT_UPDATED,
                "Account state changed",
                payload=SystemStatusPayload(
                    message="Verified account state changed",
                    component="account_monitor",
                    status="updated",
                ),
            )
        risk_changed = risk_state_changed(self._last_risk_state, risk)
        self._last_risk_state = normalize_risk_state(risk)
        if risk_changed:
            await self._publish(
                EventType.RISK_SNAPSHOT_CREATED,
                "Meaningful risk state changed",
                source="risk",
                severity=EventSeverity.WARNING
                if risk.unbounded_positions_count
                else EventSeverity.INFO,
                payload=RiskSnapshotCreatedPayload(
                    risk_snapshot_id=UUID(risk_id),
                    open_risk_percent=risk.open_risk_percent,
                    remaining_risk_percent=risk.remaining_risk_percent,
                    unbounded_positions=risk.unbounded_positions_count,
                ),
            )
        if risk.unbounded_positions_count and not was_unbounded:
            await self._publish(
                EventType.POSITION_WITHOUT_STOP_LOSS,
                "At least one open position has no stop loss",
                source="risk",
                severity=EventSeverity.WARNING,
                payload=SystemStatusPayload(
                    message="Position without stop loss; risk cannot be bounded",
                    component="risk_monitor",
                    status="unbounded",
                ),
            )
            await self._publish(
                EventType.RISK_UNBOUNDED,
                "Open position risk is unbounded because a stop is missing",
                source="risk",
                severity=EventSeverity.WARNING,
                payload=SystemStatusPayload(
                    message="Position without stop loss; aggregate risk is unknown",
                    component="risk_monitor",
                    status="unbounded",
                ),
            )
        elif not risk.unbounded_positions_count and was_unbounded:
            await self._publish(
                EventType.RISK_BACK_WITHIN_BOUNDS,
                "All observed positions have bounded risk",
                source="risk",
                payload=SystemStatusPayload(
                    message="All observed positions have bounded risk",
                    component="risk_monitor",
                    status="bounded",
                ),
            )
        self._record_worker_success("account")
        self.shadow.submit(
            ShadowInput(snapshot, result.market_snapshot_id, risk, candles_are_closed=True)
        )

    async def _candle_loop(self) -> None:
        if self.state.symbol is None or self.state.specification is None:
            return
        updated_candles = dict(self.state.candles)
        for timeframe in Timeframe:
            candles = await self.gateway.call(
                lambda api, tf=timeframe: read_completed_candles(
                    api,
                    self.state.symbol,
                    tf,
                    self.settings.live_candle_lookback,
                )
            )
            # `read_completed_candles` starts at MT5 position 1 and therefore
            # already excludes the forming candle.  Refresh the source used by
            # account snapshots and shadow decisions on every candle cycle.
            updated_candles[timeframe] = candles
            cursor = self.history.candle_cursor(self.state.symbol, timeframe.value)
            for candle in candles:
                if cursor is not None and candle.timestamp <= cursor:
                    continue
                self.snapshots.persist_candle(self.state.specification, timeframe, candle)
                self.history.set_candle_cursor(self.state.symbol, timeframe.value, candle.timestamp)
                await self._publish(
                    EventType.CANDLE_CLOSED,
                    f"{self.state.symbol} {timeframe.value} candle closed",
                    source="candle_engine",
                    payload=CandleClosedPayload(
                        symbol=self.state.symbol,
                        timeframe=timeframe.value,
                        open_time=candle.timestamp,
                        close_time=candle.timestamp
                        + timedelta(minutes=_timeframe_minutes(timeframe)),
                        open=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        tick_volume=candle.tick_volume,
                        spread=candle.spread,
                        real_volume=candle.real_volume,
                    ),
                )
                cursor = candle.timestamp
        self.state.candles = updated_candles
        self._record_worker_success("candles")

    async def _history_loop(self) -> None:
        if self.state.symbol is None:
            return
        try:
            scope = f"deals:{self.state.symbol}"
            cursor = self.history.cursor(scope)
            start = cursor[0] if cursor and cursor[0] else None
            from mt5.history import history_window, read_deals

            start, end = history_window(start)
            facts = await self.gateway.call(
                lambda api: read_deals(api, self.state.symbol, start, end)
            )
            inserted = self.history.persist_deals(facts, scope=scope)
            self.state.last_history_sync = datetime.now(UTC)
            self.state.last_deals_synchronized += inserted
            # The persisted worker row is authoritative; events are not a
            # substitute for a bounded heartbeat.
            self._record_worker_success("history")
            await self._publish(
                EventType.HISTORY_SYNC_COMPLETED,
                f"History reconciliation completed; {inserted} new deal(s)",
                source="history_reconciler",
                payload=SystemStatusPayload(
                    message="Read-only deal history reconciliation completed",
                    component="history_reconciler",
                    status="healthy",
                ),
            )
            if self._history_health_state != "HEALTHY":
                self._history_health_state = "HEALTHY"
                await self._publish(
                    EventType.HISTORY_SYNC_RECOVERED,
                    "History reconciliation recovered",
                    source="history_reconciler",
                    payload=SystemStatusPayload(
                        message="History reconciliation recovered",
                        component="history_reconciler",
                        status="healthy",
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._history_health_state == "HEALTHY":
                self._history_health_state = "DEGRADED"
                await self._publish(
                    EventType.HISTORY_SYNC_FAILED,
                    "History reconciliation failed",
                    source="history_reconciler",
                    severity=EventSeverity.WARNING,
                    payload=SystemStatusPayload(
                        message="History reconciliation failed",
                        component="history_reconciler",
                        status=type(exc).__name__,
                    ),
                )
            raise

    async def _worker(self, name: str, operation) -> None:
        interval = {
            "tick": self.settings.live_tick_interval_seconds,
            "account": self.settings.live_account_interval_seconds,
            "candles": self.settings.live_candle_interval_seconds,
            "history": self.settings.live_history_interval_seconds,
        }[name]
        while not self._stop.is_set():
            started = perf_counter()
            self._record_worker_attempt(name)
            try:
                if not self.gateway.connected:
                    await self._reconnect()
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_worker_failure(name, exc)
                await self._handle_connection_failure(exc)
            elapsed = perf_counter() - started
            if not self._stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=max(0.01, interval - elapsed))

    async def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(UTC)
            self._record_runtime_heartbeat()
            for name, worker in tuple(self._workers.items()):
                interval = {
                    "tick": self.settings.live_tick_interval_seconds,
                    "account": self.settings.live_account_interval_seconds,
                    "candles": self.settings.live_candle_interval_seconds,
                    "history": self.settings.live_history_interval_seconds,
                }.get(name, 30)
                if (
                    worker.last_attempted_at
                    and (now - worker.last_attempted_at).total_seconds()
                    > worker_health_ttl(interval)
                ):
                    self.health.record(
                        f"worker:{name}",
                        "DEGRADED",
                        message="Worker heartbeat is stale",
                        metadata={
                            "failure_count": worker.failure_count,
                            "last_success_at": (
                                worker.last_success_at.isoformat()
                                if worker.last_success_at
                                else None
                            ),
                            "last_failed_at": now.isoformat(),
                            "heartbeat_at": now.isoformat(),
                        },
                    )
            for component, observed, threshold in (
                ("tick", self.state.last_tick_observed, self.settings.data_stale_tick_seconds),
                (
                    "account",
                    self.state.last_account_observed,
                    self.settings.data_stale_account_seconds,
                ),
                (
                    "positions",
                    self.state.last_position_observed,
                    self.settings.data_stale_position_seconds,
                ),
            ):
                stale = observed is not None and (now - observed).total_seconds() > threshold
                if stale and component not in self._stale_components:
                    self._stale_components.add(component)
                    self.health.record(
                        f"data:{component}",
                        "STALE",
                        message=f"{component} data exceeded freshness threshold",
                    )
                    await self._publish(
                        EventType.DATA_STALE,
                        f"{component} data is stale",
                        source="freshness_watchdog",
                        severity=EventSeverity.WARNING,
                        payload=SystemStatusPayload(
                            message=f"{component} data exceeded freshness threshold",
                            component=component,
                            status="STALE",
                        ),
                    )
                elif not stale:
                    self._stale_components.discard(component)
            await asyncio.sleep(max(1.0, min(5.0, self.settings.live_tick_interval_seconds)))

    async def _reconnect(self) -> None:
        async with self._reconnect_lock:
            if self.gateway.connected:
                return
            self._set_runtime_state(RuntimeState.RECONNECTING, "MT5 reconnecting")
            self.health.record("mt5", "RECONNECTING", message="MT5 reconnect attempt started")
            await self._publish(
                EventType.MT5_RECONNECTING,
                "MT5 reconnect attempt started",
                source="mt5",
                severity=EventSeverity.WARNING,
                payload=SystemStatusPayload(
                    message="MT5 reconnect attempt started",
                    component="mt5",
                    status="reconnecting",
                ),
            )
            delay = self.settings.mt5_reconnect_initial_seconds
            while not self._stop.is_set():
                try:
                    await self._synchronize(initial=False)
                    self._set_runtime_state(RuntimeState.CONNECTED, "MT5 reconnected")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.health.record("mt5", "DEGRADED", message="MT5 reconnect attempt failed")
                    await asyncio.sleep(delay)
                    delay = min(self.settings.mt5_reconnect_max_seconds, delay * 2)

    async def _handle_connection_failure(self, _exc: Exception) -> None:
        self._set_runtime_state(RuntimeState.DISCONNECTED, "MT5 read-only operation failed")
        self.health.record("mt5", "DISCONNECTED", message="MT5 read-only operation failed")
        with contextlib.suppress(Exception):
            await self.gateway.shutdown()
        if not self._stop.is_set():
            await self._reconnect()

    async def _publish(
        self,
        event_type: EventType,
        message: str,
        *,
        source: str = "live_runtime",
        severity: EventSeverity = EventSeverity.INFO,
        payload=None,
    ) -> None:
        await self.events.publish(
            DomainEvent(
                event_type=event_type,
                source=source,
                severity=severity,
                correlation_id=uuid4(),
                payload=payload
                or SystemStatusPayload(
                    message=message,
                    component=source,
                    status=event_type.value,
                ),
            )
        )

    def _set_runtime_state(self, state: RuntimeState, message: str) -> None:
        if state == self.runtime_state and state not in {RuntimeState.DEGRADED, RuntimeState.ERROR}:
            return
        self.runtime_state = state
        self.health.record(
            "live_runtime",
            state.value,
            message=message,
            metadata={"symbol": self.state.symbol} if self.state.symbol else None,
        )

    def _record_runtime_heartbeat(self) -> None:
        """Persist a bounded heartbeat so process existence is not health."""

        self.health.record(
            "live_runtime",
            self.runtime_state.value,
            message="Live Data Engine heartbeat",
            metadata={
                "symbol": self.state.symbol,
                "pid": os.getpid(),
                "last_account_observed": (
                    self.state.last_account_observed.isoformat()
                    if self.state.last_account_observed
                    else None
                ),
                "last_position_observed": (
                    self.state.last_position_observed.isoformat()
                    if self.state.last_position_observed
                    else None
                ),
            },
        )

    def _record_worker_attempt(self, name: str) -> None:
        now = datetime.now(UTC)
        old = self._workers.get(name)
        self._workers[name] = WorkerHealth(
            name=name,
            state=old.state if old else RuntimeState.STARTING,
            last_attempted_at=now,
            last_success_at=old.last_success_at if old else None,
            latency_ms=old.latency_ms if old else None,
            failure_count=old.failure_count if old else 0,
            last_error_category=old.last_error_category if old else None,
        )

    def _record_worker_success(self, name: str) -> None:
        now = datetime.now(UTC)
        old = self._workers.get(name)
        worker = WorkerHealth(
            name=name,
            state=RuntimeState.CONNECTED,
            last_attempted_at=old.last_attempted_at if old else now,
            last_success_at=now,
            latency_ms=(
                now - (old.last_attempted_at if old and old.last_attempted_at else now)
            ).total_seconds()
            * 1_000,
            failure_count=0,
            last_error_category=None,
        )
        self._workers[name] = worker
        self.health.record(
            f"worker:{name}",
            "CONNECTED",
            latency_ms=worker.latency_ms,
            message="Worker cycle succeeded",
            metadata={
                "last_success_at": now.isoformat(),
                "heartbeat_at": now.isoformat(),
                "failure_count": 0,
            },
        )

    def _record_worker_failure(self, name: str, exc: Exception) -> None:
        now = datetime.now(UTC)
        old = self._workers.get(name)
        count = (old.failure_count if old else 0) + 1
        worker = WorkerHealth(
            name=name,
            state=RuntimeState.DEGRADED,
            last_attempted_at=now,
            last_success_at=old.last_success_at if old else None,
            latency_ms=old.latency_ms if old else None,
            failure_count=count,
            last_error_category=type(exc).__name__,
        )
        self._workers[name] = worker
        self.health.record(
            f"worker:{name}",
            "DEGRADED",
            message="Worker cycle failed",
            metadata={
                "failure_count": count,
                "error_category": type(exc).__name__,
                "last_failed_at": now.isoformat(),
                "heartbeat_at": now.isoformat(),
                "last_success_at": (
                    old.last_success_at.isoformat() if old and old.last_success_at else None
                ),
            },
        )

    def status(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        freshness = {
            "tick": _freshness(
                self.state.last_tick_observed,
                self.state.tick.timestamp if self.state.tick else None,
                self.settings.data_stale_tick_seconds,
                now,
                market_closed=_market_closed(self.state.specification),
            ),
            "account": _freshness(
                self.state.last_account_observed,
                None,
                self.settings.data_stale_account_seconds,
                now,
            ),
            "positions": _freshness(
                self.state.last_position_observed,
                None,
                self.settings.data_stale_position_seconds,
                now,
            ),
            "history": _freshness(
                self.state.last_history_sync,
                None,
                worker_health_ttl(self.settings.live_history_interval_seconds),
                now,
            ),
        }
        return {
            "state": self.runtime_state.value,
            "symbol": self.state.symbol,
            "started_at": self.started_at,
            "updated_at": now,
            "freshness": {key: value.model_dump(mode="json") for key, value in freshness.items()},
            "workers": [worker.model_dump(mode="json") for worker in self._workers.values()],
            "history_cursor": self.state.last_history_sync,
            "deals_synchronized": self.state.last_deals_synchronized,
        }

    @staticmethod
    def _position_changed(left: Position, right: Position) -> bool:
        return (
            left.volume != right.volume
            or left.current_price != right.current_price
            or left.stop_loss != right.stop_loss
            or left.take_profit != right.take_profit
            or left.profit != right.profit
        )

    @staticmethod
    def _account_changed(left: AccountState, right: AccountState) -> bool:
        return any(
            abs(getattr(left, key) - getattr(right, key)) > 1e-9
            for key in ("balance", "equity", "margin", "free_margin", "margin_level", "profit")
        )


def _freshness(
    observed_at,
    source_timestamp,
    threshold: float,
    now: datetime,
    *,
    market_closed: bool = False,
) -> Freshness:
    if observed_at is None:
        return Freshness(source_timestamp=source_timestamp, state=FreshnessState.UNKNOWN)
    age = max(0.0, (now - observed_at).total_seconds())
    return Freshness(
        observed_at=observed_at,
        source_timestamp=source_timestamp,
        age_seconds=age,
        state=(
            FreshnessState.LIVE
            if age <= threshold
            else FreshnessState.MARKET_CLOSED
            if market_closed
            else FreshnessState.STALE
        ),
    )


def _timeframe_minutes(timeframe: Timeframe) -> int:
    return {Timeframe.M5: 5, Timeframe.M15: 15, Timeframe.H1: 60, Timeframe.H4: 240}[timeframe]


def _market_closed(specification: SymbolSpecification | None) -> bool:
    if specification is None:
        return False
    state = specification.trade_mode_name.casefold()
    return any(token in state for token in ("closed", "disabled", "not tradable"))
