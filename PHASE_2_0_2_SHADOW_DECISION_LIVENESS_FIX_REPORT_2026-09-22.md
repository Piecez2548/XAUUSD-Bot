# Phase 2.0.2 Shadow Decision Liveness Fix — 2026-09-22

## 1. Exact root cause

The live candle worker persisted new closed M5 candles but did not refresh
`LiveDataEngine.state.candles`.  Account snapshots therefore continued to
submit an old candle window to the shadow worker.  In addition,
`read_completed_candles()` already starts at MT5 position 1 (closed-only), but
the shadow feature extractor removed the newest element again.  The effective
decision key stayed on an older M5 candle, so the existing candle-level
idempotency correctly returned the prior `NO_TRADE` instead of creating a new
decision.  This was a liveness/correlation defect, not excessive strategy
selectivity.

## 2. Persisted M5 timestamps after 05:35 UTC

The live database contained the following M5 candle records after the
reported 05:35 candle:

`05:35, 05:40, 05:45, 05:50, 05:55, 06:00, 06:05, 06:10, 06:15 UTC`.

At the evidence time through 06:04, the relevant records were present through
06:00.  No candles were fabricated.

## 3. Snapshot correlation findings

Snapshot `7c085819-6262-4064-ad1d-1e8be45c04aa` was generated at
05:43:20 UTC and had coherent account and risk rows referencing the same
snapshot.  Its decision candle was 05:35 UTC, while the persisted snapshot
window's newest M5 was 05:40 UTC.  Later account snapshots remained coherent
but repeatedly reused an old M5 window (observed 05:40 while candle records
continued advancing).  The defect was stale candle-window state, not account
or risk-row correlation.

## 4. Closed-candle indexing findings

- MT5 `read_completed_candles()` calls `copy_rates_from_pos(..., 1, count)`;
  its returned newest element is already the latest closed candle.
- Live shadow inputs are now explicitly marked `candles_are_closed=True` and
  use the newest element without a second exclusion.
- Snapshot/replay candle windows are normalized to closed-only data.
- The original forming-bar behavior remains covered for callers that provide a
  forming candle and omit `candles_are_closed=True`.

## 5. Idempotency findings

The database uniqueness constraint already uses the required logical key:

`symbol + m5_candle_timestamp + strategy_version`.

Snapshot IDs, account timestamps, and mutable values do not participate in
deduplication.  Duplicate submissions for the same key are skipped, while a
new M5 candle is processed even when the previous decision was `NO_TRADE`.
Replay uses the same logical key and does not persist replay rows that could
block future live candles.

## 6. Queue findings

The former bounded queue dropped its oldest item when full.  The worker now:

- keeps the queue bounded at eight items;
- records `QueueFull` as `worker:shadow=DEGRADED`;
- defers the logical key instead of silently discarding it;
- catches up from durable market snapshots in chronological order;
- survives per-decision strategy/persistence exceptions;
- retries missing terminal decisions during catch-up.

## 7. Worker-health design

The authoritative component is `worker:shadow`.  Its safe metadata includes
heartbeat time, received/processed/decision candle timestamps, queue depth,
failure count, error category, and `execution_allowed=false`.  No secrets are
stored or exposed.

The state is exposed through Telegram `/health`, `/shadowhealth`, FastAPI
`/api/system/health`, `/api/shadow/health`, WebSocket health envelopes, and
the dashboard.  `NO_TRADE` remains a strategy decision; worker state is shown
separately.

## 8. Stall-detection policy

The worker heartbeat remains healthy when no new market candle exists.  When a
new candle has been received but the last successful processing progress does
not advance for the bounded stall TTL (`max(60s, 4 × account-cycle interval)`),
the worker is `DEGRADED`.  A processing exception is also recorded as
`DEGRADED`.  Recovery of a subsequent valid candle returns the worker to
`CONNECTED`.

## 9. Catch-up/recovery design

On start and after queue processing, the worker reads all persisted snapshots,
orders eligible M5 candles chronologically, checks the logical idempotency
key, and enqueues unseen candles.  Restart therefore catches up intermediate
candles rather than jumping directly to the newest candle.

## 10. Files changed

- `services/live.py`
- `services/shadow_engine.py`
- `services/shadow_service.py`
- `services/shadow_replay.py`
- `services/worker_health.py`
- `persistence/repositories.py`
- `api/app.py`
- `api/realtime.py`
- `services/control.py`
- `frontend/src/pages/OverviewPage.tsx`
- `frontend/src/pages/ModulePages.tsx`
- `frontend/src/types.ts`
- `tests/test_phase16_live.py`
- `tests/test_phase17_control.py`
- `tests/test_phase20_shadow.py`

## 11. Tests added

Coverage includes closed-only indexing, five consecutive terminal `NO_TRADE`
decisions, duplicate/idempotent processing, queue-full observability,
strategy-exception recovery, chronological/idempotent restart catch-up,
heartbeat-without-new-data, new-data stall detection, API/Telegram/WebSocket
agreement, `/shadowhealth`, and execution safety.

## 12. Exact regression results

- Full pytest: **123 passed, 2 warnings**
- Ruff: **passed**
- `python -m compileall .`: **passed**
- Frontend Vitest: **4 passed**
- TypeScript: **passed**
- ESLint: **passed**
- Production build: **passed**

## 13. Replay results

`python main.py shadow-replay` completed successfully:

- Candles processed: 1000
- Decisions generated: 23
- BUY: 0
- SELL: 0
- NO_TRADE: 23
- Errors: 0
- Duplicates: 977
- Execution: **DISABLED**

No strategy thresholds were loosened and no BUY/SELL was forced.

## 14. Safety/source scan

No production `mt5.order_send()` call exists.  The only matching text is the
negative safety assertion in `tests/test_connection_and_safety.py`.  No market
orders, pending orders, position modification, closing, cancellation, or
LLM execution authorization was added.

## 15. Manual live acceptance procedure

Automated tests do not constitute live validation.  After deploying/restarting
the updated runtime:

1. Start exactly one verified Telegram control process.
2. Confirm exactly one verified live runtime and one history cadence.
3. Confirm `/health` shows `Worker:History CONNECTED` and
   `Worker:Shadow CONNECTED`.
4. Record `/shadowhealth`.
5. Leave the control process running without restart across at least five new
   closed M5 candles.
6. Recheck `/decision`, `/shadow`, `/shadowhealth`, `/health`, API health,
   WebSocket health, and the dashboard after each candle.
7. Confirm latest received/processed/decision M5 timestamps advance one candle
   at a time, consecutive `NO_TRADE` decisions are persisted, no duplicate
   logical candle exists, no `PROCESS_CRASHED` event appears, and no broker
   order appears.

## 16. Known limitations

The current live processes were not forcibly terminated during this change.
The previous duplicate-runtime evidence therefore requires a controlled
operator restart and five-candle acceptance run before declaring live Phase
2.0.2 validated.

## 17. Execution confirmation

Phase 2.0.2 remains shadow-only. `execution_allowed` is permanently false and
execution remains **DISABLED**.
