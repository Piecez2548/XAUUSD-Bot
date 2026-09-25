"""Forward-only Pair Zone validation worker.

The worker is deliberately separate from historical research and the existing
Phase 2 shadow decision stream.  It consumes closed read-only snapshots,
persists a durable session, and evaluates virtual trades only with future
closed M5 candles.  It never imports or calls an MT5 order API.
"""
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sqlalchemy import desc, func, select

from models.market import MarketSnapshot, Timeframe
from persistence.orm import (
    CandleRecord,
    ForwardSignalRecord,
    ForwardTradeRecord,
    ForwardValidationSessionRecord,
    PairZoneEvaluationRecord,
    SymbolRecord,
    SystemHealthRecord,
)
from persistence.repositories import SystemHealthRepository
from services.pair_zone_strategy import PairZoneV1
from services.shadow_outcome import (
    AMBIGUOUS,
    EXPIRED,
    PENDING,
    SL_HIT,
    TP_HIT,
    EvaluationPolicy,
    evaluate_decision,
)

EXPECTED_PAIR_ZONE_FILE_SHA256 = "fd2d73b9aa0d21004653e455263107caf727ac552fd204486f363cb7c25b7ded"
FORWARD_COMPONENT = "worker:forward_shadow"
PAIR_ZONE_EVALUATION_MAX_AGE_SECONDS = 900.0
PAIR_ZONE_M5_DATA_MAX_AGE_SECONDS = 600.0
PAIR_ZONE_M15_DATA_MAX_AGE_SECONDS = 900.0
FORWARD_POLICY_VERSION = "forward_shadow_v1"
FORWARD_STATUS_ACTIVE = "ACTIVE"
FORWARD_STATUS_PAUSED = "PAUSED"
FORWARD_STATUS_COMPLETED = "COMPLETED"
FORWARD_STATUS_ERROR = "ERROR"
FORWARD_COST_POLICY = {
    "entry_slippage_points": 0.5,
    "exit_slippage_points": 0.5,
    "commission_r": 0.02,
    "spread_fallback_points": 20.0,
    "spread_source": "actual_symbol_observation_or_configured_fallback",
}


@dataclass(frozen=True, slots=True)
class ForwardInput:
    snapshot: MarketSnapshot
    market_snapshot_id: Any
    risk: Any
    candles_are_closed: bool = True


def pair_zone_file_hash(path: Path | None = None) -> str:
    target = path or Path(__file__).resolve().parents[1] / "config" / "strategies" / "pair_zone_v1.yaml"
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _candle_json(candle: Any | None) -> dict[str, Any]:
    if candle is None:
        return {}
    return {key: getattr(candle, key) for key in ("timestamp", "open", "high", "low", "close", "tick_volume", "spread", "real_volume") if hasattr(candle, key)}


def _feature_candle(snapshot: MarketSnapshot, timestamp: datetime) -> Any | None:
    for candle in snapshot.candles.get(Timeframe.M5, ()):
        if candle.timestamp == timestamp:
            return candle
    return None


def _risk_distance(side: str, entry: float, stop: float) -> float:
    return entry - stop if side == "BUY" else stop - entry


def forward_cost_r(*, side: str, entry: float, stop: float, spread_points: float,
                   point: float, entry_slippage_points: float,
                   exit_slippage_points: float, commission_r: float) -> float:
    risk = _risk_distance(side, entry, stop)
    if risk <= 0:
        return 0.0
    return ((spread_points + entry_slippage_points + exit_slippage_points) * point / risk) + commission_r


def _drawdown(values: list[float]) -> tuple[float, int, int]:
    equity = peak = max_dd = 0.0
    loss_streak = win_streak = max_loss = max_win = 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if value < 0:
            loss_streak += 1
            win_streak = 0
        elif value > 0:
            win_streak += 1
            loss_streak = 0
        else:
            loss_streak = win_streak = 0
        max_loss = max(max_loss, loss_streak)
        max_win = max(max_win, win_streak)
    return max_dd, max_loss, max_win


