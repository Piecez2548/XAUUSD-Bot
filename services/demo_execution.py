"""Fail-closed MT5 Demo execution for canonical Forward Shadow signals.

This module is the only production path permitted to call ``order_send``.
It requires the explicit Demo environment gate and the persisted Telegram
kill-switch arm.  Real accounts, missing provenance, stale data, invalid
levels, unknown broker results, and indeterminate retries all fail closed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from config.settings import Settings
from models.market import MarketSnapshot, Timeframe
from mt5.account import read_account_state
from mt5.positions import read_open_positions
from mt5.symbols import read_symbol_specification, read_tick
from persistence.orm import (
    DemoExecutionControlRecord,
    DemoExecutionRecord,
    ForwardSignalRecord,
)
from services.risk import calculate_risk_snapshot
from services.shadow_risk_gate import ShadowRiskGate

DEMO_CONTROL_KEY = "DEMO"
DEMO_EXECUTION_MODE = "DEMO"
DEMO_MAGIC_NUMBER = 3031101


def demo_execution_armed(database: Any) -> bool:
    """Return the persisted kill-switch state without creating missing state."""

    with database.session() as session:
        row = session.scalar(
            select(DemoExecutionControlRecord).where(
                DemoExecutionControlRecord.control_key == DEMO_CONTROL_KEY
            )
        )
        return bool(row and row.enabled)


def set_demo_execution_enabled(
    database: Any,
    enabled: bool,
    *,
    reason: str,
    updated_by: str,
) -> bool:
    """Arm or immediately kill new Demo orders through durable control state."""

    with database.session() as session:
        row = session.scalar(
            select(DemoExecutionControlRecord).where(
                DemoExecutionControlRecord.control_key == DEMO_CONTROL_KEY
            )
        )
        if row is None:
            row = DemoExecutionControlRecord(
                control_key=DEMO_CONTROL_KEY,
                enabled=False,
                reason="DEFAULT_KILL_SWITCH",
                updated_by="system",
            )
            session.add(row)
            session.flush()
        row.enabled = bool(enabled)
        row.reason = reason[:128]
        row.updated_by = updated_by[:64]
        return row.enabled


class DemoExecutionGateError(ValueError):
    """A required Demo execution safety invariant could not be proven."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class DemoExecutionService:
    """Submit one canonical signal at most once to an MT5 Demo account."""

    def __init__(self, settings: Settings, database: Any, gateway: Any, *, logger: logging.Logger):
        self.settings = settings
        self.database = database
        self.gateway = gateway
        self.logger = logger
        self._lock = asyncio.Lock()

    async def execute(
        self,
        *,
        signal: ForwardSignalRecord,
        decision: Any,
        snapshot: MarketSnapshot,
        intelligence_record: Any,
    ) -> DemoExecutionRecord | None:
        """Evaluate and, only when every gate passes, submit one Demo order."""

        if not self.settings.demo_execution_enabled or not demo_execution_armed(self.database):
            return None
        async with self._lock:
            existing = self._existing(signal.id)
            if existing is not None:
                return existing
            symbol = str(getattr(intelligence_record, "symbol", snapshot.symbol.name))
            try:
                self._validate_provenance(signal, intelligence_record)
                current = await self.gateway.call(
                    lambda api: self._read_current_state(api, symbol, snapshot)
                )
                current_snapshot, current_risk, symbol_info = current
                request, risk_percent = self._build_request(
                    signal, decision, current_snapshot, current_risk, symbol_info, symbol
                )
            except DemoExecutionGateError as exc:
                return self._persist_rejection(signal, intelligence_record, symbol, exc.reason)
            except Exception as exc:
                self.logger.exception("Demo execution preflight failed: %s", type(exc).__name__)
                return self._persist_rejection(
                    signal,
                    intelligence_record,
                    symbol,
                    f"PREFLIGHT_ERROR:{type(exc).__name__}",
                )

            pending = self._persist_pending(
                signal,
                intelligence_record,
                symbol,
                current_snapshot,
                request,
                risk_percent,
            )
            if pending is None:
                return self._existing(signal.id)
            if not demo_execution_armed(self.database):
                return self._update(
                    pending.id,
                    status="REJECTED",
                    rejection_reason="KILL_SWITCH_DISABLED_BEFORE_ORDER",
                )
            try:
                result = await self.gateway.call(lambda api: api.order_send(request))
            except Exception as exc:
                self.logger.exception("Demo order submission failed: %s", type(exc).__name__)
                return self._update(
                    pending.id,
                    status="INDETERMINATE",
                    rejection_reason=f"ORDER_SEND_ERROR:{type(exc).__name__}",
                    submitted_at=datetime.now(UTC),
                )
            return self._record_broker_result(pending.id, result)

    def _read_current_state(
        self, api: Any, symbol: str, snapshot: MarketSnapshot
    ) -> tuple[MarketSnapshot, Any, Any]:
        symbol_info = api.symbol_info(symbol)
        if symbol_info is None:
            raise DemoExecutionGateError("SYMBOL_STATE_UNAVAILABLE")
        current_symbol = read_symbol_specification(api, symbol)
        current_tick = read_tick(api, symbol)
        current_account = read_account_state(api)
        current_positions = read_open_positions(api, symbol)
        now = datetime.now(UTC)
        if current_account.trade_mode != 0 or current_account.trade_mode_name != "demo":
            raise DemoExecutionGateError("ACCOUNT_IS_NOT_DEMO")
        age = (now - current_tick.timestamp).total_seconds()
        if age < 0 or age > self.settings.demo_execution_max_tick_age_seconds:
            raise DemoExecutionGateError("MARKET_DATA_STALE")
        latest_m5 = snapshot.candles.get(Timeframe.M5, ())
        if not latest_m5 or latest_m5[-1].timestamp > now:
            raise DemoExecutionGateError("CANDLE_STATE_INVALID")
        current_snapshot = snapshot.model_copy(
            update={
                "account": current_account,
                "symbol": current_symbol,
                "tick": current_tick,
                "positions": current_positions,
                "generated_at": now,
            }
        )
        risk = calculate_risk_snapshot(
            current_snapshot,
            max_trade_risk_percent=self.settings.max_trade_risk_percent,
            max_aggregate_risk_percent=self.settings.max_aggregate_risk_percent,
        )
        return current_snapshot, risk, symbol_info

    def _build_request(
        self,
        signal: ForwardSignalRecord,
        decision: Any,
        snapshot: MarketSnapshot,
        risk: Any,
        symbol_info: Any,
        symbol: str,
    ) -> tuple[dict[str, Any], float]:
        direction = str(signal.decision).upper()
        if direction not in {"BUY", "SELL"} or signal.zone_id in {"", "unknown", "None"}:
            raise DemoExecutionGateError("CANONICAL_SIGNAL_INVALID")
        if getattr(decision, "decision", None) is None or str(decision.decision.value) != direction:
            raise DemoExecutionGateError("CANONICAL_DECISION_MISMATCH")
        if not all(
            value is not None
            for value in (signal.entry_price, signal.stop_loss, signal.take_profit)
        ):
            raise DemoExecutionGateError("TRADE_LEVEL_MISSING")
        if snapshot.symbol.name != symbol:
            raise DemoExecutionGateError("SYMBOL_MISMATCH")
        if snapshot.symbol.trade_mode_name not in {"full", "long_only", "short_only"}:
            raise DemoExecutionGateError("SYMBOL_TRADE_MODE_UNAVAILABLE")
        if direction == "BUY" and snapshot.symbol.trade_mode_name == "short_only":
            raise DemoExecutionGateError("BUY_NOT_ALLOWED_BY_SYMBOL")
        if direction == "SELL" and snapshot.symbol.trade_mode_name == "long_only":
            raise DemoExecutionGateError("SELL_NOT_ALLOWED_BY_SYMBOL")
        entry = snapshot.tick.ask if direction == "BUY" else snapshot.tick.bid
        point = snapshot.symbol.point
        if (
            abs(entry - float(signal.entry_price)) / point
            > self.settings.demo_execution_max_entry_deviation_points
        ):
            raise DemoExecutionGateError("ENTRY_DEVIATION_EXCEEDED")
        stop = float(signal.stop_loss)
        target = float(signal.take_profit)
        if direction == "BUY" and not (stop < entry < target):
            raise DemoExecutionGateError("SL_TP_INVALID_FOR_BUY")
        if direction == "SELL" and not (target < entry < stop):
            raise DemoExecutionGateError("SL_TP_INVALID_FOR_SELL")
        if abs(entry - stop) < snapshot.symbol.trade_tick_size:
            raise DemoExecutionGateError("SL_DISTANCE_INVALID")
        if abs(target - entry) < snapshot.symbol.trade_tick_size:
            raise DemoExecutionGateError("TP_DISTANCE_INVALID")
        gate, volume = ShadowRiskGate(
            max_trade_risk_percent=self.settings.max_trade_risk_percent,
            max_aggregate_risk_percent=self.settings.max_aggregate_risk_percent,
        ).evaluate(snapshot, risk, entry=entry, stop=stop)
        if not gate.approved or volume is None:
            raise DemoExecutionGateError(
                gate.reason_codes[0] if gate.reason_codes else "RISK_REJECTED"
            )
        api_constants = getattr(self.gateway, "api", None)
        if api_constants is None:
            raise DemoExecutionGateError("MT5_API_UNAVAILABLE")
        filling = _filling_mode(symbol_info, api_constants)
        if filling is None:
            raise DemoExecutionGateError("FILLING_MODE_UNKNOWN")
        action = getattr(api_constants, "TRADE_ACTION_DEAL", None)
        order_type = getattr(api_constants, f"ORDER_TYPE_{direction}", None)
        order_time = getattr(api_constants, "ORDER_TIME_GTC", None)
        if None in (action, order_type, order_time):
            raise DemoExecutionGateError("MT5_ORDER_CONSTANTS_UNAVAILABLE")
        comment = _order_comment(signal.signal_id)
        request = {
            "action": action,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": round(entry, snapshot.symbol.digits),
            "sl": round(stop, snapshot.symbol.digits),
            "tp": round(target, snapshot.symbol.digits),
            "deviation": self.settings.demo_execution_deviation_points,
            "magic": DEMO_MAGIC_NUMBER,
            "comment": comment,
            "type_time": order_time,
            "type_filling": filling,
        }
        return request, float(gate.proposed_risk_percent or 0.0)

    @staticmethod
    def _validate_provenance(signal: ForwardSignalRecord, intelligence_record: Any) -> None:
        if intelligence_record is None:
            raise DemoExecutionGateError("INTELLIGENCE_PROVENANCE_MISSING")
        if not getattr(intelligence_record, "candidate_id", None):
            raise DemoExecutionGateError("INTELLIGENCE_CANDIDATE_MISSING")
        if getattr(intelligence_record, "pair_zone_event_id", None) != signal.zone_id:
            raise DemoExecutionGateError("PAIR_ZONE_PROVENANCE_MISMATCH")
        if getattr(intelligence_record, "forward_session_id", None) != signal.session_id:
            raise DemoExecutionGateError("FORWARD_SESSION_MISMATCH")
        if getattr(intelligence_record, "forward_signal_id", None) != signal.id:
            raise DemoExecutionGateError("FORWARD_SIGNAL_PROVENANCE_MISSING")

    def _existing(self, signal_id: str) -> DemoExecutionRecord | None:
        with self.database.session() as session:
            return session.scalar(
                select(DemoExecutionRecord).where(
                    DemoExecutionRecord.forward_signal_id == signal_id
                )
            )

    def _persist_rejection(
        self,
        signal: ForwardSignalRecord,
        intelligence_record: Any,
        symbol: str,
        reason: str,
    ) -> DemoExecutionRecord:
        existing = self._existing(signal.id)
        if existing is not None:
            return existing
        candidate_id = str(getattr(intelligence_record, "candidate_id", "unavailable"))
        session_id = str(getattr(intelligence_record, "forward_session_id", signal.session_id))
        return self._insert_record(
            signal=signal,
            symbol=symbol,
            session_id=session_id,
            candidate_id=candidate_id,
            status="REJECTED",
            rejection_reason=reason,
            gate_reasons=(reason,),
            request={},
            volume=None,
            risk_percent=None,
        )

    def _persist_pending(
        self,
        signal: ForwardSignalRecord,
        intelligence_record: Any,
        symbol: str,
        snapshot: MarketSnapshot,
        request: dict[str, Any],
        risk_percent: float,
    ) -> DemoExecutionRecord | None:
        try:
            return self._insert_record(
                signal=signal,
                symbol=symbol,
                session_id=signal.session_id,
                candidate_id=str(intelligence_record.candidate_id),
                status="PENDING",
                rejection_reason=None,
                gate_reasons=("ALL_PRE_ORDER_GATES_PASSED",),
                request=request,
                volume=float(request["volume"]),
                risk_percent=risk_percent,
            )
        except IntegrityError:
            return self._existing(signal.id)

    def _insert_record(
        self,
        *,
        signal: ForwardSignalRecord,
        symbol: str,
        session_id: str,
        candidate_id: str,
        status: str,
        rejection_reason: str | None,
        gate_reasons: tuple[str, ...],
        request: dict[str, Any],
        volume: float | None,
        risk_percent: float | None,
    ) -> DemoExecutionRecord:
        with self.database.session() as session:
            row = DemoExecutionRecord(
                forward_signal_id=signal.id,
                forward_session_id=session_id,
                intelligence_candidate_id=candidate_id,
                pair_zone_event_id=signal.zone_id,
                symbol=symbol,
                direction=signal.decision,
                execution_mode=DEMO_EXECUTION_MODE,
                status=status,
                rejection_reason=rejection_reason,
                gate_reasons_json=list(gate_reasons),
                planned_entry=_float_or_none(signal.entry_price),
                submitted_entry=float(request["price"]) if request else None,
                stop_loss=_float_or_none(signal.stop_loss),
                take_profit=_float_or_none(signal.take_profit),
                volume=volume,
                risk_percent=risk_percent,
                request_json=request,
            )
            session.add(row)
            session.flush()
            return row

    def _update(self, record_id: str, **values: Any) -> DemoExecutionRecord:
        with self.database.session() as session:
            row = session.get(DemoExecutionRecord, record_id)
            if row is None:
                raise RuntimeError("Demo execution record disappeared")
            for key, value in values.items():
                setattr(row, key, value)
            session.flush()
            return row

    def _record_broker_result(self, record_id: str, result: Any) -> DemoExecutionRecord:
        payload = _result_payload(result)
        retcode = _int_or_none(getattr(result, "retcode", None))
        order_ticket = _positive_int_or_none(getattr(result, "order", None))
        deal_ticket = _positive_int_or_none(getattr(result, "deal", None))
        position_ticket = _positive_int_or_none(getattr(result, "position", None))
        accepted = _accepted_ret_codes(getattr(self.gateway, "api", None))
        now = datetime.now(UTC)
        if retcode not in accepted:
            return self._update(
                record_id,
                status="REJECTED",
                rejection_reason="BROKER_REJECTED",
                broker_retcode=retcode,
                result_json=payload,
                broker_order_ticket=order_ticket,
                broker_deal_ticket=deal_ticket,
                broker_position_ticket=position_ticket,
                broker_comment=str(getattr(result, "comment", ""))[:255],
                submitted_at=now,
            )
        if not any((order_ticket, deal_ticket, position_ticket)):
            return self._update(
                record_id,
                status="INDETERMINATE",
                rejection_reason="BROKER_ACCEPTANCE_WITHOUT_TICKET",
                broker_retcode=retcode,
                result_json=payload,
                submitted_at=now,
            )
        return self._update(
            record_id,
            status="ACKNOWLEDGED",
            broker_retcode=retcode,
            result_json=payload,
            broker_order_ticket=order_ticket,
            broker_deal_ticket=deal_ticket,
            broker_position_ticket=position_ticket,
            broker_comment=str(getattr(result, "comment", ""))[:255],
            submitted_at=now,
            acknowledged_at=now,
        )


