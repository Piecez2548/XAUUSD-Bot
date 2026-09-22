"""Reusable Telegram templates driven exclusively by typed domain events."""

from __future__ import annotations

from datetime import UTC

from domain.events import (
    AiDecisionEventPayload,
    CandleClosedPayload,
    DomainEvent,
    ErrorPayload,
    EventType,
    RiskSnapshotCreatedPayload,
    SnapshotCreatedPayload,
    SystemStatusPayload,
    TradeLifecyclePayload,
)

EVENT_TITLES: dict[EventType, str] = {
    EventType.SYSTEM_STARTED: "🟢 SYSTEM STARTED",
    EventType.SYSTEM_STOPPED: "⚪ SYSTEM STOPPED",
    EventType.MT5_CONNECTED: "🟢 MT5 CONNECTED",
    EventType.MT5_DISCONNECTED: "🔴 MT5 DISCONNECTED",
    EventType.MT5_RECONNECTED: "🟢 MT5 RECONNECTED",
    EventType.MARKET_SNAPSHOT_CREATED: "📊 MARKET SNAPSHOT",
    EventType.ACCOUNT_SNAPSHOT_CREATED: "📒 ACCOUNT SNAPSHOT",
    EventType.RISK_SNAPSHOT_CREATED: "🛡️ RISK SNAPSHOT",
    EventType.AI_DECISION_CREATED: "🤖 AI DECISION",
    EventType.SHADOW_DECISION_CREATED: "🔎 SHADOW DECISION",
    EventType.TRADE_REQUESTED: "📝 TRADE REQUESTED",
    EventType.TRADE_APPROVED: "✅ TRADE APPROVED",
    EventType.TRADE_REJECTED: "⛔ TRADE REJECTED",
    EventType.POSITION_OPENED: "🟢 POSITION OPENED",
    EventType.POSITION_ADDED: "➕ POSITION ADDED",
    EventType.POSITION_MODIFIED: "✏️ POSITION MODIFIED",
    EventType.SL_MODIFIED: "✏️ SL MODIFIED",
    EventType.TP_MODIFIED: "✏️ TP MODIFIED",
    EventType.POSITION_PARTIALLY_CLOSED: "◐ PARTIAL CLOSE",
    EventType.POSITION_CLOSED: "⚪ POSITION CLOSED",
    EventType.MANUAL_CLOSE: "⚪ MANUAL CLOSE",
    EventType.AI_CLOSE: "🤖 AI CLOSE",
    EventType.TAKE_PROFIT: "✅ TAKE PROFIT",
    EventType.STOP_LOSS: "🔴 STOP LOSS",
    EventType.TELEGRAM_SENT: "📨 TELEGRAM SENT",
    EventType.RISK_LIMIT_REJECTED: "🛡️ RISK LIMIT REJECTED",
    EventType.AI_UNAVAILABLE: "⚠️ AI ERROR",
    EventType.NEWS_SERVICE_UNAVAILABLE: "⚠️ NEWS ERROR",
    EventType.KILL_SWITCH_ACTIVATED: "🛑 KILL SWITCH",
    EventType.SYSTEM_ERROR: "🚨 SYSTEM ERROR",
    EventType.SYSTEM_LIVE_STARTED: "🟢 LIVE ENGINE STARTED",
    EventType.SYSTEM_LIVE_STOPPED: "⚪ LIVE ENGINE STOPPED",
    EventType.MT5_RECONNECTING: "🟠 MT5 RECONNECTING",
    EventType.ACCOUNT_UPDATED: "📒 ACCOUNT UPDATED",
    EventType.POSITION_OBSERVED_OPEN: "🟢 POSITION OBSERVED",
    EventType.POSITION_OBSERVED_CHANGED: "✏️ POSITION UPDATED",
    EventType.POSITION_NO_LONGER_OPEN: "⚪ POSITION NO LONGER OPEN",
    EventType.HISTORY_SYNC_COMPLETED: "🧾 HISTORY SYNC",
    EventType.CANDLE_CLOSED: "🕯️ CANDLE CLOSED",
    EventType.DATA_STALE: "⚠️ DATA STALE",
    EventType.POSITION_WITHOUT_STOP_LOSS: "⚠️ POSITION WITHOUT STOP",
    EventType.RISK_UNBOUNDED: "⚠️ RISK UNBOUNDED",
    EventType.RISK_BACK_WITHIN_BOUNDS: "✅ RISK BOUNDED",
    EventType.HISTORY_SYNC_FAILED: "⚠️ HISTORY SYNC FAILED",
    EventType.HISTORY_SYNC_RECOVERED: "✅ HISTORY SYNC RECOVERED",
}