def forward_performance_rows(rows: list[ForwardTradeRecord]) -> dict[str, Any]:
    """Calculate forward-only metrics and explicit expiry contribution."""
    ordered = sorted(rows, key=lambda row: row.timestamp)
    terminal = [row for row in ordered if row.state in {"TP", "SL", "AMBIGUOUS", "EXPIRED"}]

    def block(selected: list[ForwardTradeRecord]) -> dict[str, Any]:
        resolved = [row for row in selected if row.gross_r is not None]
        gross = [float(row.gross_r) for row in resolved]
        net = [float(row.net_r) for row in resolved if row.net_r is not None]
        wins = [value for value in net if value > 0]
        losses = [value for value in net if value < 0]
        dd, loss_streak, win_streak = _drawdown(net)
        return {
            "signals": len(selected), "resolved": len(resolved),
            "gross_total_r": sum(gross), "net_total_r": sum(net),
            "gross_expectancy": sum(gross) / len(gross) if gross else None,
            "net_expectancy": sum(net) / len(net) if net else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "win_rate": len(wins) / len(net) if net else None,
            "max_drawdown_r": dd, "max_loss_streak": loss_streak,
            "max_win_streak": win_streak,
            "average_spread": sum(float(row.spread_points) for row in selected) / len(selected) if selected else None,
            "average_cost_r": sum(float(row.total_cost_r or 0) for row in resolved) / len(resolved) if resolved else None,
            "TP": sum(row.state == "TP" for row in selected),
            "SL": sum(row.state == "SL" for row in selected),
            "AMBIGUOUS": sum(row.state == "AMBIGUOUS" for row in selected),
            "EXPIRED": sum(row.state == "EXPIRED" for row in selected),
            "OPEN": sum(row.state == "OPEN" for row in selected),
        }

    by_side = {side: block([row for row in terminal if row.side == side]) for side in ("BUY", "SELL")}
    tp_sl = [row for row in terminal if row.state in {"TP", "SL", "AMBIGUOUS"}]
    expired = [row for row in terminal if row.state == "EXPIRED"]
    return {
        "signals": len(ordered), "BUY": sum(row.side == "BUY" for row in ordered),
        "SELL": sum(row.side == "SELL" for row in ordered), "OPEN": sum(row.state == "OPEN" for row in ordered),
        "TP": sum(row.state == "TP" for row in ordered), "SL": sum(row.state == "SL" for row in ordered),
        "AMBIGUOUS": sum(row.state == "AMBIGUOUS" for row in ordered), "EXPIRED": sum(row.state == "EXPIRED" for row in ordered),
        "combined": block(terminal), "BUY_metrics": by_side["BUY"], "SELL_metrics": by_side["SELL"],
        "tp_sl_only": block(tp_sl), "expired_only": block(expired),
    }


