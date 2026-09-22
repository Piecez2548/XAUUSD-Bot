"""Authenticated Telegram control plane for local monitoring infrastructure."""
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, perf_counter
from uuid import uuid4

import httpx
from sqlalchemy import desc, func, select

from config.settings import Settings
from persistence.database import Database
from persistence.migrations import migrate_database
from persistence.orm import (
    AccountSnapshotRecord,
    CandleRecord,
    MarketSnapshotRecord,
    PositionRecord,
    PositionSnapshotRecord,
    RiskSnapshotRecord,
    ShadowDecisionRecord,
    ShadowOutcomeRecord,
    SymbolRecord,
    SystemEventRecord,
    SystemHealthRecord,
)
from persistence.repositories import ControlAuditRepository, SystemHealthRepository
from services.shadow_outcome import OUTCOME_POLICY_VERSION, performance_summary
from services.supervisor import ProcessSupervisor
from services.worker_health import derive_worker_state

COMMAND_HELP = {
    "/start": "Start monitoring infrastructure",
    "/stop": "Stop monitoring infrastructure",
    "/restart": "Restart monitoring infrastructure",
    "/status": "Show system status",
    "/health": "Show component health",
    "/account": "Show verified account state",
    "/market": "Show broker market state",
    "/positions": "Show observed positions",
    "/risk": "Show current risk state",
    "/decision": "Show latest shadow decision",
    "/shadow": "Show shadow decision summary",
    "/shadowhealth": "Show shadow worker liveness",
    "/outcome": "Show recent shadow outcomes",
    "/performance": "Show shadow performance summary",
    "/outcomehealth": "Show shadow outcome worker liveness",
    "/strategy": "Show deterministic baseline rules",
    "/strategies": "Show registered shadow strategies",
    "/research": "Show active strategy research identity",
    "/logs": "Show safe recent operational events",
    "/help": "Show available commands",
}