def _order_comment(signal_id: str) -> str:
    return "XAUDEMO-" + sha256(signal_id.encode("utf-8")).hexdigest()[:20]


def _filling_mode(symbol_info: Any, api: Any) -> int | None:
    allowed = getattr(symbol_info, "filling_mode", None)
    if allowed is None:
        return None
    allowed = int(allowed)
    candidates = (
        getattr(api, "ORDER_FILLING_IOC", None),
        getattr(api, "ORDER_FILLING_FOK", None),
        getattr(api, "ORDER_FILLING_RETURN", None),
    )
    for candidate in candidates:
        if candidate is not None and (allowed == 0 or allowed & (1 << int(candidate))):
            return int(candidate)
    return None


def _accepted_ret_codes(api: Any) -> set[int]:
    if api is None:
        return set()
    return {
        int(value)
        for name in ("TRADE_RETCODE_DONE", "TRADE_RETCODE_DONE_PARTIAL", "TRADE_RETCODE_PLACED")
        if (value := getattr(api, name, None)) is not None
    }


def _result_payload(result: Any) -> dict[str, Any]:
    keys = (
        "retcode",
        "deal",
        "order",
        "volume",
        "price",
        "bid",
        "ask",
        "comment",
        "request_id",
        "retcode_external",
    )
    return {key: getattr(result, key, None) for key in keys if hasattr(result, key)}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _positive_int_or_none(value: Any) -> int | None:
    result = _int_or_none(value)
    return result if result and result > 0 else None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