class ForwardShadowWorker:
    """Durable forward Pair Zone evaluator with restart-safe idempotency."""

    def __init__(
        self,
        settings,
        database,
        *,
        logger: logging.Logger,
        execution_handler: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.logger = logger
        self.execution_handler = execution_handler
        self.health = SystemHealthRepository(database)
        self.strategy = PairZoneV1(settings)
        self.runtime_generation_id = str(uuid4())
        self.policy = EvaluationPolicy(horizon_bars=getattr(settings, "shadow_outcome_horizon_bars", 12))
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[ForwardInput] = asyncio.Queue(maxsize=8)
        self._queued: set[datetime] = set()
        self._processed: set[datetime] = set()
        self._stop = asyncio.Event()
        self.session: ForwardValidationSessionRecord | None = None
        self._drift_detected = False
        self._last_closed_m5: datetime | None = None
        self._last_closed_m15: datetime | None = None
        self._last_closed_h1: datetime | None = None
        self._last_zone_created: datetime | None = None
        self._last_signal: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_failed_at: datetime | None = None
        self._failure_count = 0
        self._last_health_write: datetime | None = None

    @property
    def state(self) -> str:
        if not self.settings.forward_shadow_enabled:
            return "DISABLED"
        if self._drift_detected:
            return "ERROR"
        if self._failure_count >= 3:
            return "ERROR"
        if self._failure_count:
            return "DEGRADED"
        return "CONNECTED" if self._task is not None else "UNKNOWN"

    def start(self) -> None:
        if not self.settings.forward_shadow_enabled:
            self._record_health("DISABLED", "Forward shadow validation disabled", force=True)
            return
        if self._task is None:
            self._initialize_session()
            if not self._drift_detected:
                self._record_health(
                    "STARTING", "Forward shadow generation initialized", force=True
                )
                self._stop.clear()
                self._task = asyncio.create_task(self._run(), name="forward-shadow-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.session is not None and self.session.status == FORWARD_STATUS_ACTIVE:
            with self.database.session() as session:
                row = session.get(ForwardValidationSessionRecord, self.session.id)
                if row is not None:
                    row.status = FORWARD_STATUS_PAUSED

    def submit(self, item: ForwardInput) -> None:
        if not self.settings.forward_shadow_enabled or self._drift_detected:
            return
        m5 = item.snapshot.candles.get(Timeframe.M5, ())
        if not item.candles_are_closed or not m5:
            return
        timestamp = m5[-1].timestamp
        self._last_closed_m5 = timestamp
        self._last_closed_m15 = item.snapshot.candles.get(Timeframe.M15, ())[-1].timestamp if item.snapshot.candles.get(Timeframe.M15) else None
        self._last_closed_h1 = item.snapshot.candles.get(Timeframe.H1, ())[-1].timestamp if item.snapshot.candles.get(Timeframe.H1) else None
        if timestamp in self._queued or timestamp in self._processed:
            return
        try:
            self._queue.put_nowait(item)
            self._queued.add(timestamp)
        except asyncio.QueueFull:
            self._failure_count += 1
            self._last_failed_at = datetime.now(UTC)
            self._record_health("DEGRADED", "Forward shadow queue is full", error_category="QueueFull")

    def _initialize_session(self) -> None:
        actual_hash = pair_zone_file_hash()
        health_error: str | None = None
        with self.database.session() as session:
            existing = session.scalar(
                select(ForwardValidationSessionRecord)
                .where(ForwardValidationSessionRecord.status.in_((FORWARD_STATUS_ACTIVE, FORWARD_STATUS_PAUSED)))
                .order_by(desc(ForwardValidationSessionRecord.started_at))
                .limit(1)
            )
            if actual_hash != EXPECTED_PAIR_ZONE_FILE_SHA256:
                self._drift_detected = True
                self.session = ForwardValidationSessionRecord(
                    session_id=f"forward_{uuid4()}", strategy_id="pair_zone_v1", strategy_version="1.0.0",
                    strategy_config_hash=actual_hash, started_at=datetime.now(UTC), source_identity="live_mt5_read_only",
                    symbol=self.settings.trading_symbol or "UNKNOWN", timeframes_json=["M5", "M15", "H1"],
                    rr=float(self.settings.forward_shadow_rr), cost_policy_json=FORWARD_COST_POLICY,
                    status=FORWARD_STATUS_ERROR, error_reason="STRATEGY_DRIFT_DETECTED", execution_allowed=False,
                )
                session.add(self.session)
                health_error = "STRATEGY_DRIFT_DETECTED"
            elif existing is not None and existing.strategy_config_hash == actual_hash:
                self.session = existing
                if existing.status == FORWARD_STATUS_PAUSED:
                    existing.status = FORWARD_STATUS_ACTIVE
            elif existing is not None and existing.strategy_config_hash != actual_hash:
                self._drift_detected = True
                self.session = ForwardValidationSessionRecord(
                    session_id=f"forward_{uuid4()}",
                    strategy_id=self.strategy.metadata.strategy_id,
                    strategy_version=self.strategy.metadata.strategy_version,
                    strategy_config_hash=actual_hash,
                    started_at=datetime.now(UTC),
                    source_identity="live_mt5_read_only",
                    symbol=self.settings.trading_symbol or "UNKNOWN",
                    timeframes_json=["M5", "M15", "H1"],
                    rr=float(self.settings.forward_shadow_rr),
                    cost_policy_json=FORWARD_COST_POLICY,
                    status=FORWARD_STATUS_ERROR,
                    error_reason="STRATEGY_DRIFT_DETECTED",
                    execution_allowed=False,
                )
                session.add(self.session)
                health_error = "STRATEGY_DRIFT_DETECTED"
            else:
                self.session = ForwardValidationSessionRecord(
                    session_id=f"forward_{uuid4()}",
                    strategy_id=self.strategy.metadata.strategy_id,
                    strategy_version=self.strategy.metadata.strategy_version,
                    strategy_config_hash=EXPECTED_PAIR_ZONE_FILE_SHA256,
                    started_at=datetime.now(UTC),
                    source_identity="live_mt5_read_only",
                    symbol=self.settings.trading_symbol or "UNKNOWN",
                    timeframes_json=["M5", "M15", "H1"],
                    rr=float(self.settings.forward_shadow_rr),
                    cost_policy_json=FORWARD_COST_POLICY,
                    status=FORWARD_STATUS_ACTIVE,
                    execution_allowed=False,
                )
                session.add(self.session)
        if health_error:
            self._record_health(
                "ERROR",
                health_error,
                force=True,
                error_category=health_error,
            )

    def _ensure_session_symbol(self, symbol: str) -> None:
        if self.session is None:
            with self.database.session() as session:
                row = ForwardValidationSessionRecord(
                    session_id=f"forward_{uuid4()}", strategy_id="pair_zone_v1", strategy_version=self.strategy.metadata.strategy_version,
                    strategy_config_hash=EXPECTED_PAIR_ZONE_FILE_SHA256, started_at=datetime.now(UTC),
                    source_identity="live_mt5_read_only", symbol=symbol, timeframes_json=["M5", "M15", "H1"],
                    rr=float(self.settings.forward_shadow_rr), cost_policy_json=FORWARD_COST_POLICY,
                    status=FORWARD_STATUS_ACTIVE, execution_allowed=False,
                )
                session.add(row)
                session.flush()
                self.session = row
        elif self.session.symbol == "UNKNOWN":
            with self.database.session() as session:
                row = session.get(ForwardValidationSessionRecord, self.session.id)
                if row is not None:
                    row.symbol = symbol
                    self.session.symbol = symbol

    async def _run(self) -> None:
        self._record_health("CONNECTED", "Forward shadow worker started", force=True)
        try:
            while not self._stop.is_set():
                self._record_health(self.state, "Forward shadow worker heartbeat")
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                timestamp = item.snapshot.candles[Timeframe.M5][-1].timestamp
                self._queued.discard(timestamp)
                await self._process(item)
                self._processed.add(timestamp)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure_count += 1
            self._last_failed_at = datetime.now(UTC)
            self._record_health("ERROR", "Forward shadow worker stopped unexpectedly", force=True, error_category=type(exc).__name__)
            self.logger.exception("Forward shadow worker terminated: %s", type(exc).__name__)
        finally:
            if not self._stop.is_set():
                self._record_health("DEGRADED", "Forward shadow worker stopped", force=True)

    async def _process(self, item: ForwardInput) -> None:
        self._ensure_session_symbol(item.snapshot.symbol.name)
        if self.session is None:
            return
        now = datetime.now(UTC)
        try:
            decision = self.strategy.evaluate(
                item.snapshot, market_snapshot_id=item.market_snapshot_id,
                risk=item.risk, candles_are_closed=True,
            )
        except Exception:
            self._persist_pair_zone_evaluation(
                None, item.snapshot, reason_override="DETECTOR_EXCEPTION"
            )
            raise
        timestamp = decision.m5_candle_timestamp
        if timestamp <= self.session.started_at:
            self._persist_pair_zone_evaluation(
                decision, item.snapshot, reason_override="FORWARD_BOUNDARY_NOT_REACHED"
            )
            await self._evaluate_open_trades()
            self._record_health("CONNECTED", "Historical context warmed; forward boundary not reached", force=True)
            return
        self._persist_pair_zone_evaluation(decision, item.snapshot)
        # Phase 3 records a separate, versioned evidence snapshot. The
        # existing Pair Zone decision and Forward Shadow history remain the
        # canonical behavior; intelligence is additive and execution-disabled.
        intelligence_available = True
        intelligence_record = None
        provenance_linked = False
        try:
            from services.intelligence import (
                StrategyIntelligenceEngine,
                persist_intelligence_record,
            )

            intelligence = StrategyIntelligenceEngine(self.settings).evaluate(
                item.snapshot, risk=item.risk, as_of=timestamp, candles_are_closed=True
            )
            persist_kwargs = {"forward_session_id": self.session.id}
            zone_id = decision.feature_context.get("zone_id") if isinstance(decision.feature_context, dict) else None
            if zone_id:
                persist_kwargs["pair_zone_event_id"] = str(zone_id)
            intelligence_record = persist_intelligence_record(self.database, intelligence, **persist_kwargs)
        except Exception as exc:
            # Intelligence is advisory metadata. Its failure must not rewrite
            # or suppress the canonical Pair Zone/Forward Shadow decision.
            intelligence_available = False
            self.logger.exception(
                "Strategy intelligence unavailable; preserving Forward Shadow decision: %s",
                type(exc).__name__,
            )
        self._last_success_at = now
        record = None
        if decision.decision.value in {"BUY", "SELL"}:
            record = self._persist_signal(decision, item.snapshot)
            if record is not None:
                self._last_signal = record.timestamp
                self._last_zone_created = _parse_iso(decision.feature_context.get("zone_created_at"))
                if intelligence_record is not None:
                    try:
                        from services.intelligence import link_intelligence_forward_provenance

                        link_intelligence_forward_provenance(
                            self.database,
                            candidate_id=intelligence_record.candidate_id,
                            forward_session_id=self.session.id,
                            forward_signal_id=record.id,
                        )
                        provenance_linked = True
                    except Exception as exc:
                        intelligence_available = False
                        self.logger.exception(
                            "Strategy intelligence provenance unavailable; preserving Forward Shadow decision: %s",
                            type(exc).__name__,
                        )
                if self.execution_handler is not None and provenance_linked:
                    try:
                        await self.execution_handler(
                            signal=record,
                            decision=decision,
                            snapshot=item.snapshot,
                            risk=item.risk,
                            intelligence_record=intelligence_record,
                        )
                    except Exception as exc:
                        self.logger.exception(
                            "Demo execution handler failed; preserving Forward Shadow decision: %s",
                            type(exc).__name__,
                        )
        await self._evaluate_open_trades()
        self._failure_count = 0
        self._record_health(
            "CONNECTED" if intelligence_available else "DEGRADED",
            "Forward shadow cycle completed"
            if intelligence_available
            else "Forward shadow cycle completed; strategy intelligence unavailable",
            force=True,
            error_category=None if intelligence_available else "INTELLIGENCE_UNAVAILABLE",
        )
        if intelligence_record is not None:
            try:
                from services.model_inference import persist_advisory_evaluation

                persist_advisory_evaluation(
                    self.database,
                    candidate_id=intelligence_record.candidate_id,
                    forward_session_id=self.session.id,
                    forward_signal_id=(
                        record.id if record is not None and provenance_linked else None
                    ),
                )
            except Exception as exc:
                # This optional offline result must not affect Forward Shadow,
                # canonical signals, or the separately gated Demo handler.
                self.logger.warning(
                    "Offline advisory inference unavailable (%s)", type(exc).__name__
                )

    def _persist_signal(self, decision: Any, snapshot: MarketSnapshot) -> ForwardSignalRecord | None:
        assert self.session is not None
        context = decision.feature_context or {}
        zone_id = str(context.get("zone_id", "unknown"))
        entry = float(decision.entry_price)
        stop = float(decision.stop_loss)
        target = float(decision.take_profit)
        m5 = _feature_candle(snapshot, decision.m5_candle_timestamp)
        spread = float(snapshot.symbol.spread) if snapshot.symbol.spread is not None else float(FORWARD_COST_POLICY["spread_fallback_points"])
        observation = {
            "bid": snapshot.tick.bid, "ask": snapshot.tick.ask, "spread": spread,
            "digits": snapshot.symbol.digits, "point": snapshot.symbol.point,
            "trade_tick_size": snapshot.symbol.trade_tick_size, "contract_size": snapshot.symbol.contract_size,
            "volume_min": snapshot.symbol.volume_min, "volume_step": snapshot.symbol.volume_step,
        }
        first = _parse_iso(context.get("pair_first_timestamp"))
        second = _parse_iso(context.get("pair_second_timestamp"))
        signal_id = f"forward-signal-{self.session.session_id}-{decision.m5_candle_timestamp.isoformat()}"
        with self.database.session() as session:
            existing = session.scalar(select(ForwardSignalRecord).where(ForwardSignalRecord.signal_id == signal_id))
            if existing is not None:
                return existing
            row = ForwardSignalRecord(
                signal_id=signal_id, session_id=self.session.id, timestamp=decision.m5_candle_timestamp,
                decision=decision.decision.value, zone_id=zone_id, entry_price=entry, stop_loss=stop,
                risk_distance=_risk_distance(decision.decision.value, entry, stop), rr=float(self.session.rr), take_profit=target,
                pair_first_timestamp=first, pair_second_timestamp=second,
                h1_context_json={"direction": decision.market_regime.value, "feature_context": context},
                confirmation_candle_json=_candle_json(m5), market_observation_json=observation,
                strategy_hash=EXPECTED_PAIR_ZONE_FILE_SHA256, execution_allowed=False,
            )
            session.add(row)
            session.flush()
            cost = forward_cost_r(side=row.decision, entry=entry, stop=stop, spread_points=spread,
                                  point=snapshot.symbol.point, entry_slippage_points=float(FORWARD_COST_POLICY["entry_slippage_points"]),
                                  exit_slippage_points=float(FORWARD_COST_POLICY["exit_slippage_points"]), commission_r=float(FORWARD_COST_POLICY["commission_r"]))
            session.add(ForwardTradeRecord(
                trade_id=f"forward-trade-{row.signal_id}", session_id=self.session.id, signal_id=row.id,
                timestamp=row.timestamp, side=row.decision, state="OPEN", entry_price=entry,
                stop_loss=stop, take_profit=target, risk_distance=row.risk_distance,
                spread_points=spread, spread_observation="ACTUAL" if snapshot.symbol.spread is not None else "ESTIMATED",
                entry_slippage_points=float(FORWARD_COST_POLICY["entry_slippage_points"]),
                exit_slippage_points=float(FORWARD_COST_POLICY["exit_slippage_points"]),
                commission_r=float(FORWARD_COST_POLICY["commission_r"]), total_cost_r=cost,
                execution_allowed=False,
            ))
            return row

    def _persist_pair_zone_evaluation(
        self,
        decision: Any | None,
        snapshot: MarketSnapshot,
        *,
        reason_override: str | None = None,
    ) -> None:
        """Replace the session's single current observation, not per-tick history."""

        if self.session is None:
            return
        context = getattr(decision, "feature_context", None)
        observation = context.get("pair_zone_observation") if isinstance(context, dict) else None
        state = "UNKNOWN"
        reason = reason_override or "AUTHORITATIVE_OBSERVATION_MISSING"
        direction = zone_id = None
        lower = upper = None
        if reason_override is None and isinstance(observation, dict):
            candidate_state = observation.get("state")
            candidate_reason = observation.get("reason")
            if candidate_state in {"ACTIVE_ZONE", "HEALTHY_NO_ACTIVE_ZONE", "UNKNOWN"}:
                state = candidate_state
                reason = str(candidate_reason or "UNSPECIFIED")[:100]
                direction = observation.get("direction")
                zone_id = observation.get("zone_id")
                lower = observation.get("zone_lower")
                upper = observation.get("zone_upper")
                if state == "ACTIVE_ZONE" and (
                    direction not in {"BUY", "SELL"}
                    or not isinstance(zone_id, str)
                    or not zone_id
                    or not isinstance(lower, (int, float))
                    or not isinstance(upper, (int, float))
                    or not math.isfinite(lower)
                    or not math.isfinite(upper)
                    or lower >= upper
                ):
                    state = "UNKNOWN"
                    reason = "ACTIVE_ZONE_PROVENANCE_INCOMPLETE"
                    direction = zone_id = None
                    lower = upper = None
                elif state == "HEALTHY_NO_ACTIVE_ZONE" and any(
                    value is not None for value in (direction, zone_id, lower, upper)
                ):
                    state = "UNKNOWN"
                    reason = "NO_ZONE_PROVENANCE_INCONSISTENT"
                    direction = zone_id = None
                    lower = upper = None
            else:
                reason = "INVALID_OBSERVATION_STATE"

        m5_candles = snapshot.candles.get(Timeframe.M5, ())
        m15_candles = snapshot.candles.get(Timeframe.M15, ())
        evaluated_m5 = (
            getattr(decision, "m5_candle_timestamp", None)
            or (m5_candles[-1].timestamp if m5_candles else None)
        )
        evaluated_m15 = m15_candles[-1].timestamp if m15_candles else None
        config_hash = pair_zone_file_hash()
        if config_hash != getattr(
            self.session, "strategy_config_hash", EXPECTED_PAIR_ZONE_FILE_SHA256
        ):
            state = "UNKNOWN"
            reason = "STRATEGY_CONFIG_MISMATCH"
            direction = zone_id = None
            lower = upper = None

        values = {
            "runtime_generation_id": self.runtime_generation_id,
            "evaluation_at": datetime.now(UTC),
            "evaluated_m5_timestamp": evaluated_m5,
            "evaluated_m15_timestamp": evaluated_m15,
            "strategy_id": self.strategy.metadata.strategy_id,
            "strategy_version": self.strategy.metadata.strategy_version,
            "config_hash": config_hash,
            "state": state,
            "reason": reason,
            "direction": direction,
            "zone_id": zone_id,
            "zone_lower": lower,
            "zone_upper": upper,
        }
        with self.database.session() as session:
            if session.get(ForwardValidationSessionRecord, self.session.id) is None:
                return
            row = session.get(PairZoneEvaluationRecord, self.session.id)
            if row is None:
                session.add(PairZoneEvaluationRecord(
                    forward_session_id=self.session.id, **values
                ))
            else:
                for name, value in values.items():
                    setattr(row, name, value)

    async def _evaluate_open_trades(self) -> None:
        if self.session is None:
            return
        with self.database.session() as session:
            trades = session.scalars(select(ForwardTradeRecord).where(ForwardTradeRecord.session_id == self.session.id, ForwardTradeRecord.state == "OPEN")).all()
            for trade in trades:
                candles = list(session.scalars(select(CandleRecord).join(SymbolRecord, CandleRecord.symbol_id == SymbolRecord.id).where(SymbolRecord.name == self.session.symbol, CandleRecord.timeframe == "M5", CandleRecord.timestamp > trade.timestamp).order_by(CandleRecord.timestamp, CandleRecord.id).limit(self.policy.horizon_bars)))
                decision = SimpleNamespace(decision=trade.side, entry_price=trade.entry_price, stop_loss=trade.stop_loss, take_profit=trade.take_profit, m5_candle_timestamp=trade.timestamp)
                values = evaluate_decision(decision, candles, policy=self.policy)
                if values["terminal_status"] == PENDING:
                    continue
                status_map = {TP_HIT: "TP", SL_HIT: "SL", AMBIGUOUS: "AMBIGUOUS", EXPIRED: "EXPIRED"}
                state = status_map.get(str(values["terminal_status"]))
                if state is None:
                    continue
                terminal_timestamp = values["terminal_candle_timestamp"]
                gross = values["realized_r"]
                cost = float(trade.total_cost_r or 0.0)
                trade.state = state
                trade.terminal_timestamp = terminal_timestamp
                trade.mark_price = values.get("exit_price") or (candles[-1].close if candles else None)
                trade.gross_r = float(gross) if gross is not None else None
                trade.net_r = float(gross) - cost if gross is not None else None
                trade.bars_held = int(values.get("bars_held") or 0)
                trade.minutes_held = ((terminal_timestamp - trade.timestamp).total_seconds() / 60.0) if terminal_timestamp else None
                trade.mfe_price = values.get("max_favorable_excursion_price")
                trade.mae_price = values.get("max_adverse_excursion_price")
                trade.mfe_r = values.get("mfe_r")
                trade.mae_r = values.get("mae_r")
                trade.evaluated_at = datetime.now(UTC)
                trade.reason_code = str(values.get("reason_code") or FORWARD_POLICY_VERSION)

    def _record_health(self, state: str, message: str, *, force: bool = False, error_category: str | None = None) -> None:
        now = datetime.now(UTC)
        if not force and self._last_health_write and (now - self._last_health_write).total_seconds() < 5:
            return
        self._last_health_write = now
        with self.database.session() as session:
            session_id = self.session.session_id if self.session is not None else None
            if self.session is not None:
                row = session.get(ForwardValidationSessionRecord, self.session.id)
                if row is not None:
                    row.updated_at = now
                total = int(
                    session.scalar(
                        select(func.count())
                        .select_from(ForwardSignalRecord)
                        .where(ForwardSignalRecord.session_id == self.session.id)
                    )
                    or 0
                )
                open_trades = int(
                    session.scalar(
                        select(func.count())
                        .select_from(ForwardTradeRecord)
                        .where(
                            ForwardTradeRecord.session_id == self.session.id,
                            ForwardTradeRecord.state == "OPEN",
                        )
                    )
                    or 0
                )
            else:
                total = 0
                open_trades = 0
        self.health.record(FORWARD_COMPONENT, state, message=message, metadata={
            "heartbeat_at": now.isoformat(), "session_id": session_id,
            "runtime_generation_id": self.runtime_generation_id,
            "pid": os.getpid(),
            "last_closed_m5": _iso(self._last_closed_m5), "last_closed_m15": _iso(self._last_closed_m15),
            "last_closed_h1": _iso(self._last_closed_h1), "last_zone_created": _iso(self._last_zone_created),
            "last_signal": _iso(self._last_signal), "open_shadow_trades": open_trades,
            "total_forward_signals": total, "failure_count": self._failure_count,
            "last_failed_at": _iso(self._last_failed_at), "last_success_at": _iso(self._last_success_at),
            "error_category": error_category, "execution_allowed": False,
        })


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo else result.replace(tzinfo=UTC)
    except ValueError:
        return None


def latest_forward_session(database) -> ForwardValidationSessionRecord | None:
    with database.session() as session:
        return session.scalar(select(ForwardValidationSessionRecord).order_by(desc(ForwardValidationSessionRecord.started_at)).limit(1))


def _session_by_id(database, session_id: str | None) -> ForwardValidationSessionRecord | None:
    with database.session() as session:
        if session_id:
            return session.scalar(select(ForwardValidationSessionRecord).where(ForwardValidationSessionRecord.session_id == session_id))
        return session.scalar(select(ForwardValidationSessionRecord).order_by(desc(ForwardValidationSessionRecord.started_at)).limit(1))


def forward_health(database, settings) -> dict[str, Any]:
    now = datetime.now(UTC)
    if not settings.forward_shadow_enabled:
        return {"worker": FORWARD_COMPONENT, "state": "DISABLED", "observed_at": None,
                "age_seconds": None, "read_only": True, "execution_allowed": False,
                "checked_at": now}
    with database.session() as session:
        row = session.scalar(
            select(SystemHealthRecord)
            .where(SystemHealthRecord.component == FORWARD_COMPONENT)
            .order_by(desc(SystemHealthRecord.timestamp))
            .limit(1)
        )
    from services.worker_health import worker_health_payload
    payload = worker_health_payload(
        row,
        interval_seconds=settings.live_history_interval_seconds,
        now=now,
    )
    payload.update({"worker": FORWARD_COMPONENT, "read_only": True, "execution_allowed": False, "checked_at": now})
    return payload


def pair_zone_status(
    database,
    settings,
    *,
    session_id: str | None,
    forward_health_payload: dict[str, Any],
    verified_live_process_identities: tuple[tuple[int, str | None], ...],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Expose only fresh Pair Zone state from the current worker generation."""

    unknown: dict[str, Any] = {
        "state": "UNKNOWN", "current_direction": "UNKNOWN",
        "reason": "AUTHORITATIVE_STATE_UNAVAILABLE", "evaluated_at": None,
        "evaluated_m5_timestamp": None, "evaluated_m15_timestamp": None,
        "zone_id": None, "zone_lower": None, "zone_upper": None,
    }
    if forward_health_payload.get("state") != "CONNECTED":
        unknown["reason"] = "FORWARD_WORKER_NOT_CONNECTED"
        return unknown
    generation_id = forward_health_payload.get("runtime_generation_id")
    if not session_id or not isinstance(generation_id, str) or not generation_id:
        unknown["reason"] = "CURRENT_SESSION_OR_GENERATION_UNVERIFIED"
        return unknown
    worker_pid = forward_health_payload.get("pid")
    if (
        type(worker_pid) is not int
        or not any(
            pid == worker_pid and isinstance(create_time, str) and create_time
            for pid, create_time in verified_live_process_identities
        )
    ):
        unknown["reason"] = "WORKER_PROCESS_OWNERSHIP_UNVERIFIED"
        return unknown
    if forward_health_payload.get("session_id") != session_id:
        unknown["reason"] = "FORWARD_SESSION_MISMATCH"
        return unknown
    try:
        actual_hash = pair_zone_file_hash()
        with database.session() as session:
            forward_session = session.scalar(
                select(ForwardValidationSessionRecord).where(
                    ForwardValidationSessionRecord.session_id == session_id
                )
            )
            row = session.get(
                PairZoneEvaluationRecord,
                forward_session.id if forward_session is not None else "",
            )
            if (
                forward_session is None
                or forward_session.status != FORWARD_STATUS_ACTIVE
                or forward_session.strategy_id != "pair_zone_v1"
                or forward_session.strategy_config_hash != EXPECTED_PAIR_ZONE_FILE_SHA256
                or actual_hash != EXPECTED_PAIR_ZONE_FILE_SHA256
                or row is None
            ):
                unknown["reason"] = "SESSION_CONFIG_OR_EVALUATION_UNVERIFIED"
                return unknown
            if (
                row.runtime_generation_id != generation_id
                or row.strategy_id != forward_session.strategy_id
                or row.strategy_version != forward_session.strategy_version
                or row.config_hash != actual_hash
            ):
                unknown["reason"] = "SESSION_GENERATION_OR_CONFIG_MISMATCH"
                return unknown
            if row.state not in {"ACTIVE_ZONE", "HEALTHY_NO_ACTIVE_ZONE", "UNKNOWN"}:
                unknown["reason"] = "PERSISTED_STATE_INVALID"
                return unknown
            last_m5 = _parse_iso(forward_health_payload.get("last_closed_m5"))
            last_m15 = _parse_iso(forward_health_payload.get("last_closed_m15"))
            if (
                row.evaluated_m5_timestamp is None
                or row.evaluated_m15_timestamp is None
                or row.evaluated_m5_timestamp != last_m5
                or row.evaluated_m15_timestamp != last_m15
            ):
                unknown["reason"] = "EVALUATION_CANDLE_BOUNDARY_MISMATCH"
                return unknown
            current = now or datetime.now(UTC)
            evaluated_at = row.evaluation_at
            if evaluated_at.tzinfo is None:
                evaluated_at = evaluated_at.replace(tzinfo=UTC)
            max_age = max(
                PAIR_ZONE_EVALUATION_MAX_AGE_SECONDS,
                float(settings.live_candle_interval_seconds) * 4.0,
            )
            evaluation_age = (current - evaluated_at).total_seconds()
            if evaluation_age < -60.0 or evaluation_age > max_age:
                unknown["reason"] = "EVALUATION_STALE"
                return unknown
            m5_closed_at = row.evaluated_m5_timestamp + timedelta(minutes=5)
            m15_closed_at = row.evaluated_m15_timestamp + timedelta(minutes=15)
            for candle_closed_at, data_max_age in (
                (m5_closed_at, PAIR_ZONE_M5_DATA_MAX_AGE_SECONDS),
                (m15_closed_at, PAIR_ZONE_M15_DATA_MAX_AGE_SECONDS),
            ):
                candle_age = (current - candle_closed_at).total_seconds()
                if candle_age < -60.0 or candle_age > data_max_age:
                    unknown["reason"] = "EVALUATED_CANDLE_STALE_OR_FUTURE"
                    return unknown
            if row.state == "UNKNOWN":
                unknown.update({
                    "reason": row.reason,
                    "evaluated_at": row.evaluation_at,
                    "evaluated_m5_timestamp": row.evaluated_m5_timestamp,
                    "evaluated_m15_timestamp": row.evaluated_m15_timestamp,
                })
                return unknown
            if row.state == "ACTIVE_ZONE" and (
                row.direction not in {"BUY", "SELL"}
                or not row.zone_id
                or row.zone_lower is None
                or row.zone_upper is None
                or not math.isfinite(row.zone_lower)
                or not math.isfinite(row.zone_upper)
                or row.zone_lower >= row.zone_upper
            ):
                unknown["reason"] = "ACTIVE_ZONE_PROVENANCE_INCOMPLETE"
                return unknown
            return {
                "state": row.state,
                "current_direction": row.direction or "NONE",
                "reason": row.reason,
                "evaluated_at": row.evaluation_at,
                "evaluated_m5_timestamp": row.evaluated_m5_timestamp,
                "evaluated_m15_timestamp": row.evaluated_m15_timestamp,
                "zone_id": row.zone_id,
                "zone_lower": row.zone_lower,
                "zone_upper": row.zone_upper,
            }
    except Exception:
        # Status must remain available while uncertain persisted state fails closed.
        unknown["reason"] = "PAIR_ZONE_STATE_READ_FAILED"
        return unknown


def forward_signals(database, session_id: str | None = None, limit: int = 100) -> list[ForwardSignalRecord]:
    with database.session() as session:
        query = select(ForwardSignalRecord).order_by(desc(ForwardSignalRecord.timestamp)).limit(limit)
        if session_id:
            row = session.scalar(select(ForwardValidationSessionRecord).where(ForwardValidationSessionRecord.session_id == session_id))
            if row is None:
                return []
            query = query.where(ForwardSignalRecord.session_id == row.id)
        return list(session.scalars(query))


def forward_trades(database, session_id: str | None = None, limit: int = 100) -> list[ForwardTradeRecord]:
    with database.session() as session:
        query = select(ForwardTradeRecord).order_by(desc(ForwardTradeRecord.timestamp)).limit(limit)
        if session_id:
            row = session.scalar(select(ForwardValidationSessionRecord).where(ForwardValidationSessionRecord.session_id == session_id))
            if row is None:
                return []
            query = query.where(ForwardTradeRecord.session_id == row.id)
        return list(session.scalars(query))


def forward_performance(database, session_id: str | None = None) -> dict[str, Any]:
    session_row = _session_by_id(database, session_id)
    if session_row is None:
        return {"session": None, "execution_allowed": False, "read_only": True, **forward_performance_rows([])}
    with database.session() as session:
        rows = list(session.scalars(select(ForwardTradeRecord).where(ForwardTradeRecord.session_id == session_row.id).order_by(ForwardTradeRecord.timestamp)))
    result = forward_performance_rows(rows)
    result.update({"session_id": session_row.session_id, "strategy_id": session_row.strategy_id, "strategy_version": session_row.strategy_version, "strategy_config_hash": session_row.strategy_config_hash, "started_at": session_row.started_at, "status": session_row.status, "execution_allowed": False, "read_only": True})
    return result
