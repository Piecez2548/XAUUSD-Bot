# Minimum Safe Trading Release — Web Control Plane MVP

## Scope

This slice adds the smallest private Web operator surface while preserving the
accepted Telegram lifecycle path, Supervisor ownership, Pair Zone behavior,
Forward Shadow history, and DemoExecutionService semantics. No production
runtime command, MT5 operation, `.env` edit, production database migration,
Scheduled Task restart, commit, or push was performed during implementation.

## Architecture

```text
authenticated Tailscale browser
        |
        v
FastAPI exact /api/control allowlist
        |
Windows local authenticated named pipe
        |
persistent Control process
        |
existing canonical handlers + existing operation lock
        |
ProcessSupervisor -> API / Live
```

The named pipe is installation-scoped, local-only, authenticated by the
Windows pipe handshake, and accepts only the fixed command vocabulary. Requests
require a UUID operation ID and bounded actor identity. `control_audit` stores
the operation ID as the correlation ID; a replay returns a safe duplicate
response without executing the handler again. FastAPI has no Supervisor or
MT5 authority and cannot accept shell/process names or arbitrary commands.

## Endpoints

- `GET /api/control/status`
- `POST /api/control/start`
- `POST /api/control/stop`
- `POST /api/control/restart`
- `GET /api/control/demo-status`
- `POST /api/control/demo-on`
- `POST /api/control/demo-off`

The endpoints require the existing Tailscale identity boundary. GET endpoints
do not mutate runtime state. POST endpoints accept only `{ "operation_id":
"<UUID>" }`. Unknown paths, methods, malformed requests, unauthenticated
requests, broker writes, lifecycle paths outside this allowlist, and any
real-money enable operation fail closed.

## Scheduled Task reliability

`scripts/install_background_control_task.ps1` updates the accepted
`XAUUSD Bot Background` task in place with hidden/logon startup,
`StartWhenAvailable=true`, and `StopIfGoingOnBatteries=false`. It does not
start or stop the current runtime. No duplicate Task Scheduler task was
created by this implementation.

## Compatibility and safety

- Telegram `/start`, `/stop`, `/restart`, `/status`, `/demo_*` remain available.
- Telegram and Web use the same Control handler implementations.
- Pair Zone rules and thresholds are unchanged.
- Forward Shadow semantics/history/session are unchanged.
- DemoExecutionService and MT5 order behavior are unchanged.
- `REAL_MONEY_EXECUTION` remains `DISABLED`.
- The Web UI contains no live-money enable action.
- The public Vercel build remains offline-safe when no explicit API base is configured.

## Validation and operator gate

Automated validation covers private authentication, exact route/method
allowlisting, fixed IPC protocol, malformed-message rejection, replay
protection, Demo configuration gating, common Control dispatch, frontend
status/button/confirmation/failure behavior, and the retained Phase 2.5/2.6
contracts. Production runtime acceptance is intentionally not performed by the
implementation task.

Before enabling Web mutations in production, an operator must verify:

1. inspect the current Task Scheduler task and apply the battery-safe installer
   only to `XAUUSD Bot Background`;
2. build the private dashboard with `scripts/build_frontend.ps1`;
3. confirm `REMOTE_DASHBOARD_MODE=true`, Tailscale Serve is tailnet-only, and
   Funnel remains disabled;
4. with the runtime healthy, open the private `/control` page and verify
   status only;
5. exercise START/STOP/RESTART and Demo arm/disarm one at a time, using the
   existing Telegram `/status` and `/demo_status` as independent evidence;
6. confirm API remains only on `127.0.0.1:8000`, no `:5173` server exists, no
   duplicate process tree appears, broker positions are unchanged, and real
   money remains disabled.

No commit or push is included in this report.
