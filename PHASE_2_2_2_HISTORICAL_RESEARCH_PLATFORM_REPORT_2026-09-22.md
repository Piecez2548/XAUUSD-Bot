# Phase 2.2.2 Historical Research Platform

## Final status

**PHASE 2.2.2 IMPLEMENTED — MANUAL RESEARCH VALIDATION REQUIRED**

This phase is implemented as an isolated, read-only historical research and
persistent backtest platform. It does not change the live Shadow strategy
contract, the Phase 2.1 outcome worker, or any execution path.

## Scope and architecture

The research path is:

`operational closed candles -> research-import -> immutable dataset manifest + copied candles -> backtest run -> persisted decisions/outcomes/metrics -> FastAPI -> Strategy Research dashboard`

Operational candles are normalized to UTC, sorted chronologically, validated
for OHLC geometry, deduplicated deterministically by timestamp, and hashed with
SHA-256. Gaps are recorded as gaps; no synthetic candle filling is performed.
Dataset provenance records the source table and source row identifiers.

The additive migration `20260922_0008_research_platform.py` adds:

- `research_datasets` and `research_candles`
- `research_runs`
- `research_decisions`
- `research_outcomes`
- `research_metrics`

Run manifests persist strategy/config identity, dataset hash, UTC range, split
definition, RR parameter, engine version, git commit/dirty state, status, and
execution permission. Runs are `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, or
`INTERRUPTED`; startup recovery marks abandoned queued/running runs
`INTERRUPTED`. NO_TRADE rows never receive fabricated trade levels.

The default backtest evaluates the explicit RR grid `[1.0, 1.5, 2.0, 2.5,
3.0]`; `--rr` runs one deterministic value. Outcome evaluation reuses the
Phase 2.1 causal TP/SL/AMBIGUOUS/EXPIRED semantics and a separate research
policy identifier.

## Interfaces

CLI:

- `python main.py research-import --timeframe M5`
- `python main.py backtest --strategy trend_pullback_v1`
- optional `--dataset`, `--rr`, `--split`, `--run-name`, and `--output`

Telegram control remains deny-by-default and read-only. `/backtest
trend_pullback_v1` validates the strategy, persists a queued job, and schedules
the bounded local job without shell execution. `/backtests` lists persisted
runs, `/dashboard` returns `DASHBOARD_PUBLIC_URL`, and existing commands are
preserved. Research worker health is persisted as `worker:research` without
credentials or arbitrary exception text.

FastAPI endpoints:

- `/api/research/datasets`
- `/api/research/runs`
- `/api/research/runs/{run_id}`
- `/api/research/runs/{run_id}/metrics`
- `/api/research/runs/{run_id}/trades`
- `/api/research/runs/{run_id}/funnel`
- `/api/research/compare`

The dashboard now has a first-class `/research` route showing immutable
datasets, run status, funnel counts, and the explicit `DISABLED` execution
banner. No Telegram token, chat ID, MT5 credential, or API key is returned by
these endpoints or rendered in the UI.

## Local smoke evidence

The existing local database was migrated additively. The import command
completed with dataset `dataset_6405d89ccacb`, hash
`6405d89ccacbc37aea26b43c1e6cd87fb7afccc916e2b25ecfc53c75e6d1c888`, XAUUSD
M5, 1,012 valid closed rows, and no invalid/conflicting rows. A deterministic
trend-pullback run completed with 67 unique snapshots, 0 BUY, 0 SELL, and 67
NO_TRADE. Orders sent and broker writes were both 0.

The available local range is short for robust development/validation/holdout
research. The persisted split policy therefore reports
`INSUFFICIENT_DATA_FOR_HOLDOUT_VALIDATION` and does not claim statistical
validation.

## Validation

- Backend pytest: **141 passed, 2 warnings**
- Ruff: **passed**
- Python compileall: **passed**
- Frontend Vitest: **4 passed**
- Frontend TypeScript: **passed**
- Frontend ESLint: **passed**
- Frontend production build: **passed**
- No order execution API was imported or called by the research platform.
- No `mt5.order_send()` was added.
- Execution remains **DISABLED**.

Added regression coverage includes immutable/deduplicated import, chronological
split and walk-forward windows, API dataset exposure without secrets, and the
existing full Phase 1–2.2.1 regression suite.

## Manual research acceptance plan

1. Leave the Telegram control process running and verify `/health` remains
   independent of research worker state.
2. Run `python main.py research-import --timeframe M5` and record dataset ID,
   hash, UTC range, row count, and any gap/duplicate metadata.
3. Run `python main.py backtest --strategy trend_pullback_v1 --rr 2.0` and
   confirm the run reaches `COMPLETED` in `/backtests`.
4. Confirm `/api/research/runs` and `/api/research/runs/{id}` show the same
   run ID, dataset hash, strategy/config identity, and `execution_allowed:false`.
5. Confirm `/api/research/runs/{id}/metrics`, `/trades`, and `/funnel` agree
   with the Telegram summary and dashboard.
6. Repeat the exact command; verify it creates a new explicit immutable run and
   does not mutate the earlier run or dataset.
7. Exercise `/backtest trend_pullback_v1`; verify it returns a job ID promptly,
   `/backtests` initially shows `QUEUED`/`RUNNING`, then `COMPLETED` or a
   truthful `FAILED` state.
8. Restart the control process during a deliberately running research job and
   verify the abandoned run becomes `INTERRUPTED` on the next startup.
9. Verify Telegram, API, WebSocket health, and dashboard continue to report
   live Shadow/MT5/History health from their existing authoritative sources.
10. Verify no new broker order, position, balance, or execution event exists;
    `Orders sent = 0`, `Broker writes = 0`, and `Execution = DISABLED`.
11. Run the RR grid and verify each RR is persisted as an independent run with
    deterministic counts and no parameter optimization.
12. Inspect the working tree and safety scan; confirm no `.env`, database,
    runtime log, token, credential, or secret is tracked.

## Safety and repository state

- Nexus repository modified: **NO**
- Vercel deployed: **NO**
- Git commit created: **NO**
- Git tag created: **NO**
- Strategy behavior changed: **NO** (research only)
- Execution enabled: **NO**
- Broker writes: **NONE**
- Orders sent: **0**

The working tree is intentionally left with the Phase 2.2.2 implementation and
report uncommitted for review, as requested.
