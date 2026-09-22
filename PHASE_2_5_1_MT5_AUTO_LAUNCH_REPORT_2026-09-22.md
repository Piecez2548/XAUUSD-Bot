# Phase 2.5.1 MT5 Auto-Launch Report — 2026-09-22

## Final status

**PHASE 2.5.1 IMPLEMENTED — MANUAL MT5 AUTO-LAUNCH VALIDATION REQUIRED**

The Telegram `/start` flow now performs bounded, read-only MT5 readiness
verification before it starts the existing API/live monitoring infrastructure.
No real MT5 auto-launch acceptance was performed in this turn, and execution
remains disabled.

## Architecture

The existing Telegram Control service remains the owner of `/start`, the
existing `ProcessSupervisor` remains the owner of API/live child processes, and
the new `mt5.bootstrap.MT5AutoLauncher` owns only the terminal-availability
check and launch decision. The launcher never stops or adopts an unrelated
terminal and never calls a broker write API.

Readiness uses the existing `MT5Connection`, symbol discovery, symbol/tick
readers, and closed-candle market-data reader. The verification sequence is:

1. Inspect `terminal64.exe` processes.
2. Reuse a detected terminal; otherwise require `MT5_AUTO_LAUNCH=true` and a
   configured executable path.
3. Launch at most one configured terminal process and wait with a bounded
   timeout.
4. Initialize the existing Python MT5 connection.
5. Verify terminal connectivity, account availability/pinning, configured
   broker server (when configured), XAUUSD symbol availability, tick/spec data,
   two closed M5 candles, and database health.
6. Only after all checks pass, start the existing API/live supervisor
   components.
7. Verify final API, live runtime, snapshot, database, and forward-worker
   health before returning `SYSTEM READY`.

## Configuration

Added to `Settings` and `.env.example`:

```text
MT5_AUTO_LAUNCH=false
MT5_TERMINAL_PATH=
MT5_STARTUP_TIMEOUT_SECONDS=30
```

`.env.example` contains placeholders only. The real `.env` remains ignored and
was not read into this report. Existing `MT5_LOGIN`, `MT5_SERVER`, and
`MT5_PASSWORD` configuration is reused; `MT5_LOGIN` pins the expected account
when supplied.

## Exact startup state machine

```text
/start
  -> acquire existing control operation lock
  -> migrate/check database
  -> probe terminal64.exe
       -> found: REUSED
       -> absent + auto-launch disabled: FAIL CLOSED
       -> absent + path missing: FAIL CLOSED
       -> absent + valid path: launch once, wait bounded: STARTED
  -> initialize MT5 Python connection
  -> terminal/account/server/symbol/tick/closed-M5/database checks
       -> any failure: monitoring NOT STARTED; Telegram Control stays online
  -> if API/live/MT5/runtime/database/forward are already healthy:
       ALREADY RUNNING / SYSTEM READY
  -> otherwise start supervised API/live processes
  -> bounded final health verification
       -> failure: supervised startup is not reported as ready
       -> success: SYSTEM READY
```

Successful responses include MT5 state (`STARTED` or `REUSED`), account
verification, market-data readiness, API/live state, actual forward-worker
state, execution disabled, and the existing forward session ID when present.

## Timeout and failure behavior

The timeout is `MT5_STARTUP_TIMEOUT_SECONDS` and covers process readiness plus
retryable terminal-connection readiness. Terminal/account/symbol/market-data
and database failures return a safe reason without exposing credentials. A
failed MT5 check prevents API/live startup. Repeated `/start` calls do not
spawn another process while the launcher-owned process is alive; a detected
terminal is reused. Unrelated MT5 processes are never terminated.

`/stop` continues to stop only verified API/live supervisor children. It does
not call MT5 shutdown and does not terminate the terminal process.

## Duplicate-process protection

- `terminal64.exe` is probed before every start.
- A running terminal is returned as `REUSED`; no second `Popen` occurs.
- A launcher-owned process that is still alive is waited on instead of relaunched.
- Existing `ProcessSupervisor` duplicate-command detection remains unchanged
  for API/live and is covered by the pre-existing supervisor tests.
- The process launch uses `shell=False` and the configured executable only.

## Forward session preservation

No Pair Zone strategy/configuration was modified. The existing forward worker
still resumes an `ACTIVE`/`PAUSED` session by its persisted ID and activation
timestamp. `/stop` causes the normal forward worker pause transition; the next
successful `/start` resumes the same session. Signals, virtual trades, and
performance history are not reset or recreated.

## Tests added

`tests/test_phase251_mt5_autolaunch.py` covers:

- already-running terminal reuse;
- absent terminal launch-once and repeated-start idempotency;
- launch timeout and no second launch;
- connection failure fail-closed;
- expected-account mismatch;
- unavailable symbol;
- market-data verification failure;
- healthy monitoring idempotent `/start`;
- `/start` blocked before supervised workers when MT5 is not ready;
- `/stop` does not touch MT5;
- forward session ID and activation timestamp survive stop/start;
- execution remains disabled through the readiness response.

Existing safety tests continue to verify no production `.order_send()` path and
secret/runtime ignore rules.

## Validation results

Final backend validation:

- full pytest: **170 passed, 2 warnings**;
- Ruff: **All checks passed**;
- compileall: **passed**;
- production source scan: **no `mt5.order_send`**;
- tracked runtime/secret scan: **no forbidden tracked artifacts**;
- `git diff --check`: **passed**.

No frontend files were changed by Phase 2.5.1, so frontend Vitest,
TypeScript, ESLint, and production build were not rerun for this backend-only
change. The Phase 2.5 frontend validation remains unchanged.

No commit, push, tag, deployment, strategy change, Forward Shadow reset, or
execution enablement was performed.

## Manual acceptance procedure

1. Set `MT5_AUTO_LAUNCH=true` and set `MT5_TERMINAL_PATH` to the local
   `terminal64.exe` path. Keep credentials in the ignored `.env` only.
2. Leave the Scheduled Task Telegram Control process running.
3. Ensure no unrelated MT5 terminal is closed or modified by the test; the
   launcher is expected to reuse a running terminal.
4. Send `/start` once. Confirm the response says `SYSTEM READY`, `MT5
   Auto Launch: STARTED` or `REUSED`, `Account VERIFIED`, `Market Data READY`,
   `API RUNNING`, `Live Engine RUNNING`, the actual Forward state, and
   `Execution DISABLED`.
5. Send `/start` again. Confirm `ALREADY RUNNING`, no additional
   `terminal64.exe` instance, unchanged Forward Session ID, unchanged
   activation timestamp, and no new strategy/session records.
6. Send `/stop`. Confirm API/live stop and `Broker positions were NOT
   modified`; confirm the MT5 terminal process remains running.
7. Send `/start` again and verify the same Forward session ID, signal/trade
   history, and activation timestamp are preserved.
8. Test fail-closed cases one at a time: invalid path, MT5 unavailable,
   connection failure, wrong pinned account, unavailable symbol, and missing
   closed M5 data. Each must report a safe reason, leave monitoring unstarted,
   and keep Telegram Control online.
9. Confirm `/health`, `/forwardhealth`, API health, and the dashboard agree and
   that no `PROCESS_CRASHED`, broker write, or order-send event occurs.