class TelegramControlService:
    """Long-poll Telegram commands without a remote-shell escape hatch."""

    def __init__(
        self,
        settings: Settings,
        project_root: Path,
        *,
        logger: logging.Logger | None = None,
        supervisor: ProcessSupervisor | None = None,
        database: Database | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.project_root = project_root
        self.logger = logger or logging.getLogger(__name__)
        self.supervisor = supervisor or ProcessSupervisor(
            project_root,
            max_restarts=settings.supervisor_max_restarts,
            restart_window_seconds=settings.supervisor_restart_window_seconds,
        )
        self.database = database or Database(settings.database_url, project_root=project_root)
        self.audit = ControlAuditRepository(self.database)
        self.health = SystemHealthRepository(self.database)
        self._client = client
        self._owns_client = client is None
        self._operation_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._last_command_at: dict[tuple[str, str], datetime] = {}
        self._process_notifications: set[tuple[str, int, str]] = set()
        self._offset_path = project_root / "data" / "telegram_update_offset.json"
        self._offset = self._load_offset()

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.telegram_control_enabled
            and self.settings.telegram_bot_token
            and self.settings.telegram_allowed_chat_ids
            and self.settings.telegram_allowed_user_ids
        )

    async def run_forever(self) -> None:
        if not self.configured:
            self.logger.warning(
                "Telegram control service disabled or deny-by-default configuration incomplete"
            )
            return
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                self.settings.telegram_control_poll_seconds + 10,
                connect=self.settings.telegram_timeout_seconds,
            )
        )
        try:
            await self._set_command_menu(client)
            while not self._stop.is_set():
                try:
                    for record in self.supervisor.monitor_once(self._supervised_commands()):
                        if record.restart_count > 0 or record.state == "ERROR":
                            key = (record.component, record.restart_count, record.state)
                            if key not in self._process_notifications:
                                self._process_notifications.add(key)
                                await self.send_message(
                                    self.settings.telegram_chat_id
                                    or self.settings.telegram_allowed_chat_ids[0],
                                    f"PROCESS_CRASHED\nComponent: {record.component}\n"
                                    f"State: {record.state}\nRestart count: {record.restart_count}",
                                )
                    updates = await self._get_updates(client)
                    for update in updates:
                        response = await self.handle_update(update)
                        message = update.get("message")
                        if response and isinstance(message, dict):
                            chat = message.get("chat")
                            if isinstance(chat, dict) and chat.get("id") is not None:
                                await self.send_message(str(chat["id"]), response)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("Telegram control polling failed")
                    await asyncio.sleep(min(30.0, self.settings.telegram_control_poll_seconds * 2))
        finally:
            if self._owns_client:
                await client.aclose()
            if self._owns_client:
                self.database.dispose()

    async def stop(self) -> None:
        self._stop.set()
        # A control-process shutdown owns the supervised children.  Stop only
        # identities verified by ProcessSupervisor; unverified PIDs remain
        # untouched and are reported as degraded.
        for component in ("live", "api"):
            try:
                self.supervisor.stop_component(component)
            except Exception:
                self.logger.exception("Failed to stop supervised %s process", component)

    async def handle_update(self, update: dict[str, object]) -> str | None:
        update_id = _int(update.get("update_id"))
        if update_id is not None and update_id < self._offset:
            return None
        message = update.get("message")
        if not isinstance(message, dict):
            self._advance_offset(update_id)
            return None
        chat = message.get("chat")
        actor = message.get("from")
        text = message.get("text")
        if not isinstance(chat, dict) or not isinstance(actor, dict) or not isinstance(text, str):
            self._advance_offset(update_id)
            return None
        chat_id = str(chat.get("id", ""))
        user_id = str(actor.get("id", ""))
        chat_type = str(chat.get("type", ""))
        command = text.strip().split(maxsplit=1)[0].casefold() if text.strip() else ""
        if command not in COMMAND_HELP:
            self._advance_offset(update_id)
            return None
        correlation_id = str(uuid4())
        authorized = self._authorized(chat_id, user_id, chat_type)
        if not authorized:
            self._audit(command, chat_id, user_id, False, "UNAUTHORIZED", correlation_id)
            self._advance_offset(update_id)
            return "Unauthorized control identity."
        if not self._rate_allowed(user_id, command):
            self._audit(command, chat_id, user_id, True, "RATE_LIMITED", correlation_id)
            self._advance_offset(update_id)
            return "Command rate limited; please retry shortly."
        try:
            response = await self._dispatch(command)
            result = "SUCCEEDED"
        except Exception:
            self.logger.exception("Telegram control command failed: %s", command)
            response = "Operation failed; inspect /health for verified state."
            result = "FAILED"
        self._audit(command, chat_id, user_id, True, result, correlation_id)
        self._advance_offset(update_id)
        return response

    def _authorized(self, chat_id: str, user_id: str, chat_type: str) -> bool:
        return (
            self.configured
            and chat_type == "private"
            and chat_id in set(self.settings.telegram_allowed_chat_ids)
            and user_id in set(self.settings.telegram_allowed_user_ids)
        )

    def _rate_allowed(self, user_id: str, command: str) -> bool:
        now = datetime.now(UTC)
        key = (user_id, command)
        previous = self._last_command_at.get(key)
        if previous and now - previous < timedelta(
            seconds=self.settings.telegram_control_rate_limit_seconds
        ):
            return False
        self._last_command_at[key] = now
        return True

    async def _dispatch(self, command: str) -> str:
        if command == "/start":
            return await self._start_infrastructure()
        if command == "/stop":
            return await self._stop_infrastructure()
        if command == "/restart":
            async with self._operation_lock:
                await self._stop_infrastructure_locked()
                return await self._start_infrastructure_locked()
        handlers = {
            "/status": self._status,
            "/health": self._health,
            "/account": self._account,
            "/market": self._market,
            "/positions": self._positions,
            "/risk": self._risk,
            "/decision": self._decision,
            "/shadow": self._shadow,
            "/shadowhealth": self._shadowhealth,
            "/outcome": self._outcome,
            "/performance": self._performance,
            "/outcomehealth": self._outcomehealth,
            "/strategy": self._strategy,
            "/strategies": self._strategies,
            "/research": self._research,
            "/logs": self._logs,
            "/help": self._help,
        }
        return handlers[command]()

    async def _start_infrastructure(self) -> str:
        async with self._operation_lock:
            return await self._start_infrastructure_locked()

    async def _start_infrastructure_locked(self) -> str:
        migrate_database(self.settings.database_url, self.project_root)
        api_command = [sys.executable, str(self.project_root / "main.py"), "server"]
        live_command = [sys.executable, str(self.project_root / "main.py"), "live"]
        self.supervisor.start_component("api", api_command)
        self.supervisor.start_component("live", live_command)
        verification = await self._wait_for_startup_verification()
        if not verification["complete"]:
            headline = "🟠 START INCOMPLETE"
        else:
            headline = "🟢 MONITORING STARTED"
        checks = " ".join(
            f"{name.upper()}={value}" for name, value in verification["checks"].items()
        )
        return (
            f"{headline}\nVerification: {checks}\n\n{self._status()}\n\n"
            "Execution  DISABLED\nMode: READ ONLY"
        )

    async def _stop_infrastructure(self) -> str:
        async with self._operation_lock:
            return await self._stop_infrastructure_locked()

    async def _stop_infrastructure_locked(self) -> str:
        live = self.supervisor.stop_component("live")
        api = self.supervisor.stop_component("api")
        if live.state != "STOPPED" or api.state != "STOPPED":
            return (
                "🟠 MONITORING STOP INCOMPLETE\n\n"
                f"Live Engine  {live.state}\nAPI           {api.state}\n"
                "Broker positions were NOT modified.\n"
                "Telegram Control  ONLINE\nExecution         DISABLED"
            )
        return (
            "🔴 MONITORING STOPPED\n\n"
            "Live Engine  STOPPED\nAPI           STOPPED\n\n"
            "Broker positions were NOT modified.\n"
            "Telegram Control  ONLINE\nExecution         DISABLED"
        )

    def _status(self) -> str:
        records = self.supervisor.status()
        live_process = records.get("live")
        runtime = self._latest_service_state("live_runtime")
        live_state = (
            "RUNNING"
            if live_process is not None
            and live_process.state == "RUNNING"
            and runtime == "CONNECTED"
            else runtime
            if live_process is not None and live_process.state == "RUNNING"
            else live_process.state
            if live_process is not None
            else "STOPPED"
        )
        return "\n".join(
            [
                "SYSTEM STATUS",
                "Supervisor    HEALTHY",
                f"Live Engine   {live_state}",
                f"API           {records.get('api').state if records.get('api') else 'STOPPED'}",
                f"MT5           {self._latest_service_state('mt5')}",
                f"Database      {'CONNECTED' if self.database.healthcheck() else 'DISCONNECTED'}",
                f"Telegram      {self._telegram_state()}",
                "Execution     DISABLED",
                "Mode: READ ONLY",
            ]
        )

    async def _wait_for_startup_verification(self) -> dict[str, object]:
        deadline = monotonic() + self.settings.supervisor_operation_timeout_seconds
        checks: dict[str, str] = {}
        while True:
            records = self.supervisor.status()
            api_process = records.get("api")
            live_process = records.get("live")
            api_ok = (
                api_process is not None
                and api_process.state == "RUNNING"
                and await self._api_responsive()
            )
            runtime = self._latest_service_state("live_runtime")
            mt5 = self._latest_service_state("mt5")
            _risk, _observed, freshness, _rows, snapshot_id = self._snapshot_context()
            checks = {
                "api": "OK" if api_ok else "WAITING",
                "live_runtime": "CONNECTED" if runtime == "CONNECTED" else runtime,
                "mt5": mt5,
                "snapshot": "COMPLETE" if snapshot_id and freshness == "LIVE" else freshness,
                "database": "CONNECTED" if self.database.healthcheck() else "DISCONNECTED",
            }
            complete = (
                all(value in {"OK", "CONNECTED", "COMPLETE"} for value in checks.values())
                and live_process is not None
                and live_process.state == "RUNNING"
            )
            if complete or monotonic() >= deadline:
                return {"complete": complete, "checks": checks}
            await asyncio.sleep(0.25)

    async def _api_responsive(self) -> bool:
        host = self.settings.api_host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                response = await client.get(f"http://{host}:{self.settings.api_port}/api/health")
            return response.is_success and response.json().get("read_only") is True
        except (httpx.HTTPError, ValueError):
            return False

    def _health(self) -> str:
        statuses = self._latest_health()
        statuses["live_runtime"] = self._latest_service_state("live_runtime")
        statuses["mt5"] = self._latest_service_state("mt5")
        statuses["worker:history"] = self._history_worker_state()
        statuses["worker:shadow"] = self._shadow_worker_state()
        statuses["worker:shadow_outcome"] = self._shadow_outcome_worker_state()
        lines = ["SYSTEM HEALTH"]
        for name, default in (
            ("supervisor", "HEALTHY"),
            ("live_runtime", "UNKNOWN"),
            ("mt5", "UNKNOWN"),
            ("database", "CONNECTED" if self.database.healthcheck() else "DISCONNECTED"),
            ("telegram", self._telegram_state()),
            ("worker:history", "UNKNOWN"),
            ("worker:shadow", "UNKNOWN"),
            ("worker:shadow_outcome", "UNKNOWN"),
        ):
            label = name.replace('_', ' ').title()
            label_width = max(16, len(label) + 2)
            lines.append(f"{label:<{label_width}}{statuses.get(name, default)}")
        return "\n".join(lines)

    def _history_worker_state(self) -> str:
        with self.database.session() as session:
            row = session.scalar(
                select(SystemHealthRecord)
                .where(SystemHealthRecord.component == "worker:history")
                .order_by(desc(SystemHealthRecord.timestamp))
                .limit(1)
            )
        return derive_worker_state(
            status=row.status if row else None,
            timestamp=row.timestamp if row else None,
            interval_seconds=self.settings.live_history_interval_seconds,
        )

    def _shadow_health_row(self):
        with self.database.session() as session:
            return session.scalar(
                select(SystemHealthRecord)
                .where(SystemHealthRecord.component == "worker:shadow")
                .order_by(desc(SystemHealthRecord.timestamp), desc(SystemHealthRecord.id))
                .limit(1)
            )

    def _shadow_worker_state(self) -> str:
        row = self._shadow_health_row()
        return derive_worker_state(
            status=row.status if row else None,
            timestamp=row.timestamp if row else None,
            interval_seconds=max(15.0, self.settings.live_account_interval_seconds * 4),
        )

    def _shadow_outcome_worker_state(self) -> str:
        with self.database.session() as session:
            row = session.scalar(
                select(SystemHealthRecord)
                .where(SystemHealthRecord.component == "worker:shadow_outcome")
                .order_by(desc(SystemHealthRecord.timestamp), desc(SystemHealthRecord.id))
                .limit(1)
            )
        return derive_worker_state(
            status=row.status if row else None,
            timestamp=row.timestamp if row else None,
            interval_seconds=max(15.0, self.settings.live_candle_interval_seconds * 4),
        )

    def _shadowhealth(self) -> str:
        row = self._shadow_health_row()
        metadata = row.metadata_json if row and row.metadata_json else {}
        with self.database.session() as session:
            latest_m5 = session.scalar(
                select(CandleRecord.timestamp)
                .where(CandleRecord.timeframe == "M5")
                .order_by(desc(CandleRecord.timestamp), desc(CandleRecord.id))
                .limit(1)
            )
        latest_available = metadata.get("latest_available_m5")
        latest_received = metadata.get("latest_received_m5") or metadata.get(
            "last_received_candle_at"
        )
        latest_processed = metadata.get("latest_processed_m5") or metadata.get(
            "last_processed_candle_at"
        )
        latest_decision = metadata.get("latest_decision_m5") or metadata.get("last_decision_at")
        last_failure = metadata.get("last_failure") or metadata.get("error_category") or "NONE"
        latest_persisted_text = (
            _format_observed_text(latest_available)
            if latest_available
            else _format_observed(latest_m5)
        )
        return "\n".join(
            [
                "SHADOW ENGINE HEALTH",
                f"State: {self._shadow_worker_state()}",
                f"Latest persisted M5: {latest_persisted_text}",
                f"Latest received M5: {_format_observed_text(latest_received)}",
                f"Latest processed M5: {_format_observed_text(latest_processed)}",
                f"Latest decision M5: {_format_observed_text(latest_decision)}",
                f"Processing lag: {metadata.get('processing_lag_seconds', 'UNKNOWN')}",
                f"Queue: {metadata.get('queue_depth', 0)}/{metadata.get('queue_capacity', 8)}",
                f"Deferred: {metadata.get('deferred_count', 0)}",
                f"Catch-up pending: {metadata.get('catchup_pending_count', 0)}",
                f"Total backlog: {metadata.get('total_backlog', metadata.get('queue_depth', 0))}",
                f"Last failure: {last_failure}",
                "Execution: DISABLED",
            ]
        )

    def _account(self) -> str:
        with self.database.session() as session:
            row = session.scalar(
                select(AccountSnapshotRecord)
                .order_by(desc(AccountSnapshotRecord.timestamp))
                .limit(1)
            )
        if row is None:
            return "ACCOUNT\nStatus: UNKNOWN (no verified account state)"
        return (
            "ACCOUNT\n"
            f"Balance: {row.balance:.2f}\nEquity: {row.equity:.2f}\n"
            f"Floating P/L: {row.profit:.2f}\nMargin: {row.margin:.2f}\n"
            f"Free Margin: {row.free_margin:.2f}\nMargin Level: {row.margin_level:.2f}\n"
            f"Timestamp: {row.timestamp.isoformat()} UTC"
        )

    def _market(self) -> str:
        with self.database.session() as session:
            row = session.execute(
                select(MarketSnapshotRecord, SymbolRecord)
                .join(SymbolRecord, SymbolRecord.id == MarketSnapshotRecord.symbol_id)
                .order_by(desc(MarketSnapshotRecord.timestamp))
                .limit(1)
            ).first()
        if row is None:
            return "MARKET\nStatus: UNKNOWN (no verified market state)"
        snapshot, symbol = row
        return (
            f"MARKET\nSymbol: {symbol.name}\nBid: {snapshot.bid}\nAsk: {snapshot.ask}\n"
            f"Spread: {snapshot.spread}\nTimestamp: {snapshot.timestamp.isoformat()} UTC"
        )

    def _positions(self) -> str:
        _risk_row, observed_at, freshness, position_rows, snapshot_id = self._snapshot_context()
        count = str(len(position_rows)) if freshness != "STATE_SYNC_PENDING" else "UNKNOWN"
        lines = [
            "POSITIONS",
            f"Open positions: {count}",
            f"Observed: {_format_observed(observed_at)}",
            f"Freshness: {freshness}",
            f"Snapshot: {snapshot_id or 'UNKNOWN'}",
        ]
        if freshness == "STATE_SYNC_PENDING":
            return "\n".join(lines + ["Current position/risk snapshots are not coherent yet."])
        for record, latest in position_rows:
            if latest:
                risk = "UNBOUNDED" if latest.stop_loss <= 0 else "BOUNDED"
                lines.append(
                    f"#{record.broker_ticket} {record.direction} volume={latest.volume} "
                    f"entry={latest.open_price} current={latest.current_price} "
                    f"SL={latest.stop_loss or 'UNAVAILABLE'} "
                    f"P/L={latest.profit + latest.swap:.2f} Risk={risk}"
                )
        if not position_rows:
            lines.append("No open positions.")
        return "\n".join(lines)

    def _risk(self) -> str:
        row, observed_at, freshness, _position_rows, snapshot_id = self._snapshot_context()
        if row is None:
            return "RISK\nStatus: UNKNOWN (no verified risk state)"
        state = (
            "UNBOUNDED"
            if row.unbounded_positions_count
            else (
                "LIMIT EXCEEDED"
                if row.open_risk_percent is not None
                and row.open_risk_percent >= row.max_aggregate_risk_percent
                else "WITHIN LIMIT"
            )
        )
        known_risk = _format_percent(row.open_risk_percent)
        remaining = _format_percent(row.remaining_risk_percent)
        if freshness == "STATE_SYNC_PENDING":
            state = "STATE_SYNC_PENDING"
        bounded_count = (
            "UNKNOWN"
            if freshness == "STATE_SYNC_PENDING"
            else str(row.open_positions_count - row.unbounded_positions_count)
        )
        unbounded_count = (
            "UNKNOWN" if freshness == "STATE_SYNC_PENDING" else str(row.unbounded_positions_count)
        )
        return (
            "RISK\nPer-trade maximum: 2%\nAggregate hard maximum: 6%\n"
            f"Known bounded risk: {known_risk}\nRemaining budget: {remaining}\n"
            f"Bounded positions: {bounded_count}\n"
            f"Unbounded positions: {unbounded_count}\nRisk state: {state}\n"
            f"Observed: {_format_observed(observed_at)}\nFreshness: {freshness}\n"
            f"Snapshot: {snapshot_id or 'UNKNOWN'}"
        )

    def _decision(self) -> str:
        with self.database.session() as session:
            row = session.scalar(
                select(ShadowDecisionRecord)
                .order_by(
                    desc(ShadowDecisionRecord.m5_candle_timestamp),
                    desc(ShadowDecisionRecord.created_at),
                )
                .limit(1)
            )
        if row is None:
            return "LATEST SHADOW DECISION\nStatus: UNKNOWN (no completed decision)"
        lines = [
            "LATEST SHADOW DECISION",
            f"Decision: {row.decision}",
            f"Regime: {row.market_regime}",
            f"Strategy: {row.strategy_name} {row.strategy_version}",
            f"Snapshot: {row.market_snapshot_id or 'UNKNOWN'}",
            f"Candle: {row.m5_candle_timestamp.isoformat()} UTC",
            f"Reason: {row.human_readable_reason}",
        ]
        if row.decision in {"BUY", "SELL"}:
            lines.extend(
                [
                    f"Entry: {row.entry_price}",
                    f"SL: {row.stop_loss}",
                    f"TP: {row.take_profit}",
                    f"RR: {row.risk_reward_ratio}",
                    f"Shadow lot: {row.hypothetical_volume}",
                    f"Risk: {row.approved_risk_percent}%",
                ]
            )
        lines.extend(
            [
                f"Risk gate: {row.risk_gate_state}",
                "Execution: DISABLED",
                "SHADOW ONLY — NO ORDER SENT",
            ]
        )
        return "\n".join(lines)

    def _shadow(self) -> str:
        with self.database.session() as session:
            rows = session.execute(
                select(ShadowDecisionRecord.decision, func.count()).group_by(
                    ShadowDecisionRecord.decision
                )
            ).all()
            pending = (
                session.scalar(
                    select(func.count())
                    .select_from(ShadowDecisionRecord)
                    .where(ShadowDecisionRecord.outcome_status == "PENDING")
                )
                or 0
            )
        counts = {decision: int(count) for decision, count in rows}
        return (
            "SHADOW SUMMARY\n"
            f"Total decisions: {sum(counts.values())}\n"
            f"BUY: {counts.get('BUY', 0)}\nSELL: {counts.get('SELL', 0)}\n"
            f"NO_TRADE: {counts.get('NO_TRADE', 0)}\n"
            f"Pending outcomes: {pending}\nExecution: DISABLED"
        )

    def _outcome(self) -> str:
        with self.database.session() as session:
            rows = session.scalars(
                select(ShadowOutcomeRecord)
                .where(ShadowOutcomeRecord.evaluation_policy_version == OUTCOME_POLICY_VERSION)
                .order_by(
                    desc(ShadowOutcomeRecord.decision_m5_timestamp),
                    desc(ShadowOutcomeRecord.created_at),
                )
                .limit(5)
            ).all()
        if not rows:
            return "SHADOW OUTCOMES\nNo evaluated shadow outcomes yet.\nExecution: DISABLED"
        lines = ["SHADOW OUTCOMES"]
        for row in rows:
            value = "UNKNOWN" if row.realized_r is None else f"{row.realized_r:.3f}R"
            lines.append(
                f"{row.decision_m5_timestamp.isoformat()} {row.side} {row.terminal_status} {value}"
            )
        lines.append("Execution: DISABLED")
        return "\n".join(lines)

    def _performance(self) -> str:
        report = performance_summary(self.database, policy_version=OUTCOME_POLICY_VERSION)

        def fmt(value):
            return (
                "UNKNOWN"
                if value is None
                else f"{value:.3f}"
                if isinstance(value, float)
                else str(value)
            )

        return (
            "SHADOW PERFORMANCE\n"
            f"Eligible: {report['eligible_trades']} Resolved: {report['resolved_sample_size']}\n"
            f"TP: {report['tp_hits']} SL: {report['sl_hits']} Ambiguous: {report['ambiguous']} Expired: {report['expired']}\n"
            f"Win rate: {fmt(report['win_rate'])} Average R: {fmt(report['average_r'])} Total R: {fmt(report['total_r'])}\n"
            "Execution: DISABLED"
        )

    def _outcomehealth(self) -> str:
        with self.database.session() as session:
            row = session.scalar(
                select(SystemHealthRecord)
                .where(SystemHealthRecord.component == "worker:shadow_outcome")
                .order_by(desc(SystemHealthRecord.timestamp), desc(SystemHealthRecord.id))
                .limit(1)
            )
        state = derive_worker_state(
            status=row.status if row else None,
            timestamp=row.timestamp if row else None,
            interval_seconds=max(15.0, self.settings.live_candle_interval_seconds * 4),
        )
        return f"SHADOW OUTCOME HEALTH\nState: {state}\nObserved: {row.timestamp.isoformat() if row else 'UNKNOWN'}\nExecution: DISABLED"

    def _strategy(self) -> str:
        return (
            f"SHADOW STRATEGY {self.settings.shadow_strategy}\n"
            "H4/H1: directional bias from EMA, slope, and higher/lower structure.\n"
            "M15: setup must align with the higher-timeframe bias.\n"
            "M5: timing must align; stop uses recent structure plus ATR buffer.\n"
            "Target: minimum configurable R:R (default 2.0).\n"
            "Risk gate: max 2% per shadow trade and 6% aggregate.\n"
            "Execution: DISABLED — SHADOW ONLY"
        )

    def _strategies(self) -> str:
        from services.strategy_platform import StrategyRegistry

        registry = StrategyRegistry(self.settings)
        return "REGISTERED SHADOW STRATEGIES\n" + "\n".join(
            f"{identifier}{' (ACTIVE)' if identifier == self.settings.shadow_strategy else ''}"
            for identifier in registry.identifiers()
        ) + "\nExecution: DISABLED"

    def _research(self) -> str:
        from services.strategy_platform import StrategyRegistry

        strategy = StrategyRegistry(self.settings).resolve(self.settings.shadow_strategy)
        return (
            "ACTIVE SHADOW STRATEGY\n"
            f"{strategy.metadata.strategy_id}\n"
            f"Version: {strategy.metadata.strategy_version}\n"
            f"Config: {strategy.metadata.config_hash}\n"
            "Activation policy: next closed M5 boundary\n"
            "Execution: DISABLED"
        )

    def _snapshot_context(self):
        """Return the latest coherent risk/position view from one persisted cycle."""

        with self.database.session() as session:
            candidates = session.execute(
                select(RiskSnapshotRecord, MarketSnapshotRecord)
                .join(
                    MarketSnapshotRecord,
                    RiskSnapshotRecord.market_snapshot_id == MarketSnapshotRecord.id,
                )
                .where(MarketSnapshotRecord.positions_observed_successfully.is_(True))
                .order_by(desc(RiskSnapshotRecord.timestamp))
            ).all()
            latest_risk_any = session.scalar(
                select(RiskSnapshotRecord).order_by(desc(RiskSnapshotRecord.timestamp)).limit(1)
            )
            latest_market_any = session.scalar(
                select(MarketSnapshotRecord).order_by(desc(MarketSnapshotRecord.timestamp)).limit(1)
            )
            for risk, market in candidates:
                snapshot_count = (
                    session.scalar(
                        select(func.count())
                        .select_from(PositionSnapshotRecord)
                        .where(PositionSnapshotRecord.market_snapshot_id == market.id)
                    )
                    or 0
                )
                if (
                    risk.open_positions_count != market.open_position_count
                    or snapshot_count != market.open_position_count
                ):
                    continue
                if (latest_risk_any is not None and latest_risk_any.timestamp > risk.timestamp) or (
                    latest_market_any is not None and latest_market_any.timestamp > market.timestamp
                ):
                    continue
                rows = session.execute(
                    select(PositionRecord, PositionSnapshotRecord)
                    .join(
                        PositionSnapshotRecord,
                        PositionSnapshotRecord.position_id == PositionRecord.id,
                    )
                    .where(PositionSnapshotRecord.market_snapshot_id == market.id)
                    .order_by(PositionRecord.first_seen_at)
                ).all()
                observed_at = risk.timestamp
                freshness = _freshness_state(
                    observed_at,
                    max_age_seconds=self.settings.data_stale_position_seconds,
                )
                return risk, observed_at, freshness, tuple(rows), market.id

            risk = session.scalar(
                select(RiskSnapshotRecord).order_by(desc(RiskSnapshotRecord.timestamp)).limit(1)
            )
            market = session.scalar(
                select(MarketSnapshotRecord).order_by(desc(MarketSnapshotRecord.timestamp)).limit(1)
            )
            active = session.scalars(
                select(PositionRecord)
                .where(PositionRecord.closed_observed_at.is_(None))
                .order_by(PositionRecord.first_seen_at)
            ).all()
            rows = []
            for record in active:
                latest = session.scalar(
                    select(PositionSnapshotRecord)
                    .where(PositionSnapshotRecord.position_id == record.id)
                    .order_by(desc(PositionSnapshotRecord.timestamp))
                    .limit(1)
                )
                rows.append((record, latest))
            observed_at = (
                risk.timestamp if risk is not None else market.timestamp if market else None
            )
            freshness = _freshness_state(
                observed_at,
                max_age_seconds=self.settings.data_stale_position_seconds,
            )
            if risk is not None or market is not None:
                freshness = "STATE_SYNC_PENDING"
            return risk, observed_at, freshness, tuple(rows), None

    def _logs(self) -> str:
        with self.database.session() as session:
            rows = session.scalars(
                select(SystemEventRecord).order_by(desc(SystemEventRecord.timestamp)).limit(8)
            ).all()
        lines = ["RECENT SAFE EVENTS"]
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            detail = payload.get("message") or payload.get("status") or "recorded"
            lines.append(f"{row.timestamp.isoformat()} {row.event_type}: {str(detail)[:120]}")
        return "\n".join(lines)

    @staticmethod
    def _help() -> str:
        return "COMMANDS\n" + "\n".join(f"{key} — {value}" for key, value in COMMAND_HELP.items())

    def _latest_service_state(self, component: str) -> str:
        with self.database.session() as session:
            if component == "mt5":
                health_row = session.scalar(
                    select(SystemHealthRecord)
                    .where(SystemHealthRecord.component == "mt5")
                    .order_by(desc(SystemHealthRecord.timestamp))
                    .limit(1)
                )
                if health_row is not None:
                    threshold = max(30.0, self.settings.live_account_interval_seconds * 4)
                    if not _is_stale(
                        health_row.timestamp,
                        max_age=timedelta(seconds=threshold),
                    ):
                        return health_row.status
                    # A stale persisted health row is evidence that current
                    # verification has stopped; an old CONNECTED event must
                    # not make the broker look healthy again.
                    return "DEGRADED" if health_row.status not in {"STOPPED"} else "STOPPED"
                # Domain events are historical notifications, not a current
                # connection proof. A new live read must create the health row.
                return "UNKNOWN"
            row = session.scalar(
                select(SystemHealthRecord)
                .where(SystemHealthRecord.component == component)
                .order_by(desc(SystemHealthRecord.timestamp))
                .limit(1)
            )
        if row is None:
            return "UNKNOWN"
        if component == "live_runtime":
            threshold = max(30.0, self.settings.live_account_interval_seconds * 4)
            if _is_stale(row.timestamp, max_age=timedelta(seconds=threshold)):
                return "DEGRADED" if row.status != "STOPPED" else "STOPPED"
        return row.status

    def _telegram_state(self) -> str:
        if not self.settings.telegram_enabled:
            return "DISABLED"
        if not (
            self.settings.telegram_bot_token
            and self.settings.telegram_bot_token.strip()
            and self.settings.telegram_chat_id
            and self.settings.telegram_chat_id.strip()
        ):
            return "ERROR"
        with self.database.session() as session:
            row = session.scalar(
                select(SystemHealthRecord)
                .where(SystemHealthRecord.component == "telegram")
                .order_by(desc(SystemHealthRecord.timestamp))
                .limit(1)
            )
        if row is not None:
            return row.status
        return "UNKNOWN"

    def _latest_health(self) -> dict[str, str]:
        with self.database.session() as session:
            rows = SystemHealthRepository.latest_records(session)
        result: dict[str, str] = {}
        for row in rows:
            result.setdefault(row.component, row.status)
        return result

    def _audit(
        self,
        command: str,
        chat_id: str,
        user_id: str,
        authorized: bool,
        result: str,
        correlation_id: str,
    ) -> None:
        try:
            self.audit.record(
                command=command,
                chat_id=chat_id,
                user_id=user_id,
                authorized=authorized,
                result=result,
                correlation_id=correlation_id,
            )
        except Exception:
            self.logger.exception("Control audit persistence failed")

    async def _get_updates(self, client: httpx.AsyncClient) -> list[dict[str, object]]:
        response = await client.get(
            self._api_url("getUpdates"),
            params={
                "offset": self._offset,
                "timeout": int(self.settings.telegram_control_poll_seconds),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError("Telegram getUpdates returned an unsuccessful response")
        result = payload.get("result", [])
        return result if isinstance(result, list) else []

    async def _set_command_menu(self, client: httpx.AsyncClient) -> None:
        started_at = perf_counter()
        try:
            response = await client.post(
                self._api_url("setMyCommands"),
                json={
                    "commands": [
                        {"command": key[1:], "description": value}
                        for key, value in COMMAND_HELP.items()
                    ]
                },
            )
            response.raise_for_status()
            self._record_telegram_outcome(
                "success", latency_ms=(perf_counter() - started_at) * 1_000
            )
        except httpx.HTTPStatusError as exc:
            outcome = (
                "unrecoverable_failure"
                if 400 <= exc.response.status_code < 500 and exc.response.status_code != 429
                else "recoverable_failure"
            )
            self._record_telegram_outcome(outcome, latency_ms=(perf_counter() - started_at) * 1_000)
            raise
        except (httpx.HTTPError, TimeoutError):
            self._record_telegram_outcome(
                "recoverable_failure", latency_ms=(perf_counter() - started_at) * 1_000
            )
            raise

    async def send_message(self, chat_id: str, text: str) -> None:
        if not self.configured or chat_id not in self.settings.telegram_allowed_chat_ids:
            return
        client = self._client or httpx.AsyncClient(timeout=self.settings.telegram_timeout_seconds)
        started_at = perf_counter()
        try:
            for attempt in range(3):
                try:
                    response = await client.post(
                        self._api_url("sendMessage"), json={"chat_id": chat_id, "text": text[:3500]}
                    )
                    response.raise_for_status()
                    self._record_telegram_outcome(
                        "success", latency_ms=(perf_counter() - started_at) * 1_000
                    )
                    return
                except httpx.HTTPStatusError as exc:
                    if 400 <= exc.response.status_code < 500 and exc.response.status_code != 429:
                        self._record_telegram_outcome(
                            "unrecoverable_failure",
                            latency_ms=(perf_counter() - started_at) * 1_000,
                        )
                        return
                    if attempt == 2:
                        self._record_telegram_outcome(
                            "recoverable_failure",
                            latency_ms=(perf_counter() - started_at) * 1_000,
                        )
                        return
                    await asyncio.sleep(0.5 * (2**attempt))
                except (httpx.HTTPError, TimeoutError):
                    if attempt == 2:
                        self._record_telegram_outcome(
                            "recoverable_failure",
                            latency_ms=(perf_counter() - started_at) * 1_000,
                        )
                        return
                    await asyncio.sleep(0.5 * (2**attempt))
        finally:
            if self._owns_client:
                await client.aclose()

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method}"

    def _record_telegram_outcome(self, outcome: str, *, latency_ms: float) -> None:
        try:
            self.health.record_telegram_outcome(outcome, latency_ms=latency_ms)
        except Exception:
            self.logger.exception("Telegram health persistence failed")

    def _supervised_commands(self) -> dict[str, list[str]]:
        return {
            "api": [sys.executable, str(self.project_root / "main.py"), "server"],
            "live": [sys.executable, str(self.project_root / "main.py"), "live"],
        }

    def _load_offset(self) -> int:
        try:
            return int(json.loads(self._offset_path.read_text(encoding="utf-8"))["offset"])
        except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
            return 0

    def _advance_offset(self, update_id: int | None) -> None:
        if update_id is None:
            return
        self._offset = max(self._offset, update_id + 1)
        self._offset_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._offset_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"offset": self._offset}), encoding="utf-8")
        os.replace(temporary, self._offset_path)


def _int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _format_percent(value: float | None) -> str:
    return "UNKNOWN" if value is None else f"{value:.2f}%"


def _format_observed(value: datetime | None) -> str:
    return "UNKNOWN" if value is None else value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_observed_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "UNKNOWN"
    try:
        return _format_observed(datetime.fromisoformat(value))
    except ValueError:
        return "UNKNOWN"


def _freshness_state(value: datetime | None, *, max_age_seconds: float) -> str:
    if value is None:
        return "UNKNOWN"
    observed = value if value.tzinfo else value.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - observed).total_seconds()
    return "STALE" if age > max_age_seconds else "LIVE"


def _is_stale(timestamp: datetime, *, max_age: timedelta = timedelta(minutes=5)) -> bool:
    observed = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
    return datetime.now(UTC) - observed > max_age
