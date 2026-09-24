"""Authenticated Telegram control plane for local monitoring infrastructure."""
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, perf_counter
from uuid import uuid4

import httpx
from sqlalchemy import desc, func, select

from config.settings import Settings
from mt5.bootstrap import MT5AutoLauncher, MT5StartupResult
from notifications.telegram_th import (
    execution_disabled,
    format_thai_datetime,
    format_value,
    status_line,
    thai_help,
    translate_health_state,
    translate_trade_state,
)
from persistence.database import Database
from persistence.migrations import migrate_database
from persistence.orm import (
    AccountSnapshotRecord,
    CandleRecord,
    ForwardSignalRecord,
    ForwardValidationSessionRecord,
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
from services.control_ipc import (
    ControlIpcError,
    ControlIpcServer,
    validate_ipc_request,
)
from services.demo_execution import demo_execution_armed, set_demo_execution_enabled
from services.shadow_outcome import OUTCOME_POLICY_VERSION, performance_summary
from services.supervisor import (
    ProcessSupervisor,
    resolve_supervised_python,
    write_supervisor_diagnostic,
    write_supervisor_lifecycle_event,
)
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
    "/backtests": "List recent persisted research runs",
    "/backtest": "Queue a deterministic research backtest",
    "/compare": "Compare persisted strategy research runs",
    "/robustness": "Show persisted Pair Zone robustness evidence",
    "/costs": "Show persisted Pair Zone cost sensitivity",
    "/stability": "Show persisted Pair Zone temporal stability",
    "/forward": "Show live forward shadow validation",
    "/forwardhealth": "Show forward shadow worker health",
    "/forwardtrades": "Show recent forward virtual trades",
    "/forwardperformance": "Show forward gross/net performance",
    "/dashboard": "Open the configured dashboard URL",
    "/demo_on": "Arm Demo execution gates",
    "/demo_off": "Disable new Demo orders",
    "/demo_status": "Show Demo execution gate state",
    "/logs": "Show safe recent operational events",
    "/help": "Show available commands",
}

