# Phase 1.7 Implementation Report — 2026-09-22

## 1 Architecture implemented

Added a persistent `TelegramControlService` using Telegram long polling and a
local `ProcessSupervisor` for the API and read-only Live Data Engine. The
control service is independent from the Live Data Engine and uses the existing
database health, event, WebSocket, and FastAPI architecture.

## 2 Files changed

- `config/settings.py`, `.env.example`
- `services/control.py`, `services/supervisor.py`, `main.py`
- `persistence/orm.py`, `persistence/repositories.py`
- `alembic/versions/20260922_0003_control_audit.py`
- `api/app.py`
- `frontend/src/pages/ModulePages.tsx`, `frontend/src/types.ts`
- `scripts/install_telegram_control_task.ps1`
- `scripts/uninstall_telegram_control_task.ps1`
- `scripts/status_telegram_control_task.ps1`
- `tests/test_phase17_control.py`
- `docs/ARCHITECTURE.md`, `docs/DATABASE.md`, `docs/OPERATIONS.md`, `docs/SECURITY.md`, `README.md`

## 3 Telegram commands

Implemented `/start`, `/stop`, `/restart`, `/status`, `/health`, `/account`,
`/market`, `/positions`, `/risk`, `/logs`, and `/help`. All handlers are
fixed, bounded, and read-only with respect to MT5. `/start` starts monitoring
infrastructure only.

## 4 Authorization model

Commands are denied by default and require `TELEGRAM_CONTROL_ENABLED=true`, a
private chat, and a match in both `TELEGRAM_ALLOWED_CHAT_IDS` and
`TELEGRAM_ALLOWED_USER_IDS`. Unauthorized requests receive no system data and
are recorded in `control_audit`.

## 5 Supervisor state machine

The registry uses `STARTING`, `RUNNING`, `STOPPING`, `STOPPED`, `CRASHED`,
`DEGRADED`, and `ERROR`. Registry records contain lifecycle metadata only;
the API exposes a sanitized subset.

## 6 Process lifecycle behavior

`/start` is idempotent, verifies the database, starts API and Live Engine
children, and reports incomplete state when verification is not fresh or
successful. `/stop` gracefully stops children and explicitly reports that
broker positions were not modified. `/restart` serializes stop/start.

## 7 Single-instance protection

An atomic `O_EXCL` supervisor lock serializes lifecycle operations. Stale locks
are recovered only when their owner PID is no longer alive. Existing persisted
PIDs are reused only after conservative command-line identity verification;
unverified PIDs are never terminated.

## 8 Crash recovery policy

Unexpected child exits are marked `CRASHED` and restarted within the configured
window, defaulting to at most 3 restarts in 10 minutes. Once the bound is
exceeded the component becomes `ERROR` and no further automatic restarts occur.

## 9 Telegram polling design

`getUpdates` long polling uses the configured poll interval and bounded client
timeouts. `setMyCommands` registers the fixed menu. Delivery retries are
bounded to three attempts with exponential backoff; polling failures are
isolated and do not crash the observatory.

## 10 Duplicate-update protection

The highest processed update offset is persisted atomically in
`data/telegram_update_offset.json`. Updates below the offset are ignored, so
restarts and Telegram retries do not execute commands twice.

## 11 Audit model

`control_audit` stores UTC time, command, actor identifiers, authorization,
bounded result, and correlation ID. It does not store message bodies, process
commands, credentials, or Telegram tokens.

## 12 Rate limiting

Commands are rate-limited per `(user_id, command)` with a configurable default
of three seconds. State-changing commands share an async operation lock.

## 13 Production dashboard architecture

After `npm run build`, FastAPI serves `frontend/dist` at
`http://127.0.0.1:8000/`; Vite is not required for normal operation. The
health page polls the existing API and displays sanitized supervisor state.

## 14 Windows startup implementation

Explicit Task Scheduler scripts install, uninstall, and query an ONLOGON task
that starts only `python main.py control`. Installation is never automatic and
does not start monitoring or execution by itself.

## 15 Security controls

No remote shell, `shell=True`, `eval`, `exec`, `os.system`, order submission,
or trade execution path was added. API, WebSocket, frontend, logs, and health
payloads exclude bot tokens and chat IDs. Execution remains permanently
`DISABLED` in this phase.

## 16 Safety scan results

Source scan found no execution calls or unsafe shell APIs outside test-only
assertions. Supervisor subprocesses use argument lists, `shell=False`, and
redirected standard streams.

## 17 Tests added

`tests/test_phase17_control.py` covers deny-by-default authorization, private
chat enforcement, command availability, duplicate updates, graceful stop,
supervisor duplicate start/stop, stale lock recovery, and bounded crash
recovery. Existing Telegram health tests cover disabled, unknown, connected,
degraded/error, recovery, secret redaction, API, and WebSocket paths.

## 18 Exact regression results

- Python: **75 passed, 2 warnings**
- Ruff: **All checks passed**
- `compileall`: **passed**
- Frontend Vitest: **4 passed**
- Frontend TypeScript: **passed**
- Frontend ESLint: **passed**
- Frontend Vite production build: **passed**
- Alembic migration smoke test through revision `20260922_0003`: **passed**

The two warnings are dependency deprecation warnings from Starlette/httpx and
AnyIO; no test failed.

## 19 Manual validation commands

```powershell
cd "D:\Project_001\Nexus-Project\XAUUSD Bot"
$env:TELEGRAM_CONTROL_ENABLED = "true"
$env:TELEGRAM_ALLOWED_CHAT_IDS = "<private-chat-id>"
$env:TELEGRAM_ALLOWED_USER_IDS = "<telegram-user-id>"
python main.py migrate
python main.py control
```

Then use `/status`, `/health`, `/start`, `/account`, `/market`, `/positions`,
`/risk`, `/logs`, `/stop`, and `/restart` from the allowlisted private chat.
Run `python main.py telegram-test` separately to verify notification delivery
and persisted `CONNECTED` health.

## 20 Known limitations

This automated run did not use live broker credentials or a real Telegram bot,
so MT5 connectivity and external delivery still require operator validation.
The control process supervises API and Live Engine children; it is intentionally
not a child of its own supervisor registry. No Phase 2 AI decision or trade
execution work was implemented.
