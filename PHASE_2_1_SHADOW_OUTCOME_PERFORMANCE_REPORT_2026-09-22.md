# Phase 2.1 Shadow Outcome & Performance Evaluation Report

Date: 2026-09-22 (UTC)

## Scope and safety

Phase 2.1 adds read-only, causal evaluation of persisted shadow decisions. It does not alter strategy rules, submit orders, call `mt5.order_send()`, or enable execution. The existing Phase 2 shadow-decision path remains unchanged. The runtime SQLite database was backed up before the additive migration; no database rows were deleted or reset.

## Audit result and root cause

Before implementation, the repository contained shadow decisions and a `PENDING` field but no durable per-decision outcome record, no explicit outcome-worker heartbeat, and no common API/Telegram/dashboard outcome source. Consequently, outcome freshness and performance could not be authoritative or restart-safe. The root cause addressed here is the missing durable outcome/health layer, not a trading-strategy defect.

The implementation now uses:

`shadow_decision -> shadow_outcomes (unique decision + policy) -> worker:shadow_outcome health row -> API/Telegram/WebSocket/dashboard`.

Only closed M5 candles with `timestamp > decision.m5_candle_timestamp` are eligible. The decision candle and all earlier candles are excluded. TP/SL on the same candle is `AMBIGUOUS` because OHLC does not prove intrabar order. A finite horizon produces `EXPIRED` mark-to-market R; insufficient future candles remain `PENDING`.

## Freshness policy

The outcome worker writes an explicit persisted heartbeat on startup and every evaluation cycle. Public state is derived from that row using the shared worker-health policy:

- `CONNECTED`: recent successful heartbeat.
- `DEGRADED`: known recent failure or heartbeat age between one and two TTL windows.
- `UNKNOWN`: no authoritative row or heartbeat older than two TTL windows.
- `DISABLED`: shadow engine disabled.
- `ERROR`: repeated worker/evaluation failure is recorded in the health row.

Outcome TTL is `max(60 seconds, 4 × live candle interval)`, which exceeds the normal polling interval and tolerates scheduler jitter. API `/api/shadow/outcome-health`, system health, WebSocket health events, Telegram `/health` and `/outcomehealth`, and the dashboard all derive from `worker:shadow_outcome`.

## Duplicate-worker investigation

The inspected supervisor registry contained one `api` and one `live` identity, both stopped at audit time; no duplicate live/history worker instance was evidenced. The Phase 2.1 change adds one independently named `shadow-outcome-worker` task inside the single live runtime and does not spawn a second history reconciler. No unrelated process was killed or modified.

## Implementation files

- `persistence/orm.py` — `ShadowOutcomeRecord` and indexes.
- `alembic/versions/20260922_0006_shadow_outcomes.py` — additive migration.
- `persistence/repositories.py` — idempotent outcome repository.
- `services/shadow_outcome.py` — causal evaluator, worker heartbeat, analytics.
- `services/live.py` — lifecycle integration for the independent outcome worker.
- `config/settings.py`, `.env.example` — bounded horizon setting.
- `main.py` — `shadow-evaluate` CLI.
- `api/app.py`, `api/realtime.py` — outcome/health/performance API and WebSocket exposure.
- `services/control.py` — Telegram `/outcome`, `/performance`, `/outcomehealth`, and `/health` status.
- `frontend/src/types.ts`, `frontend/src/pages/ModulePages.tsx` — read-only outcome and performance display.
- `tests/test_phase21_shadow_outcomes.py` — causal, status, horizon, idempotency, recovery-safe and denominator tests.

## Historical evaluation

After creating backup `backups/trading_observatory_20260922T092515.279504Z.db`, migration `20260922_0006` was applied. `python main.py shadow-evaluate` completed successfully:

- Created: 0
- Pending: 0
- Terminal: 0
- Errors: 0
- Execution: DISABLED

The current persisted decision set contained no eligible BUY/SELL decisions requiring outcome rows, so no performance sample was invented.

## Validation results

## Live operator validation evidence

The supplied live acceptance evidence records:

- Initial live start at `2026-09-22 09:31 UTC`.
- Stability observation through `09:38 UTC`.
- Controlled restart at `09:39 UTC`.
- Successful recovery observed through `09:41 UTC`.
- History, Shadow, and Shadow Outcome workers remained `CONNECTED`.
- Shadow decision processing advanced through M5 `09:35 UTC`.
- History cadence remained single at approximately 30 seconds.
- No `PROCESS_CRASHED` event was observed in the supplied acceptance logs.
- Execution remained `DISABLED`.
- No eligible `BUY`/`SELL` decision occurred.

Therefore, the live BUY/SELL outcome path remains **NOT YET OBSERVED**. Live TP/SL/AMBIGUOUS/EXPIRED behavior is not claimed as observed; those paths are covered by the automated deterministic tests only.

- Python regression: `134 passed, 2 warnings` (`.venv\Scripts\python.exe -m pytest -q --disable-warnings`)
- Ruff: passed (`.venv\Scripts\python.exe -m ruff check .`)
- Compileall: passed (`python -m compileall -q .`)
- Frontend Vitest: `2 files, 4 tests passed`
- Frontend TypeScript: passed (`npm run typecheck`)
- Frontend ESLint: passed (`npm run lint`)
- Frontend production build: passed (`npm run build`, Vite completed)
- Additive migration on a temporary SQLite database: passed through `20260922_0006`
- Tracked-file secret scan: no credential values found; only configuration key names/documentation references were matched. Runtime `.env`, SQLite DB, process registry, Telegram offset, logs, backups, `node_modules`, and `dist` remain ignored/untracked.

## Manual live acceptance procedure

1. Confirm `.env` has `SHADOW_ENGINE_ENABLED=true`, execution remains disabled, and Telegram control identities remain deny-by-default.
2. Start the existing Telegram control process and the normal supervised API/live runtime; do not start a second live process manually.
3. Leave the control process running for at least 5–10 outcome polling intervals (and longer than the configured horizon if validating terminal outcomes).
4. Check Telegram `/health` and `/outcomehealth`; API `/api/system/health`, `/api/live/status`, `/api/shadow/outcome-health`, `/api/shadow/outcomes`, and `/api/shadow/performance`; WebSocket `system_health`; and the dashboard Shadow page.
5. Verify all surfaces agree on the same `worker:shadow_outcome` state and heartbeat timestamp, that repeated cycles remain `CONNECTED`, and that stale/failure/recovery transitions are truthful.
6. Verify `data/shadow_outcomes` remains additive/idempotent across a controlled restart and that no order endpoint or broker write API is called.

PHASE 2.1 LIVE INFRASTRUCTURE ACCEPTED —
LIVE BUY/SELL OUTCOME PATH NOT YET OBSERVED —
EXECUTION DISABLED
