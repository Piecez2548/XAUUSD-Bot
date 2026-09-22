# Phase 2.0.3 — Shadow Catch-up, Ordering & Health Truth Fix

Date: 2026-09-22  
Scope: read-only shadow analysis only. Trading strategy and order execution were not changed.

## Root cause

The latest-decision readers ordered `shadow_decisions` by `created_at`. Catch-up legitimately persisted older market candles after newer candles. In the supplied live database the relevant rows were:

| M5 candle event time | created/insert time | strategy | snapshot | row id |
|---|---|---|---|---|
| 2026-09-22 04:25 UTC | 06:51:40.425741 UTC | baseline_v1 | a4e3dd79-3b42-4c86-b521-d8ddb7220e74 | 946acccb-122b-40d9-a306-52fa589cf798 |
| 2026-09-22 05:05 UTC | 06:55:52.600280 UTC | baseline_v1 | 3622f4e4-2ae7-40f8-b0af-2e68afd28e72 | b240706c-e2d6-494e-9206-9ceb9c659600 |
| 2026-09-22 05:35 UTC | 05:43:20.964552 UTC | baseline_v1 | 7c085819-6262-4064-ad1d-1e8be45c04aa | 4d3f47d6-8e3d-42fa-98d4-a06bab9a3116 |
| 2026-09-22 06:35 UTC | 06:43:05.266874 UTC | baseline_v1 | aecc2123-4b99-4cb0-924e-863f5415e7b5 | c2241004-a614-47c0-b6c5-565e0204291b |

Thus a 04:25 row inserted at 06:51 could displace 06:35 in `/decision`, Telegram, and dashboard readers. The second truth issue was `queue_depth` adding deferred work to the bounded queue size, producing `15` for a queue whose configured capacity is `8`.

## Implemented policy

- Latest decision and history order by `(m5_candle_timestamp DESC, created_at DESC, id DESC)`.
- Three monotonic logical watermarks are persisted in `worker:shadow` metadata: `latest_available_m5`, `latest_received_m5`, `latest_processed_m5`; `latest_decision_m5` is also exposed.
- `queue_depth` is the actual bounded queue size only (`0..8`). `deferred_count`, `catchup_pending_count`, and `total_backlog` are separate fields.
- Catch-up is sorted ascending by M5 event time, identifies existing decision keys in one bulk query, deduplicates candidate keys, and enqueues only a small batch per pass so live work retains priority.
- States are `CONNECTED`, `CATCHING_UP`, `DEGRADED`, `ERROR`, `UNKNOWN`, or `DISABLED`. A recent successful heartbeat with pending work is `CATCHING_UP`; a stalled pending backlog is `DEGRADED`; repeated failures (`>=3`) are `ERROR`; no authoritative heartbeat is `UNKNOWN`.
- The prior worker metadata is restored at startup, and new health metadata is monotonic rather than replacing known watermarks with null values.
- Snapshot reconstruction remains temporally bounded (`CandleRecord.timestamp <= market snapshot timestamp`), so catch-up has no look-ahead. Missing context remains a deterministic NO_TRADE path.
- WebSocket health derives shadow-worker freshness through the same worker-health policy. The dashboard reads the same API fields and displays event-time M5 timestamps.
- Process supervision now refuses to adopt or spawn when multiple matching live processes are detected; it records `DEGRADED` and leaves processes untouched for explicit operator cleanup.

## Duplicate-process investigation

Duplicate live processes were present during inspection: one system-Python and one `.venv` `main.py live` process, with corresponding duplicate `control` and `server` processes. The lifecycle root cause is two interpreter environments being launched outside one supervisor registry; the persisted registry tracked only the `.venv` PIDs. No process was killed and no database row was deleted. The supervisor guard now prevents silently coexisting matches on future start/restart operations.

## Files changed

- `services/shadow_service.py`
- `services/worker_health.py`
- `services/supervisor.py`
- `persistence/repositories.py`
- `api/app.py`
- `api/realtime.py`
- `services/control.py`
- `frontend/src/types.ts`
- `frontend/src/pages/ModulePages.tsx`
- `tests/test_phase20_shadow.py`

## Tests added/updated

The Phase 2 shadow suite now covers event-time ordering, monotonic watermarks, bounded queue/deferred accounting, catch-up state transitions, stale-progress degradation, repeated-failure error, successful recovery, API health payload fields, Telegram/WebSocket agreement, restart/idempotency, closed-candle ordering, no-new-data heartbeat behavior, failure recovery, duplicate decision keys, and execution-disabled safety. The suite contains 28 passing tests.

## Validation results

- Backend full pytest: **128 passed, 2 warnings**.
- Ruff: **passed**.
- Python compileall: **passed**.
- Shadow replay: **1000 candles processed, 23 decisions generated, 977 duplicates, 0 errors; all 23 NO_TRADE; Execution DISABLED**.
- TypeScript: **passed** (`tsc --noEmit`).
- ESLint-equivalent project lint: **passed** (`npm run lint`, oxlint).
- Production build: **passed** (`npm run build`, Vite; 4672 modules transformed).
- Frontend Vitest targeted smoke: **1 file / 5 tests passed** (`src/App.test.ts`). A full unfiltered Vitest run was started but did not terminate in the available validation window; no frontend test failure was reported.

## Manual live acceptance still required

Do not declare the live issue closed from automated tests alone. First use the existing control process to remove the unverified duplicate runtime only after operator confirmation, then restart control once through the supervisor. Leave the Telegram control process running for longer than several configured history polling intervals. Observe `/shadowhealth`, `/health`, `/decision`, `/shadow`, the API, WebSocket, and dashboard while at least five new M5 candles close. Confirm:

1. `State` reaches `CONNECTED` after backlog reaches zero (or stays `CATCHING_UP` with forward progress).
2. `Queue` never exceeds `8`; deferred/catch-up/total backlog are separately visible.
3. Latest received, processed, decision, and `/decision` M5 event timestamps never move backwards.
4. History polling produces one logical terminal decision per symbol/M5/strategy.
5. No process crashes, duplicate `HISTORY_SYNC_COMPLETED` cadence, order writes, or `mt5.order_send()` calls occur.

Execution remains disabled and Phase 2 shadow-decision behavior is preserved.