API_READINESS_VERSION = "phase26-supervisor-socket-readiness-v2"


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
        mt5_bootstrap: MT5AutoLauncher | None = None,
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
        self.mt5_bootstrap = mt5_bootstrap or MT5AutoLauncher(settings, logger=self.logger)
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
        self._research_tasks: set[asyncio.Task[object]] = set()
        self._ipc_server: ControlIpcServer | None = None
        from services.research_platform import recover_interrupted_runs
        recover_interrupted_runs(self.database)

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
                "Telegram notifications disabled or incomplete; local Web Control IPC remains active"
            )
        try:
            self._ipc_server = ControlIpcServer(
                self.project_root,
                asyncio.get_running_loop(),
                self._handle_ipc_request,
                request_timeout_seconds=max(
                    120.0, self.settings.supervisor_operation_timeout_seconds + 90.0
                ),
            )
            self._ipc_server.start()
        except ControlIpcError:
            self.logger.exception("Local Web Control IPC could not start; Web mutations unavailable")
            self._ipc_server = None

        # Keep the authenticated local API available while the trading
        # runtime is intentionally stopped.  This bootstraps only the
        # control-plane child; MT5 and Live remain explicit lifecycle work.
        if not await self._ensure_control_plane_api():
            self.logger.error(
                "Control Plane API bootstrap did not reach a verified loopback listener"
            )

        client = self._client
        if client is None and self.configured:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.settings.telegram_control_poll_seconds + 10,
                    connect=self.settings.telegram_timeout_seconds,
                )
            )
        try:
            if client is not None:
                await self._set_command_menu(client)
            while not self._stop.is_set():
                try:
                    for record in self.supervisor.monitor_once(self._supervised_commands()):
                        if self._process_crash_notification_required(record):
                            key = (record.component, record.restart_count, record.state)
                            if key not in self._process_notifications:
                                self._process_notifications.add(key)
                                await self.send_message(
                                    self.settings.telegram_chat_id
                                    or self.settings.telegram_allowed_chat_ids[0],
                                    f"PROCESS_CRASHED\nComponent: {record.component}\n"
                                    f"State: {record.state}\nRestart count: {record.restart_count}",
                                )
                    if client is not None:
                        updates = await self._get_updates(client)
                        for update in updates:
                            response = await self.handle_update(update)
                            message = update.get("message")
                            if response and isinstance(message, dict):
                                chat = message.get("chat")
                                if isinstance(chat, dict) and chat.get("id") is not None:
                                    await self.send_message(str(chat["id"]), response)
                    else:
                        await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("Telegram control polling failed")
                    await asyncio.sleep(min(30.0, self.settings.telegram_control_poll_seconds * 2))
        finally:
            if self._ipc_server is not None:
                self._ipc_server.close()
                self._ipc_server = None
            if self._owns_client and client is not None:
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
            response = await self._dispatch(command, text)
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

    @staticmethod
    def _process_crash_notification_required(record) -> bool:
        """Notify only for a crash transition, not historical restart count.

        ``restart_count`` is cumulative registry history.  A deliberate stop
        can therefore legitimately return ``STOPPED`` with a non-zero count.
        A recovered crash retains the exit diagnostic on the returned RUNNING
        record, while an unrecovered crash is CRASHED/ERROR.
        """

        if record.state in {"CRASHED", "ERROR"}:
            return True
        return bool(
            record.state == "RUNNING"
            and isinstance(record.last_error, str)
            and record.last_error.startswith("supervised process exited with code ")
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

    async def _dispatch(self, command: str, text: str = "") -> str:
        if command == "/start":
            return await self._start_infrastructure()
        if command == "/stop":
            return await self._stop_infrastructure()
        if command == "/restart":
            return await self._restart_infrastructure()
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
            "/backtests": self._backtests,
            "/compare": self._compare,
            "/robustness": self._robustness,
            "/costs": self._costs,
            "/stability": self._stability,
            "/forward": self._forward,
            "/forwardhealth": self._forwardhealth,
            "/forwardtrades": self._forwardtrades,
            "/forwardperformance": self._forwardperformance,
            "/dashboard": self._dashboard,
            "/demo_on": self._demo_on,
            "/demo_off": self._demo_off,
            "/demo_status": self._demo_status,
            "/logs": self._logs,
            "/help": self._help,
        }
        if command == "/backtest":
            return await self._backtest(text)
        if command == "/strategy":
            return self._strategy(text)
        return handlers[command]()

    async def _restart_infrastructure(self) -> str:
        async with self._operation_lock:
            return await self._restart_infrastructure_locked()

    async def _restart_infrastructure_locked(self) -> str:
        operation_id = str(uuid4())
        write_supervisor_lifecycle_event(
            self.project_root,
            operation="/restart",
            stage="restart_started",
            component="infrastructure",
            details={"operation_id": operation_id},
        )
        stop_response = await self._stop_infrastructure_locked(
            lifecycle_operation="restart",
            operation_id=operation_id,
        )
        records = self.supervisor.status()
        stop_verified = all(
            records.get(component) is not None
            and records[component].state == expected
            for component, expected in (("api", "RUNNING"), ("live", "STOPPED"))
        )
        write_supervisor_lifecycle_event(
            self.project_root,
            operation="/restart",
            stage="restart_stop_verified",
            component="infrastructure",
            details={
                "operation_id": operation_id,
                "verified_stopped": stop_verified,
                "states": {
                    component: getattr(records.get(component), "state", None)
                    for component in ("api", "live")
                },
            },
        )
        if not stop_verified:
            write_supervisor_lifecycle_event(
                self.project_root,
                operation="/restart",
                stage="restart_aborted",
                component="infrastructure",
                details={
                    "operation_id": operation_id,
                    "abort_reason": "existing supervised topology did not reach STOPPED",
                },
            )
            return (
                f"{stop_response}\n"
                "RESTART ABORTED — an existing supervised topology did not reach STOPPED."
            )
        write_supervisor_lifecycle_event(
            self.project_root,
            operation="/restart",
            stage="restart_start_transition",
            component="infrastructure",
            details={"operation_id": operation_id},
        )
        response = await self._start_infrastructure_locked()
        final_records = self.supervisor.status()
        write_supervisor_lifecycle_event(
            self.project_root,
            operation="/restart",
            stage="restart_completed",
            component="infrastructure",
            details={
                "operation_id": operation_id,
                "final_states": {
                    component: getattr(final_records.get(component), "state", None)
                    for component in ("api", "live")
                },
            },
        )
        return response

    async def _start_infrastructure(self) -> str:
        async with self._operation_lock:
            return await self._start_infrastructure_locked()

    async def _start_infrastructure_locked(self) -> str:
        try:
            self._reconcile_supervisor()
        except Exception as exc:
            self.logger.exception("Supervisor reconciliation failed before /start")
            self._write_startup_diagnostic("supervisor_reconciliation", exc)
            return self._startup_incomplete_response(
                {"supervisor": "ERROR", "api": "NOT_STARTED", "live": "NOT_STARTED"},
                f"supervisor reconciliation failed ({type(exc).__name__}); inspect local logs",
            )
        try:
            migrate_database(self.settings.database_url, self.project_root)
        except Exception as exc:
            self.logger.exception("Database migration failed before /start")
            self._write_startup_diagnostic("database_migration", exc)
            return (
                "🔴 ระบบยังไม่พร้อม\n"
                "การตรวจสอบ: DATABASE=FAILED\n"
                f"สาเหตุ: การเตรียมฐานข้อมูลล้มเหลว ({type(exc).__name__})\n"
                "ระบบติดตาม: ยังไม่เริ่ม\n"
                "Telegram Control: 🟢 ออนไลน์\n"
                f"{execution_disabled()}"
            )
        try:
            mt5_result = await self.mt5_bootstrap.ensure_ready(self.database)
        except Exception as exc:
            self.logger.exception("MT5 readiness failed unexpectedly before /start")
            self._write_startup_diagnostic("mt5_readiness", exc)
            return self._startup_incomplete_response(
                {"supervisor": "RECONCILED", "mt5": "ERROR", "api": "NOT_STARTED", "live": "NOT_STARTED"},
                f"MT5 readiness failed ({type(exc).__name__}); inspect local logs",
            )
        if not mt5_result.ready:
            return self._mt5_start_failure(mt5_result)
        if await self._monitoring_is_healthy():
            return self._system_ready_response(mt5_result, already_running=True)
        try:
            supervised_python = resolve_supervised_python()
        except RuntimeError as exc:
            self._write_startup_diagnostic("interpreter_resolution", exc)
            return self._startup_incomplete_response(
                {"api": "NOT_STARTED", "live": "NOT_STARTED"},
                str(exc),
            )
        api_command = [supervised_python, str(self.project_root / "main.py"), "server"]
        live_command = [supervised_python, str(self.project_root / "main.py"), "live"]
        self._startup_started_at = datetime.now(UTC)
        # API is the persistent control-plane child.  Only Live belongs to
        # the trading-runtime transaction and may be rolled back on failure.
        started_live_here: list[str] = []
        started_records: dict[str, object] = {}
        try:
            before = self.supervisor.status()
            api = self.supervisor.start_component("api", api_command)
            started_records["api"] = api
            if api.state != "RUNNING":
                return self._startup_incomplete_response(
                    {"api": api.state, "live": "NOT_STARTED"}, api.last_error
                )
            live = self.supervisor.start_component("live", live_command)
            started_records["live"] = live
            if self._start_spawned_by_request("live", before, live):
                started_live_here.append("live")
            if live.state != "RUNNING":
                self._stop_failed_start(
                    started_live_here,
                    reason=f"live verification failed: {live.last_error or live.state}",
                    records=started_records,
                )
                return self._startup_incomplete_response(
                    {"api": "OK", "live": live.state}, live.last_error
                )
        except Exception as exc:
            self.logger.exception("Supervised child startup failed before verification")
            self._write_startup_diagnostic("supervised_child_start", exc)
            self._stop_failed_start(
                started_live_here,
                reason=f"supervised child startup exception: {type(exc).__name__}",
                records=started_records,
            )
            return self._startup_incomplete_response(
                {"api": "ERROR", "live": "NOT_STARTED"},
                f"supervised child startup failed ({type(exc).__name__}); inspect local logs",
            )
        verification = await self._wait_for_startup_verification()
        verification_checks = dict(verification.get("checks", {}))
        verification_complete = bool(verification.get("complete"))
        write_supervisor_lifecycle_event(
            self.project_root,
            operation="/start",
            stage="startup_verification",
            component="infrastructure",
            details={
                "verification_state": "COMPLETE" if verification_complete else "INCOMPLETE",
                "checks": verification_checks,
                "rollback_trigger": (
                    None
                    if verification_complete
                    else "startup verification complete=False"
                ),
            },
        )
        if not verification["complete"]:
            checks = " ".join(
                f"{name.upper()}={value}" for name, value in verification_checks.items()
            )
            self._stop_failed_start(
                started_live_here,
                reason=f"startup verification incomplete: {checks}",
                records=started_records,
            )
            return (
                "🟠 START INCOMPLETE — ระบบเริ่มได้ไม่ครบ\n"
                f"การตรวจสอบ: {checks}\n"
                "ระบบติดตาม: ยังไม่เริ่ม\n"
                "Telegram Control: 🟢 ออนไลน์\n"
                f"{execution_disabled()}"
            )
        result = MT5StartupResult(
            ready=True,
            launch_state=mt5_result.launch_state,
            pid=mt5_result.pid,
            checks={**mt5_result.checks, **verification["checks"]},
            verification=mt5_result.verification,
        )
        return self._system_ready_response(result)

    @staticmethod
    def _startup_incomplete_response(checks: dict[str, str], reason: str | None) -> str:
        rendered = " ".join(f"{name.upper()}={value}" for name, value in checks.items())
        detail = reason or "supervised process identity or startup verification failed"
        return (
            "🟠 START INCOMPLETE — ระบบเริ่มได้ไม่ครบ\n"
            f"การตรวจสอบ: {rendered}\n"
            f"สาเหตุ: {detail}\n"
            "ระบบติดตาม: ยังไม่เริ่ม\n"
            "Telegram Control: 🟢 ออนไลน์\n"
            f"{execution_disabled()}"
        )

    def _reconcile_supervisor(self):
        reconcile = getattr(self.supervisor, "reconcile", None)
        if callable(reconcile):
            return reconcile()
        # Compatibility for test doubles and older injected supervisors.
        return self.supervisor.status()

    def _write_startup_diagnostic(self, stage: str, exc: BaseException) -> None:
        write_supervisor_diagnostic(
            self.project_root,
            operation="/start",
            stage=stage,
            exc=exc,
            secrets=(
                self.settings.mt5_password or "",
                self.settings.telegram_bot_token or "",
            ),
        )

    def _start_spawned_by_request(self, component: str, before, record) -> bool:
        """Only roll back processes actually created by this /start call."""

        marker = getattr(self.supervisor, "last_start_spawned", None)
        if callable(marker):
            return bool(marker(component))
        return False

    def _supervisor_state(self, records=None) -> str:
        records = records or self.supervisor.status()
        states = [records.get(name).state if records.get(name) else "STOPPED" for name in ("api", "live")]
        if all(state == "RUNNING" for state in states):
            return "CONNECTED"
        api = records.get("api")
        live = records.get("live")
        if (
            getattr(api, "state", None) == "RUNNING"
            and getattr(live, "state", None) == "STOPPED"
            and getattr(live, "desired_state", "STOPPED") == "STOPPED"
        ):
            return "CONNECTED"
        if any(state == "RUNNING" for state in states):
            return "DEGRADED"
        if all(state == "STOPPED" for state in states):
            return "STOPPED"
        return "DEGRADED"

    async def _monitoring_is_healthy(self) -> bool:
        records = self.supervisor.status()
        api_process = records.get("api")
        live_process = records.get("live")
        if (
            api_process is None
            or api_process.state != "RUNNING"
            or live_process is None
            or live_process.state != "RUNNING"
        ):
            return False
        if self._supervisor_state(records) != "CONNECTED":
            return False
        if self._latest_service_state("live_runtime") != "CONNECTED":
            return False
        if not self.database.healthcheck() or not await self._api_responsive():
            return False
        forward = self._forward_worker_state()
        return forward in {"CONNECTED", "DISABLED"}

    def _forward_worker_state(self) -> str:
        from services.forward_shadow import forward_health

        return str(forward_health(self.database, self.settings).get("state", "UNKNOWN"))

    def _mt5_start_failure(self, result: MT5StartupResult) -> str:
        checks = " ".join(f"{name.upper()}={value}" for name, value in result.checks.items())
        return (
            "🔴 SYSTEM NOT READY — ระบบยังไม่พร้อม\n"
            f"การตรวจสอบ: {checks}\n"
            f"สาเหตุ: {result.reason or 'MT5 readiness verification failed'}\n"
            "ระบบติดตาม: ยังไม่เริ่ม\n"
            "Telegram Control: 🟢 ออนไลน์\n"
            f"{execution_disabled()}"
        )

    def _system_ready_response(
        self, result: MT5StartupResult, *, already_running: bool = False
    ) -> str:
        session_id = "UNKNOWN"
        try:
            from services.forward_shadow import latest_forward_session

            session = latest_forward_session(self.database)
            if session is not None:
                session_id = session.session_id
        except Exception:
            self.logger.exception("Unable to read forward session for /start response")
        checks = result.checks
        headline = "🟢 ระบบพร้อมทำงาน — ALREADY RUNNING" if already_running else "🟢 ระบบพร้อมทำงาน"
        api_state = "CONNECTED" if checks.get("api") in {"OK", "CONNECTED"} else checks.get("api", "UNKNOWN")
        live_state = "CONNECTED" if checks.get("live_runtime") == "CONNECTED" else checks.get("live_runtime", "UNKNOWN")
        forward_state = checks.get("forward", self._forward_worker_state())
        launch = "🟢 เปิดให้อัตโนมัติแล้ว" if result.launch_state == "STARTED" else "🟢 ใช้ MT5 ที่เปิดอยู่แล้ว"
        return (
            f"{headline}\n\n"
            f"{status_line('MT5', checks.get('terminal', 'UNKNOWN'), connection=True)}\n"
            f"{'บัญชี':<18}{'🟢 ตรวจสอบแล้ว (VERIFIED)' if checks.get('account') in {'OK', 'CONNECTED', 'VERIFIED'} else translate_health_state(checks.get('account', 'UNKNOWN'))}\n"
            f"{'ข้อมูลตลาด':<18}{'🟢 พร้อม (READY)' if checks.get('market_data') in {'OK', 'CONNECTED', 'READY'} else translate_health_state(checks.get('market_data', 'UNKNOWN'))}\n"
            f"{status_line('API', api_state, connection=True)}\n"
            f"{status_line('Live Engine', live_state, connection=True)}\n"
            f"{status_line('Forward Shadow', forward_state)}\n\n"
            "โหมด: อ่านข้อมูล / จำลองการเทรด\n"
            f"MT5: {launch}\n"
            f"Forward Session: {session_id}\n"
            f"{execution_disabled()}"
        )

    def _stop_failed_start(
        self,
        components: list[str] | None = None,
        *,
        reason: str = "startup rollback",
        records: dict[str, object] | None = None,
    ) -> None:
        targets = ("live",) if components is None else components
        for component in targets:
            try:
                before = (records or {}).get(component)
                final = self.supervisor.stop_component(component)
                write_supervisor_lifecycle_event(
                    self.project_root,
                    operation="/start",
                    stage="startup_rollback",
                    component=component,
                    details={
                        "initial_pid": getattr(before, "pid", None),
                        "observed_topology": {
                            "parent_pid": getattr(before, "parent_pid", None),
                            "process_tree": getattr(before, "process_tree", ()),
                            "process_identities": getattr(before, "process_identities", ()),
                        },
                        "verification_state": getattr(before, "state", None),
                        "verification_failure_reason": getattr(before, "last_error", None),
                        "rollback_trigger": reason,
                        "rollback_target": component,
                        "final_state": getattr(final, "state", None),
                    },
                )
            except (AttributeError, RuntimeError):
                self.logger.warning("Unable to stop failed supervised component %s", component)

    async def _stop_infrastructure(self) -> str:
        async with self._operation_lock:
            return await self._stop_infrastructure_locked()

    async def _stop_infrastructure_locked(
        self,
        *,
        lifecycle_operation: str = "stop",
        operation_id: str | None = None,
    ) -> str:
        live = self._stop_supervised_component("live", lifecycle_operation, operation_id)
        # The API is the authenticated local Web Control Plane, so it remains
        # supervised and reachable while the trading runtime is stopped.
        api = self.supervisor.status().get("api")
        api_state = getattr(api, "state", "STOPPED")
        if live.state != "STOPPED" or api_state != "RUNNING":
            return (
                "🟠 หยุดระบบติดตามได้ไม่ครบ\n\n"
                f"Live Engine  {translate_health_state(live.state)}\nAPI           {translate_health_state(api_state)} (Control Plane)\n"
                "ไม่มีการแก้ไข Position ที่ Broker\nBroker positions were NOT modified.\n"
                "Telegram Control  🟢 ออนไลน์\n"
                "MT5 ไม่ถูกปิดโดยคำสั่งนี้\n"
                f"{execution_disabled()}"
            )
        return (
            "🔴 หยุดระบบติดตามตลาดแล้ว\n\n"
            "Live Engine  ⚫ หยุดทำงาน (STOPPED)\nAPI           🟢 ทำงานอยู่ (RUNNING — Control Plane)\n\n"
            "ไม่มีการแก้ไข Position ที่ Broker\nBroker positions were NOT modified.\n"
            "Telegram Control ยังคงออนไลน์\n"
            "MT5 ไม่ถูกปิดโดยคำสั่งนี้\n"
            "Execution         DISABLED\n"
            f"{execution_disabled()}"
        )

    def _stop_supervised_component(
        self,
        component: str,
        lifecycle_operation: str,
        operation_id: str | None,
    ):
        stop = self.supervisor.stop_component
        try:
            import inspect

            parameters = inspect.signature(stop).parameters.values()
            supports_context = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                or parameter.name in {"operation_id", "lifecycle_operation"}
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_context = False
        if supports_context:
            return stop(
                component,
                operation_id=operation_id,
                lifecycle_operation=lifecycle_operation,
            )
        return stop(component)

    async def _ensure_control_plane_api(self) -> bool:
        """Keep the authenticated local API available independently of Live."""

        try:
            records = self.supervisor.status()
            api = records.get("api")
            if api is None or api.state != "RUNNING":
                api = self.supervisor.start_component(
                    "api", self._supervised_commands()["api"]
                )
            if api.state != "RUNNING":
                self.logger.error(
                    "Control Plane API start failed: %s",
                    getattr(api, "last_error", None) or api.state,
                )
                return False
        except Exception:
            self.logger.exception("Control Plane API bootstrap failed")
            return False

        deadline = monotonic() + self.settings.supervisor_operation_timeout_seconds
        while True:
            if await self._api_responsive():
                return True
            if monotonic() >= deadline:
                return False
            await asyncio.sleep(0.25)

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
                "📡 สถานะระบบ",
                status_line("Supervisor", self._supervisor_state(records)),
                status_line("Live Engine", live_state, connection=True),
                status_line("API", records.get("api").state if records.get("api") else "STOPPED", connection=True),
                status_line("MT5", self._latest_service_state("mt5"), connection=True),
                status_line("ฐานข้อมูล", "CONNECTED" if self.database.healthcheck() else "DISCONNECTED", connection=True),
                status_line("Telegram", self._telegram_state(), connection=True),
                "โหมด: อ่านข้อมูลอย่างเดียว",
                execution_disabled(),
            ]
        )

    def _operator_status_payload(self) -> dict[str, object]:
        """Return a structured operator view without adding a new state source."""

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
        with self.database.session() as session:
            latest_session = session.scalar(
                select(ForwardValidationSessionRecord)
                .order_by(desc(ForwardValidationSessionRecord.started_at))
                .limit(1)
            )
            latest_signal = session.scalar(
                select(ForwardSignalRecord)
                .order_by(desc(ForwardSignalRecord.timestamp), desc(ForwardSignalRecord.id))
                .limit(1)
            )
        return {
            "checked_at": datetime.now(UTC),
            "control": "CONNECTED",
            "supervisor": self._supervisor_state(records),
            "api": records.get("api").state if records.get("api") else "STOPPED",
            "live": live_state,
            "mt5": self._latest_service_state("mt5"),
            "database": "CONNECTED" if self.database.healthcheck() else "DISCONNECTED",
            "telegram": self._telegram_state(),
            "forward_shadow": self._forward_worker_state(),
            "execution": {
                "demo_execution_enabled": bool(self.settings.demo_execution_enabled),
                "demo_kill_switch_armed": bool(demo_execution_armed(self.database)),
                "real_money_execution": "DISABLED",
            },
            "strategy": {
                # The durable forward tables retain canonical signals, not a
                # mutable current-zone snapshot.  Do not relabel the latest
                # historical signal as the current Pair Zone state.
                "pair_zone_state": "UNKNOWN",
                "current_direction": "UNKNOWN",
                "latest_canonical_signal_id": getattr(latest_signal, "signal_id", None),
                "latest_canonical_direction": getattr(latest_signal, "decision", None),
                "latest_canonical_signal_at": getattr(latest_signal, "timestamp", None),
                "latest_forward_session_id": getattr(latest_session, "session_id", None),
            },
        }

    async def _handle_ipc_request(self, message: dict[str, str]) -> dict[str, object]:
        """Handle the fixed Web command vocabulary inside the Control process."""

        request = validate_ipc_request(message)
        operation_id = request["operation_id"]
        command = request["command"]
        actor = request["actor"]
        if command == "status":
            return {
                "ok": True,
                "duplicate": False,
                "operation_id": operation_id,
                "command": command,
                "message": self._status(),
                "status": self._operator_status_payload(),
            }
        if command == "demo_status":
            return {
                "ok": True,
                "duplicate": False,
                "operation_id": operation_id,
                "command": command,
                "message": self._demo_status(),
                "status": self._operator_status_payload(),
            }
        async with self._operation_lock:
            existing = self.audit.find_by_correlation_id(operation_id)
            if existing is not None:
                return {
                    "ok": existing.result == "SUCCEEDED",
                    "duplicate": True,
                    "operation_id": operation_id,
                    "command": command,
                    "message": "Operation already processed; inspect current status.",
                }
            try:
                if command == "start":
                    message_text = await self._start_infrastructure_locked()
                elif command == "stop":
                    message_text = await self._stop_infrastructure_locked()
                elif command == "restart":
                    message_text = await self._restart_infrastructure_locked()
                elif command == "demo_on":
                    message_text = self._demo_on()
                elif command == "demo_off":
                    message_text = self._demo_off()
                else:
                    raise ControlIpcError("IPC command is not allowlisted")
                result = "SUCCEEDED"
            except Exception:
                self.logger.exception("Web Control command failed: %s", command)
                message_text = "Operation failed; inspect /api/control/status for verified state."
                result = "FAILED"
            self._audit(
                f"/{command}",
                None,
                actor,
                True,
                result,
                operation_id,
            )
            return {
                "ok": result == "SUCCEEDED",
                "duplicate": False,
                "operation_id": operation_id,
                "command": command,
                "message": message_text,
                "status": self._operator_status_payload(),
            }

    async def _wait_for_startup_verification(self) -> dict[str, object]:
        deadline = monotonic() + self.settings.supervisor_operation_timeout_seconds
        checks: dict[str, str] = {}
        startup_started_at = getattr(self, "_startup_started_at", datetime.now(UTC))
        while True:
            records = self.supervisor.status()
            api_process = records.get("api")
            live_process = records.get("live")
            api_ok = (
                api_process is not None
                and api_process.state == "RUNNING"
                and await self._api_responsive()
            )
            runtime = self._latest_service_state("live_runtime", minimum_timestamp=startup_started_at)
            mt5 = self._latest_service_state("mt5", minimum_timestamp=startup_started_at)
            _risk, _observed, freshness, _rows, snapshot_id = self._snapshot_context()
            checks = {
                "api": "OK" if api_ok else "WAITING",
                "live_runtime": "CONNECTED" if runtime == "CONNECTED" else runtime,
                "mt5": mt5,
                "snapshot": "COMPLETE" if snapshot_id and freshness == "LIVE" else freshness,
                "database": "CONNECTED" if self.database.healthcheck() else "DISCONNECTED",
                "forward": self._forward_worker_state(),
            }
            complete = (
                all(
                    value in {"OK", "CONNECTED", "COMPLETE", "DISABLED"}
                    for value in checks.values()
                )
                and live_process is not None
                and live_process.state == "RUNNING"
            )
            if complete or monotonic() >= deadline:
                return {"complete": complete, "checks": checks}
            await asyncio.sleep(0.25)

    async def _api_responsive(self) -> bool:
        """Verify the supervisor-owned API listener without bypassing auth.

        The API is intentionally protected even on localhost when private
        dashboard mode is enabled.  Readiness therefore cannot call an HTTP
        route.  ProcessSupervisor.status() reconciles the persisted identity
        against the current OS topology; the socket check then verifies that
        the verified API process has a loopback listener.
        """

        records = self.supervisor.status()
        api_process = records.get("api")
        identity_verified, identity_reason = self._api_process_verification(api_process)
        details = {
            "readiness_version": API_READINESS_VERSION,
            "record_state": getattr(api_process, "state", None),
            "desired_state": getattr(api_process, "desired_state", None),
            "registered_pid": getattr(api_process, "pid", None),
            "registered_process_create_time": getattr(
                api_process, "process_create_time", None
            ),
            "registered_process_identity_create_times": [
                {"pid": identity[0], "create_time": identity[1]}
                for identity in (getattr(api_process, "process_identities", ()) or ())
                if isinstance(identity, (tuple, list)) and len(identity) == 2
            ],
            "process_tree": list(getattr(api_process, "process_tree", ()) or ()),
            "identity_verified": identity_verified,
            "identity_reason": identity_reason,
            "configured_api_host": self.settings.api_host,
            "configured_api_port": self.settings.api_port,
        }

        def finish(ready: bool, reason: str, **updates: object) -> bool:
            details.update(updates)
            details["final_ready"] = ready
            details["final_reason"] = reason
            write_supervisor_lifecycle_event(
                self.project_root,
                operation="/start",
                stage="api_readiness_probe",
                component="api",
                details=details,
            )
            return ready

        if not identity_verified:
            return finish(
                False,
                "verified supervisor API process identity/state is unavailable",
                loopback_validation=False,
                socket_connect_attempted=False,
                socket_connect_succeeded=False,
            )

        endpoint = self._api_loopback_endpoint()
        if endpoint is None:
            return finish(
                False,
                "configured API host is not a valid loopback address",
                loopback_validation=False,
                socket_connect_attempted=False,
                socket_connect_succeeded=False,
            )
        host, port = endpoint
        details["validated_loopback_host"] = host
        details["validated_loopback_port"] = port
        try:
            connected = await asyncio.to_thread(self._api_socket_is_available, host, port)
        except (OSError, ValueError) as exc:
            return finish(
                False,
                "loopback API socket connection failed",
                loopback_validation=True,
                socket_connect_attempted=True,
                socket_connect_succeeded=False,
                socket_error_type=type(exc).__name__,
                socket_error=str(exc),
            )
        if not connected:
            return finish(
                False,
                "loopback API socket connection was not established",
                loopback_validation=True,
                socket_connect_attempted=True,
                socket_connect_succeeded=False,
            )

        # Reconcile once more after the connect to fail closed if the process
        # exited or the PID identity changed during the socket probe.
        latest = self.supervisor.status().get("api")
        latest_identity_verified, latest_identity_reason = self._api_process_verification(latest)
        same_pid = getattr(latest, "pid", None) == getattr(api_process, "pid", None)
        return finish(
            latest_identity_verified and same_pid,
            "verified API process and loopback listener are ready"
            if latest_identity_verified and same_pid
            else "API process identity changed or is no longer verified after socket connect",
            loopback_validation=True,
            socket_connect_attempted=True,
            socket_connect_succeeded=True,
            post_connect_state=getattr(latest, "state", None),
            post_connect_desired_state=getattr(latest, "desired_state", None),
            post_connect_pid=getattr(latest, "pid", None),
            post_connect_process_create_time=getattr(latest, "process_create_time", None),
            post_connect_process_tree=list(getattr(latest, "process_tree", ()) or ()),
            post_connect_identity_verified=latest_identity_verified,
            post_connect_identity_reason=latest_identity_reason,
            post_connect_same_pid=same_pid,
        )

    def _api_loopback_endpoint(self) -> tuple[str, int] | None:
        host = str(self.settings.api_host or "").strip()
        if host.casefold() == "localhost":
            host = "127.0.0.1"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        if not address.is_loopback:
            return None
        try:
            port = int(self.settings.api_port)
        except (TypeError, ValueError):
            return None
        if not 1 <= port <= 65535:
            return None
        return host, port

    @staticmethod
    def _api_process_verification(record) -> tuple[bool, str]:
        if record is None:
            return False, "API process record is absent"
        if getattr(record, "state", None) != "RUNNING":
            return False, "API process state is not RUNNING"
        if getattr(record, "desired_state", None) != "RUNNING":
            return False, "API desired_state is not RUNNING"
        pid = getattr(record, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            return False, "registered API PID is invalid"
        identities = tuple(getattr(record, "process_identities", ()) or ())
        if os.name == "nt" and not identities:
            return False, "Windows process identity set is empty"
        identity_pids = {
            identity[0]
            for identity in identities
            if isinstance(identity, (tuple, list)) and len(identity) == 2
        }
        if pid not in identity_pids:
            return False, "registered API PID is absent from verified process identities"
        return True, "registered API PID/create-time identity is verified by supervisor status"

    @classmethod
    def _api_process_is_verified(cls, record) -> bool:
        return cls._api_process_verification(record)[0]

    @staticmethod
    def _api_socket_is_available(host: str, port: int) -> bool:
        with socket.create_connection((host, port), timeout=1.0):
            return True

    def _health(self) -> str:
        statuses = self._latest_health()
        records = self.supervisor.status()
        statuses["supervisor"] = self._supervisor_state(records)
        statuses["live_runtime"] = self._latest_service_state("live_runtime")
        statuses["mt5"] = self._latest_service_state("mt5")
        statuses["worker:history"] = self._history_worker_state()
        statuses["worker:shadow"] = self._shadow_worker_state()
        statuses["worker:shadow_outcome"] = self._shadow_outcome_worker_state()
        statuses["worker:forward_shadow"] = self._forward_worker_state()
        lines = ["🩺 สุขภาพระบบ"]
        for name, default in (
            ("supervisor", "UNKNOWN"),
            ("live_runtime", "UNKNOWN"),
            ("mt5", "UNKNOWN"),
            ("database", "CONNECTED" if self.database.healthcheck() else "DISCONNECTED"),
            ("telegram", self._telegram_state()),
            ("worker:history", "UNKNOWN"),
            ("worker:shadow", "UNKNOWN"),
            ("worker:shadow_outcome", "UNKNOWN"),
            ("worker:forward_shadow", "UNKNOWN"),
        ):
            labels = {
                "supervisor": "ตัวควบคุม",
                "live_runtime": "ระบบ Live",
                "mt5": "MT5",
                "database": "ฐานข้อมูล",
                "telegram": "Telegram",
                "worker:history": "ข้อมูลย้อนหลัง",
                "worker:shadow": "Shadow",
                "worker:shadow_outcome": "ประเมินผล Shadow",
                "worker:forward_shadow": "Forward Shadow",
            }
            lines.append(status_line(labels[name], statuses.get(name, default)))
        lines.extend(["", execution_disabled()])
        lines.extend([
            f"Worker:History  {statuses.get('worker:history', 'UNKNOWN')}",
            f"Worker:Shadow   {statuses.get('worker:shadow', 'UNKNOWN')}",
        ])
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
                "🧠 สุขภาพ Shadow Engine",
                f"สถานะ: {translate_health_state(self._shadow_worker_state())}",
                f"แท่ง M5 ที่บันทึกล่าสุด: {latest_persisted_text}",
                f"แท่ง M5 ที่รับล่าสุด: {_format_observed_text(latest_received)}",
                f"แท่ง M5 ที่ประมวลผลล่าสุด: {_format_observed_text(latest_processed)}",
                f"แท่ง M5 ที่ตัดสินใจล่าสุด: {_format_observed_text(latest_decision)}",
                f"ความล่าช้า: {metadata.get('processing_lag_seconds', 'UNKNOWN')}",
                f"คิว: {metadata.get('queue_depth', 0)}/{metadata.get('queue_capacity', 8)}",
                f"รอประมวลผล: {metadata.get('deferred_count', 0)}",
                f"ค้างจาก catch-up: {metadata.get('catchup_pending_count', 0)}",
                f"ค้างทั้งหมด: {metadata.get('total_backlog', metadata.get('queue_depth', 0))}",
                f"ข้อผิดพลาดล่าสุด: {last_failure}",
                f"State: {self._shadow_worker_state()}",
                execution_disabled(),
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
            return "📊 บัญชี MT5\nสถานะ: ⚪ ยังไม่ทราบสถานะ (UNKNOWN)\nยังไม่มีข้อมูลบัญชีที่ตรวจสอบแล้ว"
        return (
            "📊 บัญชี MT5\n"
            "สถานะ: 🟢 ตรวจสอบแล้ว (CONNECTED)\n"
            f"ยอดคงเหลือ: {row.balance:.2f}\nEquity: {row.equity:.2f}\n"
            f"กำไร/ขาดทุนลอยตัว: {row.profit:.2f}\nMargin: {row.margin:.2f}\n"
            f"Free Margin: {row.free_margin:.2f}\nMargin Level: {row.margin_level:.2f}\n"
            f"เวลา: {format_thai_datetime(row.timestamp, include_utc=True)}"
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
            return "📈 ตลาด\nสถานะ: ⚪ ยังไม่ทราบสถานะ (UNKNOWN)\nยังไม่มีข้อมูลตลาดที่ตรวจสอบแล้ว"
        snapshot, symbol = row
        return (
            f"📈 ตลาด\nสัญลักษณ์: {symbol.name}\nBid: {snapshot.bid}\nAsk: {snapshot.ask}\n"
            f"Spread: {snapshot.spread}\nเวลา: {format_thai_datetime(snapshot.timestamp, include_utc=True)}"
        )

    def _positions(self) -> str:
        _risk_row, observed_at, freshness, position_rows, snapshot_id = self._snapshot_context()
        count = str(len(position_rows)) if freshness != "STATE_SYNC_PENDING" else "UNKNOWN"
        lines = [
            "📌 Position ที่ตรวจพบ",
            f"Position ที่เปิดอยู่: {count}",
            f"เวลาตรวจพบ: {_format_observed(observed_at)}",
            f"ความสดใหม่: {freshness}",
            f"Freshness: {freshness}",
            f"Snapshot: {snapshot_id or 'UNKNOWN'}",
        ]
        if freshness == "STATE_SYNC_PENDING":
            return "\n".join(lines + [f"Open positions: {count}", "ข้อมูล Position/Risk รอบล่าสุดยังไม่สอดคล้องกัน"])
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
            lines.extend(["ยังไม่มี Position ที่เปิดอยู่", f"Open positions: {count}"])
        return "\n".join(lines)

    def _risk(self) -> str:
        row, observed_at, freshness, _position_rows, snapshot_id = self._snapshot_context()
        if row is None:
            return "🛡️ สถานะความเสี่ยง\nสถานะ: ⚪ ยังไม่ทราบสถานะ (UNKNOWN)\nยังไม่มีข้อมูล Risk ที่ตรวจสอบแล้ว"
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
            "🛡️ สถานะความเสี่ยง\nความเสี่ยงสูงสุดต่อไม้: 2%\nงบความเสี่ยงรวมสูงสุด: 6%\n"
            f"ความเสี่ยงที่เปิดอยู่: {known_risk}\nงบความเสี่ยงที่เหลือ: {remaining}\n"
            f"Position ที่มีขอบเขตความเสี่ยง: {bounded_count}\n"
            f"Position ที่ไม่มีขอบเขตความเสี่ยง: {unbounded_count}\n"
            f"สถานะ: {state}\nเวลาตรวจพบ: {_format_observed(observed_at)}\nความสดใหม่: {freshness}\n"
            f"Snapshot: {snapshot_id or 'UNKNOWN'}\n"
            f"Bounded positions: {bounded_count}\nUnbounded positions: {unbounded_count}\n"
            f"Freshness: {freshness}\n\n{execution_disabled()}"
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
            return "🧠 การตัดสินใจล่าสุด\nสถานะ: ⚪ ยังไม่ทราบสถานะ (UNKNOWN)\nยังไม่มีการตัดสินใจที่เสร็จสมบูรณ์"
        lines = [
            "🧠 การตัดสินใจล่าสุด",
            f"การตัดสินใจ: {row.decision}",
            f"แนวโน้มตลาด: {row.market_regime}",
            f"กลยุทธ์: {row.strategy_name} {row.strategy_version}",
            f"Snapshot: {row.market_snapshot_id or 'UNKNOWN'}",
            f"แท่งเวลา: {format_thai_datetime(row.m5_candle_timestamp, include_utc=True)}",
            f"เหตุผล: {row.human_readable_reason}",
        ]
        if row.decision in {"BUY", "SELL"}:
            lines.extend(
                [
                    f"เข้า: {row.entry_price}",
                    f"SL: {row.stop_loss}",
                    f"TP: {row.take_profit}",
                    f"RR: {row.risk_reward_ratio}",
                    f"ขนาดไม้จำลอง: {row.hypothetical_volume}",
                    f"Risk: {row.approved_risk_percent}%",
                ]
            )
        lines.extend(
            [
                f"Risk Gate: {row.risk_gate_state}",
                "นี่คือการตัดสินใจจำลอง ไม่มี Order ถูกส่ง",
                "SHADOW ONLY — NO ORDER SENT",
                execution_disabled(),
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
            "🧠 สรุประบบ Shadow\n"
            f"การตัดสินใจทั้งหมด: {sum(counts.values())}\n"
            f"BUY: {counts.get('BUY', 0)}\nSELL: {counts.get('SELL', 0)}\n"
            f"NO_TRADE: {counts.get('NO_TRADE', 0)}\n"
            f"ผลลัพธ์ที่รอประเมิน: {pending}\n{execution_disabled()}\n"
            "Execution: DISABLED"
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
            return "📊 ผลลัพธ์ Shadow\nยังไม่มีผลลัพธ์ที่ประเมินแล้ว\n" + execution_disabled()
        lines = ["📊 ผลลัพธ์ Shadow ล่าสุด"]
        for row in rows:
            value = "UNKNOWN" if row.realized_r is None else f"{row.realized_r:.3f}R"
            lines.append(
                f"{format_thai_datetime(row.decision_m5_timestamp)} {row.side} "
                f"{translate_trade_state(row.terminal_status)} {value}"
            )
        lines.append(execution_disabled())
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
            "📈 ผลงาน Shadow\n"
            f"ไม้ที่เข้าเกณฑ์: {report['eligible_trades']}\nไม้ที่จบแล้ว: {report['resolved_sample_size']}\n"
            f"TP: {report['tp_hits']}  SL: {report['sl_hits']}  AMBIGUOUS: {report['ambiguous']}  EXPIRED: {report['expired']}\n"
            f"Win Rate: {fmt(report['win_rate'])}  Average R: {fmt(report['average_r'])}  Total R: {fmt(report['total_r'])}\n"
            f"{execution_disabled()}"
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
        observed = format_thai_datetime(row.timestamp, include_utc=True) if row else "ยังไม่มีข้อมูล"
        return f"🩺 สุขภาพระบบประเมินผล Shadow\nสถานะ: {translate_health_state(state)}\nเวลาตรวจพบ: {observed}\n{execution_disabled()}"

    def _strategy(self, text: str = "/strategy") -> str:
        from services.strategy_platform import StrategyRegistry

        parts = text.strip().split()
        identifier = parts[1] if len(parts) > 1 else self.settings.shadow_strategy
        try:
            strategy = StrategyRegistry(self.settings).resolve(identifier)
        except ValueError:
            return f"กลยุทธ์ไม่ถูกต้อง\nไม่รู้จัก strategy: {identifier}\n{execution_disabled()}"
        return (
            f"🧠 กฎกลยุทธ์ Shadow: {identifier}\n"
            f"Version: {strategy.metadata.strategy_version}\n"
            f"Config: {strategy.metadata.config_hash}\n"
            f"Rules: {strategy.explain()}\n"
            f"{execution_disabled()} — SHADOW ONLY"
        )

    def _strategies(self) -> str:
        from services.strategy_platform import StrategyRegistry

        registry = StrategyRegistry(self.settings)
        return "🧠 กลยุทธ์ Shadow ที่มีในระบบ\n" + "\n".join(
            f"{identifier}{' (ACTIVE)' if identifier == self.settings.shadow_strategy else ''}"
            for identifier in registry.identifiers()
        ) + "\n" + execution_disabled()

    def _research(self) -> str:
        from services.strategy_platform import StrategyRegistry

        strategy = StrategyRegistry(self.settings).resolve(self.settings.shadow_strategy)
        return (
            "🔬 กลยุทธ์ Shadow ที่ใช้งาน\n"
            f"{strategy.metadata.strategy_id}\n"
            f"Version: {strategy.metadata.strategy_version}\n"
            f"Config: {strategy.metadata.config_hash}\n"
            "นโยบายเริ่มใช้: ขอบเขตแท่ง M5 ที่ปิดถัดไป\n"
            f"{execution_disabled()}"
        )

    def _backtests(self) -> str:
        from services.research_platform import list_runs

        rows = list_runs(self.database, limit=8)
        if not rows:
            return "🧪 Backtest งานวิจัย\nยังไม่มีงานวิจัยที่บันทึกไว้\n" + execution_disabled()
        lines = ["🧪 Backtest งานวิจัยล่าสุด"]
        for row in rows:
            summary = row.summary_json if isinstance(row.summary_json, dict) else {}
            lines.append(f"{row.run_id[:8]} {row.status} {row.strategy_id} RR={row.parameters_json.get('rr', 'GRID')} "
                         f"BUY={summary.get('buy', 0)} SELL={summary.get('sell', 0)} NO_TRADE={summary.get('no_trade', 0)}")
        lines.append(execution_disabled())
        return "\n".join(lines)

    def _compare(self, text: str) -> str:
        from services.research_platform import list_runs

        requested = text.strip().split()[1:]
        if len(requested) < 2:
            return "เปรียบเทียบไม่ได้\nวิธีใช้: /compare trend_pullback_v1 pair_zone_v1"
        aliases = {
            "trend_pullback_v1": {"trend_pullback_v1", "trend_pullback"},
            "pair_zone_v1": {"pair_zone_v1"},
        }
        rows = [row for row in list_runs(self.database, limit=200)
                if row.status == "COMPLETED" and "chronological" not in (row.run_name or "")
                and not (row.run_name or "").endswith(("-development", "-validation", "-holdout"))
                and row.parameters_json.get("rr") == 2.0]
        lines = ["🧪 เปรียบเทียบกลยุทธ์ (RR=2.0)"]
        for identifier in requested:
            accepted = aliases.get(identifier, {identifier})
            row = next((item for item in rows if item.strategy_id in accepted), None)
            if row is None:
                lines.append(f"{identifier}: ยังไม่มีผลวิจัยที่บันทึกไว้")
                continue
            summary = row.summary_json if isinstance(row.summary_json, dict) else {}
            lines.append(
                f"{identifier} dataset={row.dataset_hash[:12]} signals={summary.get('trades', 0)} "
                f"expectancy={summary.get('expectancy')} total_R={summary.get('total_r')} "
                f"PF={summary.get('profit_factor')} win_rate={summary.get('win_rate')} "
                f"max_DD={summary.get('max_drawdown_r')}"
            )
        lines.append("แสดงหลักฐานเท่านั้น ไม่มีการเลือกผู้ชนะอัตโนมัติ")
        lines.append(execution_disabled())
        return "\n".join(lines)

    def _latest_robustness_summary(self) -> tuple[str | None, dict[str, object]]:
        from services.research_robustness import list_robustness

        rows = list_robustness(self.database, strategy_id="pair_zone_v1", limit=1)
        if not rows:
            return None, {}
        row = rows[0]
        return row.robustness_id, row.summary_json if isinstance(row.summary_json, dict) else {}

    def _robustness(self) -> str:
        robustness_id, summary = self._latest_robustness_summary()
        if robustness_id is None:
            return "🧪 Robustness\nยังไม่มีผลทดสอบ Pair Zone ที่บันทึกไว้\n" + execution_disabled()
        normal = (summary.get("cost_scenarios") or {}).get("normal", {})
        return ("🧪 ผลทดสอบ Robustness เสร็จสิ้น\nROBUSTNESS TESTS COMPLETE\n"
                f"Run: {robustness_id[:12]}\n"
                f"การจัดกลุ่ม: {summary.get('classification', 'unknown').upper()}\n"
                f"Signals BUY={summary.get('canonical_signals', {}).get('buy', 0)} SELL={summary.get('canonical_signals', {}).get('sell', 0)}\n"
                f"Normal cost Net R={normal.get('net_total_r')} Net DD={normal.get('net_max_drawdown_r')}\n"
                "สัญญาณถูกตรึงไว้; แสดงหลักฐานเท่านั้น\n" + execution_disabled())

    def _costs(self) -> str:
        robustness_id, summary = self._latest_robustness_summary()
        if robustness_id is None:
            return "🧪 ผลกระทบต้นทุน\nยังไม่มีผลทดสอบต้นทุน Pair Zone ที่บันทึกไว้\n" + execution_disabled()
        scenarios = summary.get("cost_scenarios") or {}
        lines = ["🧪 ผลกระทบ Spread/Slippage/Cost ของ Pair Zone"]
        for name in ("zero", "normal", "elevated", "stress"):
            row = scenarios.get(name, {})
            lines.append(f"{name}: gross={row.get('gross_total_r')} net={row.get('net_total_r')} cost={row.get('cost_total_r')}")
        lines.append(execution_disabled())
        return "\n".join(lines)

    def _stability(self) -> str:
        robustness_id, summary = self._latest_robustness_summary()
        if robustness_id is None:
            return "🧪 ความเสถียรตามช่วงเวลา\nยังไม่มีผลทดสอบ Pair Zone ที่บันทึกไว้\n" + execution_disabled()
        monthly = summary.get("monthly_stability_normal_cost") or {}
        sides = summary.get("buy_sell_normal_cost") or {}
        return ("🧪 ความเสถียรของ Pair Zone\n"
                f"จำนวนเดือน: {len(monthly)}\n"
                f"BUY net R={sides.get('BUY', {}).get('net_total_r')} SELL net R={sides.get('SELL', {}).get('net_total_r')}\n"
                "ใช้ช่วงเวลาตามลำดับจริง; แสดงหลักฐานเชิงพรรณนาเท่านั้น\n" + execution_disabled())

    def _forward(self) -> str:
        from services.forward_shadow import forward_performance, latest_forward_session

        row = latest_forward_session(self.database)
        if row is None:
            return "🔭 FORWARD TEST — Pair Zone V1\nยังไม่มี Forward Validation session ที่กำลังทำงาน\n" + execution_disabled()
        performance = forward_performance(self.database, row.session_id)
        combined = performance.get("combined", {})
        return (
            "🔭 FORWARD TEST — Pair Zone V1\n"
            f"สถานะ: {translate_health_state(row.status)}\n"
            f"กลยุทธ์: {row.strategy_id} {row.strategy_version}\n"
            f"Session: {row.session_id}\n"
            f"เริ่มทดสอบ: {format_thai_datetime(row.started_at, include_utc=True)}\n"
            f"สัญญาณทั้งหมด: {performance.get('signals', 0)}\n"
            f"ไม้จำลองที่เปิดอยู่: {performance.get('OPEN', 0)}\n"
            f"ไม้ที่จบแล้ว: {combined.get('resolved', 0)}\n"
            f"ผลรวมสุทธิ: {combined.get('net_total_r')} R\n\n"
            "นี่คือการเทรดจำลองจากข้อมูลตลาดจริง\nไม่มีการส่งคำสั่งซื้อขายไปยัง Broker\n"
            f"{execution_disabled()}"
        )

    def _forwardhealth(self) -> str:
        from services.forward_shadow import forward_health, latest_forward_session

        health = forward_health(self.database, self.settings)
        last_m5 = health.get("last_closed_m5")
        session = latest_forward_session(self.database)
        session_id = health.get("session_id") or (session.session_id if session else None)
        strategy_id = health.get("strategy_id") or (session.strategy_id if session else "pair_zone_v1")
        strategy_version = health.get("strategy_version") or (session.strategy_version if session else "")
        return (
            "🔭 สุขภาพ Forward Shadow\n"
            f"สถานะ: {translate_health_state(health.get('state'))}\n"
            f"กลยุทธ์: {strategy_id} {strategy_version}\n"
            f"Session:\n{session_id or 'UNKNOWN'}\n"
            f"แท่ง M5 ล่าสุด: {format_thai_datetime(last_m5, include_utc=True)}\n"
            f"สัญญาณล่าสุด: {health.get('last_signal') or 'ยังไม่มี'}\n"
            f"ไม้จำลองที่ยังเปิดอยู่: {health.get('open_shadow_trades', 0)}\n"
            f"{execution_disabled()}"
        )

    def _forwardtrades(self) -> str:
        from services.forward_shadow import forward_trades, latest_forward_session

        row = latest_forward_session(self.database)
        trades = forward_trades(self.database, row.session_id if row else None, limit=8)
        if not trades:
            return "📭 ไม้จำลอง Forward\nยังไม่มีสัญญาณที่เข้าเงื่อนไขสำหรับเปิดไม้จำลอง\nระบบยังคงตรวจตลาดตามปกติ\nนี่ไม่ใช่ข้อผิดพลาด\n\n" + execution_disabled()
        lines = ["📭 ไม้จำลอง Forward"]
        for trade in trades:
            rr = None
            if trade.risk_distance:
                rr = abs(trade.take_profit - trade.entry_price) / abs(trade.risk_distance)
            lines.extend([
                f"{trade.side} — {translate_trade_state(trade.state)}",
                f"เข้า: {trade.entry_price}",
                f"SL: {trade.stop_loss}",
                f"TP: {trade.take_profit}",
                f"RR: {format_value(rr, 2) if rr is not None else 'ยังไม่มีข้อมูล'}",
                f"เวลาเปิด: {format_thai_datetime(trade.timestamp, include_utc=True)}",
                f"ผลลัพธ์: {translate_trade_state(trade.state)}",
                f"Gross: {format_value(trade.gross_r, 2, signed=True)} R",
                f"Net: {format_value(trade.net_r, 2, signed=True)} R",
                "",
            ])
        lines.append(execution_disabled())
        return "\n".join(lines)

    def _forwardperformance(self) -> str:
        from services.forward_shadow import forward_performance, latest_forward_session

        row = latest_forward_session(self.database)
        if row is None:
            return "📈 ผลการทดสอบ Forward\nยังไม่มี Forward Test session\n" + execution_disabled()
        result = forward_performance(self.database, row.session_id)
        combined = result.get("combined", {})
        expired = result.get("expired_only", {})
        tp_sl = result.get("tp_sl_only", {})
        return (
            "📈 ผลการทดสอบ Forward\n"
            f"สัญญาณทั้งหมด: {result.get('signals', 0)}\nBUY: {result.get('BUY', 0)}\nSELL: {result.get('SELL', 0)}\n\n"
            f"ผลลัพธ์ TP/SL/AMBIGUOUS: {tp_sl.get('resolved', 0)}\n"
            f"TP/SL/AMBIGUOUS Net R: {tp_sl.get('net_total_r')}\n"
            f"EXPIRED: {expired.get('EXPIRED', 0)}  Net R: {expired.get('net_total_r')}\n"
            f"OPEN: {result.get('OPEN', 0)}\n"
            f"Expectancy: {combined.get('net_expectancy')} R\nProfit Factor: {combined.get('profit_factor')}\n"
            f"Net R: {combined.get('net_total_r')}\n\n"
            "ข้อมูลนี้มาจาก Forward Test เท่านั้น\nห้ามรวมกับผล Historical Backtest\n"
            f"{execution_disabled()}"
        )

    async def _backtest(self, text: str) -> str:
        from services.research_platform import (
            enqueue_backtest_dataset,
            mark_run_failed,
            run_backtest,
        )

        parts = text.strip().split()
        strategy_id = parts[1] if len(parts) == 2 else self.settings.shadow_strategy
        from services.strategy_platform import StrategyRegistry
        if strategy_id not in StrategyRegistry(self.settings).identifiers():
            return "🧪 เริ่ม Backtest ไม่ได้\nไม่รู้จัก strategy โปรดใช้ /strategies"
        job_id = enqueue_backtest_dataset(self.database, self.settings, strategy_id=strategy_id,
                                          project_root=self.project_root, run_name="telegram")
        self.health.record("worker:research", "CONNECTED", message="Research job queued",
                           metadata={"job_id": job_id, "execution_allowed": False})

        async def execute() -> None:
            try:
                await asyncio.to_thread(run_backtest, self.database, self.settings,
                                        project_root=self.project_root, strategy_id=strategy_id,
                                        rr=2.0, run_name="telegram", existing_run_id=job_id)
                self.health.record("worker:research", "CONNECTED", message="Research job completed",
                                   metadata={"job_id": job_id, "execution_allowed": False})
            except Exception:
                self.logger.exception("Research backtest job failed: %s", job_id)
                mark_run_failed(self.database, job_id, "RESEARCH_JOB_FAILED")
                self.health.record("worker:research", "DEGRADED", message="Research job failed",
                                   metadata={"job_id": job_id, "execution_allowed": False})

        task = asyncio.create_task(execute(), name=f"research-{job_id[:8]}")
        self._research_tasks.add(task)
        task.add_done_callback(self._research_tasks.discard)
        return f"🧪 รับงาน Backtest แล้ว\nJob: {job_id}\nStrategy: {strategy_id}\nดูความคืบหน้าได้ที่ /backtests\n{execution_disabled()}"

    def _dashboard(self) -> str:
        url = self.settings.dashboard_public_url
        return f"🖥️ Dashboard\n{url or '⚪ ยังไม่ทราบสถานะ (DASHBOARD_PUBLIC_URL ยังไม่ได้ตั้งค่า)'}\n{execution_disabled()}"

    def _demo_on(self) -> str:
        if not self.settings.demo_execution_enabled:
            return (
                "DEMO execution remains DISABLED.\n"
                "Set DEMO_EXECUTION_ENABLED=true and reload Control before arming."
            )
        set_demo_execution_enabled(
            self.database,
            True,
            reason="OPERATOR_ARMED_DEMO_EXECUTION",
            updated_by="telegram_control",
        )
        return (
            "DEMO execution ARMED.\n"
            "Each order still requires a verified DEMO account and every safety gate.\n"
            "Real-money execution remains DISABLED."
        )

    def _demo_off(self) -> str:
        set_demo_execution_enabled(
            self.database,
            False,
            reason="OPERATOR_KILL_SWITCH",
            updated_by="telegram_control",
        )
        return (
            "DEMO execution KILL SWITCH ACTIVE.\n"
            "No new Demo orders will be submitted.\n"
            "Existing broker positions are not closed by this command."
        )

    def _demo_status(self) -> str:
        configured = "true" if self.settings.demo_execution_enabled else "false"
        armed = "true" if demo_execution_armed(self.database) else "false"
        return (
            "DEMO execution status\n"
            f"DEMO_EXECUTION_ENABLED={configured}\n"
            f"KILL_SWITCH_ARMED={armed}\n"
            "REAL_MONEY_EXECUTION=DISABLED"
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
        lines = ["🧾 เหตุการณ์ล่าสุด (ข้อมูลปลอดภัย)"]
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            detail = payload.get("message") or payload.get("status") or "recorded"
            lines.append(f"{format_thai_datetime(row.timestamp)} {row.event_type}: {str(detail)[:120]}")
        return "\n".join(lines)

    @staticmethod
    def _help() -> str:
        return thai_help()

    def _latest_service_state(
        self,
        component: str,
        *,
        minimum_timestamp: datetime | None = None,
    ) -> str:
        with self.database.session() as session:
            if component == "mt5":
                health_row = session.scalar(
                    select(SystemHealthRecord)
                    .where(SystemHealthRecord.component == "mt5")
                    .order_by(desc(SystemHealthRecord.timestamp))
                    .limit(1)
                )
                if health_row is not None:
                    if minimum_timestamp is not None:
                        observed = (
                            health_row.timestamp
                            if health_row.timestamp.tzinfo
                            else health_row.timestamp.replace(tzinfo=UTC)
                        )
                        if observed < minimum_timestamp:
                            return "UNKNOWN"
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
        if minimum_timestamp is not None:
            observed = row.timestamp if row.timestamp.tzinfo else row.timestamp.replace(tzinfo=UTC)
            if observed < minimum_timestamp:
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
        supervised_python = resolve_supervised_python()
        return {
            "api": [supervised_python, str(self.project_root / "main.py"), "server"],
            "live": [supervised_python, str(self.project_root / "main.py"), "live"],
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
