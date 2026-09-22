# Phase 1.7.1 Implementation Report — 2026-09-22

## 1 Root cause

The live child PIDs were present and the account/tick/history worker records
continued advancing, so this was not a supervisor-only display issue. Database
inspection showed that the running child was an older process started before
the Phase 1.7.1 account-cycle changes: `live_runtime` stopped emitting a
heartbeat after startup, there were no persisted `mt5` health rows, market
snapshots had migration defaults rather than verified observation metadata, and
risk rows had `market_snapshot_id = NULL`. The child was alive but its runtime
state and persisted snapshot path were not authoritative for consumers.

The account worker also previously persisted account and risk through separate
`persist_live_*` methods. It did not persist the refreshed position set or
close disappeared `PositionRecord` rows. Telegram `/positions` and the API
therefore read the old open-position row while `/risk` read a newer risk row.
MT5 `CONNECTED` was represented primarily by a transient event and could
remain `UNKNOWN` after that event aged out. Consumers independently selected
latest risk/market rows instead of one completed shared snapshot.

## 2 Sources of truth discovered

`mt5.positions_get(symbol)` is mapped by `mt5.positions.read_open_positions`;
`None` raises `PositionReadError`, while `()` is a verified empty set. The Live
Data Engine owns polling and in-memory state. `SnapshotRepository` persists
current positions, position snapshots, account snapshots, market snapshots, and
risk. Telegram reads those persisted records. FastAPI reads the same records;
the React dashboard consumes FastAPI and the existing WebSocket/event paths.
History reconciliation stores broker deal facts separately and reconstructs
completed trades only when entry and exit facts are verified.

## 3 Stale-state fix

The live account cycle now builds one `MarketSnapshot` with the latest account,
positions, symbol, tick, and candles, calculates risk from it, and calls
`SnapshotRepository.persist(snapshot, risk)`. The transaction marks any
position missing from a successful empty MT5 result with `closed_observed_at`,
so it no longer appears as current. MT5 read errors still raise and preserve
the last verified state; they are never converted to zero positions. A new
additive migration records `positions_observed_successfully` and
`open_position_count` on each market snapshot, including verified empty sets,
without creating fake position rows.

## 4 Runtime, MT5, and database truth

The live watchdog now appends a bounded `live_runtime` heartbeat containing
state, timestamp, PID, symbol, and last verified observation timestamps. API
and Telegram control treat a missing/stale heartbeat as `UNKNOWN`/`DEGRADED`,
never as healthy merely because a process PID exists. A verified initialize/read
cycle records `mt5=CONNECTED`; read failures record `DISCONNECTED`,
`RECONNECTING`, or `DEGRADED`, and a later successful read records recovery.

All SQLite relative URLs are normalized against the absolute application root
before engine creation, migration, or child-process launch. Health responses
expose only a non-sensitive `database_identity` hash, never credentials or
Telegram secrets, allowing operators to verify that control, API, live, and
CLI use the same store.

## 5 Snapshot consistency design

Account, positions, and risk now share one market snapshot ID and observation
timestamp. `PositionSnapshotRecord.market_snapshot_id` and
`RiskSnapshotRecord.market_snapshot_id` identify the same cycle. Telegram,
FastAPI, and the dashboard expose `observed_at`, snapshot ID, and freshness.
Consumers select the latest completed shared snapshot by ID; a newer incomplete
write does not create a hybrid view. If no complete snapshot exists,
`STATE_SYNC_PENDING` is returned.

## 6 Risk calculation verification

Risk remains read-only and uses broker-provided symbol metadata (`trade_tick_size`
and `trade_tick_value_loss`) without hard-coded XAUUSD pip economics. BUY stops
must be below entry and SELL stops above entry; invalid-side or missing stops
are `UNBOUNDED/UNKNOWN`, never zero. Deterministic BUY and SELL tests verify
loss-producing stop calculations and conservative unknown-risk behavior.

## 7 Files changed

- `services/live.py`: atomic live account/position/risk persistence
- `services/risk.py`: protective stop-side validation
- `services/control.py`: coherent position/risk responses with timestamps and shared snapshot lookup
- `api/app.py`: complete-snapshot lookup, position status, and freshness metadata
- `persistence/orm.py`, `persistence/repositories.py`, `alembic/versions/20260922_0004_position_observation.py`: explicit position observation state
- `persistence/database.py`, `persistence/migrations.py`: deterministic SQLite resolution and safe database identity
- `frontend/src/types.ts`, `frontend/src/pages/OverviewPage.tsx`, `frontend/src/pages/ModulePages.tsx`
- `tests/test_phase171_live_consistency.py`, `tests/test_phase17_control.py`, `tests/test_readers.py`
- this report

The additive `20260922_0004` migration is backward-safe for existing databases;
the existing market snapshot foreign key remains the correlation mechanism.

## 8 Tests added

Added deterministic coverage for verified empty positions, explicit zero
observation state, stale row removal, atomic snapshot IDs, Telegram
zero-position output, latest coherent snapshot selection, API/dashboard
consistency, live account-cycle close behavior, BUY/SELL risk, unknown stops,
MT5 empty tuple handling, and MT5 read failure handling.
Additional regression coverage verifies missing/stale and fresh runtime
heartbeats, MT5 failure/recovery, deterministic database identity across
working directories, atomic rollback on snapshot failure, sanitized worker
failure health, and bounded `/start` verification.

## 9 Exact regression results

- Python: **94 passed, 2 warnings**
- Ruff: **All checks passed**
- `compileall`: **passed**
- Frontend Vitest: **4 passed**
- Frontend TypeScript: **passed**
- Frontend ESLint: **passed**
- Frontend production build: **passed**

Warnings are existing Starlette/httpx and AnyIO deprecation warnings only.

## 10 Manual validation procedure/results

No live MT5 or Telegram credentials were used in automated validation. Run:

```powershell
cd "D:\Project_001\Nexus-Project\XAUUSD Bot"
python main.py migrate
python main.py control
```

Restart the supervised children after deploying this change (for example use
Telegram `/restart`) so the live child loads the heartbeat and atomic-cycle
code. Then wait for a successful account cycle and verify:

```text
/status    -> MT5 CONNECTED, Live Engine RUNNING
/health    -> Live Runtime CONNECTED, MT5 CONNECTED
/positions -> Open positions: 0, Freshness: LIVE, Snapshot: <id>
/risk      -> Bounded positions: 0, Unbounded positions: 0,
              Risk state: WITHIN LIMIT, Freshness: LIVE, Snapshot: <same id>
```

With no open XAUUSD position, wait for one successful account/position cycle,
then run `/positions` and `/risk`. Both should show zero, the same observed
cycle, and `Freshness: LIVE`. Repeat with a manually opened demo position
protected by SL; both commands should show the same snapshot ID and coherent
bounded risk. Close it manually, wait one cycle, and verify both return zero.

## 11 Remaining limitations

The optional authoritative `order_calc_profit` path is not invoked because the
current risk service receives immutable snapshots rather than a live MT5 API
handle; broker symbol metadata remains the explicit fallback. Live broker timing
and external Telegram delivery still require operator validation. Historical
trade reconstruction remains separate from current open positions.

## 12 Completion gate and execution safety

**AUTOMATED VALIDATION PASSED — LIVE OPERATOR VALIDATION REQUIRED**

The live MT5 + Telegram acceptance sequence above has not been rerun by this
automated implementation turn, so the issue is not declared fully fixed until
that operator check succeeds.

Confirmed: no Phase 2 work, AI decisions, order submission, position
modification, or `mt5.order_send()` was added. The application remains strictly
read-only.
