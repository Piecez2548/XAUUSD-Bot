# Phase 2.5 Forward Shadow Validation Report — 2026-09-22

## Final status

**PHASE 2.5 IMPLEMENTED — FORWARD VALIDATION SAMPLE COLLECTING**

The forward-validation path is implemented and remains read-only. It has not
been declared statistically validated: the current runtime was intentionally
not restarted during this change, `FORWARD_SHADOW_ENABLED` remains disabled by
default, and no forward sample has been fabricated or backfilled from
historical research.

## Implementation status

`pair_zone_v1` remains frozen at version `1.0.0`. The authoritative strategy
file hash is:

`fd2d73b9aa0d21004653e455263107caf727ac552fd204486f363cb7c25b7ded`

The new independent forward path now provides:

- a durable `ACTIVE` / `PAUSED` / `COMPLETED` / `ERROR` session manifest;
- an activation timestamp that is persisted before the first signal;
- closed-candle-only M15 pair/zone, H1 context, and M5 confirmation inputs;
- future-only causal virtual outcomes with explicit `TP`, `SL`, `AMBIGUOUS`,
  `EXPIRED`, and `OPEN` states;
- gross/net R accounting with actual spread observation or an explicit
  configured fallback, slippage, and commission fields;
- persisted expired-trade mark price, holding time, MFE, and MAE;
- restart-safe session reuse and deterministic signal/trade identifiers;
- startup strategy-hash drift detection that creates an `ERROR` session and
  does not mutate prior records;
- authoritative `worker:forward_shadow` health and the API, Telegram, and
  dashboard views described below.

## Data and sample status

No forward session was activated during this implementation turn. Therefore:

- forward session: not started;
- forward signals/trades: `0` / `0`;
- live BUY/SELL outcome path: not yet observed;
- historical Phase 2.3/2.4 results: displayed only as a separate reference;
- no historical rows are copied into forward tables.

The historical reference remains the accepted Phase 2.4 Pair Zone evidence:
568 canonical signals, normal-cost net `+46.571R`, expectancy `+0.0821R`,
profit factor `1.171`, and max drawdown `14.592R`. These values are not
aggregated with forward performance.

## Health and freshness policy

Forward health is persisted through the existing `SystemHealthRepository` and
serialized by the shared worker-health policy. The freshness interval is the
configured history polling interval with a minimum four-interval heartbeat
window (`worker_health_ttl`, minimum 60 seconds), so ordinary scheduler jitter
does not turn a healthy worker into `UNKNOWN`.

- `DISABLED`: `FORWARD_SHADOW_ENABLED=false`.
- `CONNECTED`: recent authoritative forward heartbeat.
- `DEGRADED`: recent known failure or one-interval staleness window.
- `ERROR`: strategy drift or repeated/unrecoverable worker failure.
- `UNKNOWN`: no authoritative recent heartbeat exists.

The same persisted source is exposed through `/api/forward/health`, system
health, realtime health payloads, Telegram `/forwardhealth`, and the Forward
Validation dashboard. All views explicitly report execution disabled.

## Duplicate-worker investigation

No duplicate live/history reconciler was found. The process table contains one
supervised `main.py live` command (`data/process_registry.json` PID 6476) and
its normal interpreter child (PID 18164), not two independent live engines.
The analogous control/server pairs are the supervisor launcher and child
processes. The existing supervisor rejects more than one exact command match,
adopts one verified survivor, and has regression coverage for duplicate
prevention.

The approximately three-second event pairs in the supplied evidence are not
evidence of two active reconcilers. They are consistent with adjacent startup,
recovery, or success/failure event writes from the single supervised runtime;
the authoritative persisted worker heartbeat is now the source of truth rather
than event existence.

## API, Telegram, and dashboard

Added read-only endpoints:

- `/api/forward/session`
- `/api/forward/health`
- `/api/forward/signals`
- `/api/forward/trades`
- `/api/forward/performance`

Added Telegram commands `/forward`, `/forwardhealth`, `/forwardtrades`, and
`/forwardperformance`. Added a first-class **Forward Validation** dashboard
page with explicit `LIVE FORWARD SHADOW`, `EXECUTION DISABLED`, and `NO REAL
ORDERS` labels. Historical metrics are visibly marked as reference-only.

## Safety status

- `execution_allowed=false` is persisted and returned by all forward payloads.
- No `mt5.order_send` exists in production Python source.
- No broker write API was added or called.
- `pair_zone_v1` strategy code/configuration was not modified.
- No Phase 2.1 or trade execution work was started.
- Nexus repository was not modified.
- Runtime processes were not terminated or restarted.
- Migration `20260922_0010_forward_shadow.py` was applied to the local database.

## Files changed for Phase 2.5

- `services/forward_shadow.py`
- `persistence/orm.py`
- `alembic/versions/20260922_0010_forward_shadow.py`
- `config/settings.py`, `.env.example`
- `services/live.py`, `services/worker_health.py`, `services/control.py`
- `api/app.py`, `api/realtime.py`
- `frontend/src/pages/ForwardValidationPage.tsx`
- `frontend/src/App.tsx`, `frontend/src/components/AppShell.tsx`,
  `frontend/src/types.ts`, `frontend/src/styles.css`
- `tests/test_phase25_forward_shadow.py`

## Validation results

Final validation results:

- `pytest -q`: **161 passed, 2 warnings**;
- Ruff: **All checks passed**;
- `compileall`: **passed**;
- frontend Vitest: **4 passed**;
- frontend TypeScript: **passed**;
- frontend ESLint: **passed**;
- frontend production build: **passed** (`vite build`, 2,284 modules).

Existing Phase 2.2–2.4 work remains uncommitted as it was before this phase;
no commit, push, tag, or deployment was performed.

## Manual live acceptance procedure

1. Confirm the frozen Pair Zone file hash above before enabling the worker.
2. Set `FORWARD_SHADOW_ENABLED=true` (and leave execution disabled), then use
   the existing operator-controlled runtime restart. This was not performed in
   this turn.
3. Leave the Telegram control process running for longer than several history
   polling intervals (at least 4 × `LIVE_HISTORY_INTERVAL_SECONDS`; with the
   default 30 seconds, observe for at least 3–5 minutes).
4. Compare `/forwardhealth`, `/health`, `/forward`, `/forwardtrades`, and the
   dashboard. They must show the same persisted worker state and session ID.
5. Confirm successive closed M5 observations update the heartbeat, no signal
   has a timestamp at or before the activation boundary, and any signal uses
   only future closed M5 candles for its outcome.
6. Verify `execution_allowed=false`, `NO REAL ORDERS`, zero broker writes, and
   no `PROCESS_CRASHED` events. Keep the control process running during the
   observation window; do not infer acceptance from automated tests alone.
