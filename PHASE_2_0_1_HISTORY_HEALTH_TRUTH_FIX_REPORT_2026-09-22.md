# Phase 2.0.1 History Health Truth Fix — 2026-09-22

## Root cause

The history reconciler was successfully persisting `worker:history` rows, but
the Telegram and API readers selected only the newest 100 health rows across
all components.  The busy live workers could push the newest history row out
of that window, so the readers returned `UNKNOWN` even though reconciliation
was still succeeding.  `HISTORY_SYNC_COMPLETED` was never a valid health
source of truth.

The supplied paired timestamps were also confirmed to be duplicate live
runtime instances: two `main.py live` process groups were active, with the
older group orphaned after its control process exited.  Each group ran one
history task, producing the approximately three-second event pairs.

## Fix

- Every successful history reconciliation now persists an explicit
  `worker:history` `CONNECTED` heartbeat before publishing its domain event.
- Heartbeat metadata includes only safe operational fields:
  `last_success_at`, `heartbeat_at`, `last_failed_at`, `failure_count`, and
  error category.  No token or chat identifier is persisted or returned.
- All health readers use the newest persisted row per component (not a global
  row limit) and the shared worker-health derivation policy.
- The same policy is used by the FastAPI health endpoints, WebSocket health
  envelopes, Telegram `/health`, live status, and dashboard service state.
- TTL is `max(60 seconds, 4 × configured history polling interval)`.  A recent
  successful row is `CONNECTED`; a recent failure or a row stale by one TTL is
  `DEGRADED`; after two TTLs, or with no authoritative row, state is
  `UNKNOWN`.
- Supervisor startup adopts one exact existing command when the registry is
  missing, reports multiple exact matches as `DEGRADED`, and never terminates
  an unverified process.  Control shutdown now stops verified `live` and `api`
  children to prevent orphaned polling workers.

## Files changed

- `services/worker_health.py`
- `services/live.py`
- `services/control.py`
- `services/supervisor.py`
- `persistence/repositories.py`
- `api/app.py`
- `api/realtime.py`
- `frontend/src/pages/OverviewPage.tsx`
- `tests/test_phase201_history_health.py`

## Automated validation

- Full pytest: **113 passed, 2 warnings**.
- Ruff: **passed**.
- Python compileall: **passed**.
- Frontend Vitest: **4 passed**.
- TypeScript: **passed**.
- ESLint: **passed**.
- Production frontend build: **passed**.
- Shadow replay: completed with `Execution: DISABLED`; no order execution was
  added and no `mt5.order_send()` path was introduced.

## Manual live acceptance still required

Do not treat the automated suite as live acceptance.  First stop/restart the
Telegram control process through the approved operator procedure so its
verified supervisor owns exactly one `live` child; do not kill unrelated or
unverified PIDs.  Then leave the Telegram control process running for longer
than several configured history intervals (at least five intervals), and
check repeatedly:

1. Telegram `/health` reports `Worker:History CONNECTED`.
2. `/logs` shows one `HISTORY_SYNC_COMPLETED` per interval, without the
   approximately three-second duplicate pair.
3. `/api/system/health.services.history_worker`, `/api/live/status`, WebSocket
   `system_health`, and the dashboard History Worker pill agree.
4. A deliberate recoverable history failure reports `DEGRADED`, then the next
   successful cycle returns all surfaces to `CONNECTED`.
5. Verify execution remains `DISABLED` and no order-write operation is made.

Record the live PIDs and UTC timestamps in the operator validation log before
declaring Phase 2.0.1 accepted.