def _value(label: str, value: object | None) -> str | None:
    return f"{label}: {value}" if value is not None else None


def format_telegram_event(event: DomainEvent) -> str:
    title = EVENT_TITLES.get(event.event_type, event.event_type.value)
    lines: list[str | None] = [title, ""]
    payload = event.payload
    if isinstance(payload, TradeLifecyclePayload):
        lines.extend(
            [
                " ".join(item for item in (payload.symbol, payload.direction) if item),
                "",
                _value("Entry/Price", payload.price),
                _value("Lot", payload.volume),
                _value("SL", payload.stop_loss),
                _value("TP", payload.take_profit),
                _value(
                    "Risk",
                    (f"{payload.risk_percent:.2f}%" if payload.risk_percent is not None else None),
                ),
                _value("Risk Amount", payload.risk_amount),
                _value("Planned RR", payload.planned_rr),
                _value("Net P&L", payload.pnl),
                _value("Result R", payload.realized_r),
                _value("Reason", payload.reason),
                _value("Ticket", f"#{payload.broker_ticket}" if payload.broker_ticket else None),
            ]
        )
    elif isinstance(payload, SnapshotCreatedPayload):
        lines.extend(
            [
                _value("Symbol", payload.symbol),
                _value("Open positions", payload.positions_count),
                _value("Snapshot", payload.snapshot_id),
            ]
        )
    elif isinstance(payload, RiskSnapshotCreatedPayload):
        lines.extend(
            [
                _value(
                    "Open risk",
                    f"{payload.open_risk_percent:.2f}%"
                    if payload.open_risk_percent is not None
                    else "unavailable",
                ),
                _value(
                    "Remaining budget",
                    f"{payload.remaining_risk_percent:.2f}%"
                    if payload.remaining_risk_percent is not None
                    else "unavailable",
                ),
                _value("Positions without bounded risk", payload.unbounded_positions),
            ]
        )
    elif isinstance(payload, AiDecisionEventPayload):
        lines.extend(
            [
                (
                    "SHADOW ONLY — NO ORDER SENT"
                    if event.event_type == EventType.SHADOW_DECISION_CREATED
                    else None
                ),
                f"{payload.symbol} {payload.action}",
                _value(
                    "Confidence",
                    f"{payload.confidence * 100:.0f}%" if payload.confidence is not None else None,
                ),
                _value("Validation", payload.validation_status),
                _value("Execution", payload.execution_status),
            ]
        )
    elif isinstance(payload, CandleClosedPayload):
        lines.extend(
            [
                f"{payload.symbol} {payload.timeframe}",
                _value("Open time", payload.open_time.astimezone(UTC).isoformat()),
                _value("Close", payload.close),
                _value("High", payload.high),
                _value("Low", payload.low),
                _value("Volume", payload.tick_volume),
            ]
        )
    elif isinstance(payload, ErrorPayload):
        lines.extend(
            [
                _value("Component", payload.component),
                _value("Code", payload.error_code),
                _value("Message", payload.message),
                _value("Recoverable", "yes" if payload.recoverable else "no"),
            ]
        )
    elif isinstance(payload, SystemStatusPayload):
        lines.extend(
            [
                _value("Component", payload.component),
                _value("Status", payload.status),
                payload.message,
            ]
        )

    lines.extend(
        [
            "",
            f"Time: {event.timestamp.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"Source: {event.source}",
            f"Event: {event.event_id}",
        ]
    )
    return "\n".join(line for line in lines if line is not None)


def telegram_test_message() -> str:
    return "XAUUSD AI Trader\nTelegram connection successful.\n\nREAD-ONLY MODE"
