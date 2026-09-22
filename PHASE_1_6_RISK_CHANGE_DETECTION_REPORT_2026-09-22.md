# Phase 1.6 Risk Change-Detection Fix — 2026-09-22

## 1. Root cause

`LiveDataEngine._account_loop` persisted risk every configured interval and also published `RISK_SNAPSHOT_CREATED` every cycle. The existing Telegram subscriber correctly delivered each event, so unchanged telemetry became repeated alerts.

## 2. Files changed

Changed `services/risk.py` and `services/live.py`; added focused regression coverage in `tests/test_phase16_live.py`. No Phase 2 files or execution paths were added.

## 3. Risk comparison policy

`normalize_risk_state` compares bounded aggregate risk, remaining budget, open/bounded/unbounded counts, aggregate state, hard-limit state, and sorted per-position ticket/bounded/risk state. Volatile timestamps, IDs, and correlation metadata are excluded.

## 4. Float tolerance chosen and why

The explicit tolerance is `0.01` percentage points. Differences below this threshold are treated as equity-driven/floating-point noise; differences at or above it remain meaningful and can emit an event.

## 5. Snapshot persistence policy

Risk is still recalculated and persisted every account/risk cadence, so `/api/risk/current`, dashboard values, and freshness remain current even when no alert is emitted.

## 6. Domain-event policy

The initial synchronized risk snapshot is emitted once. Subsequent `RISK_SNAPSHOT_CREATED` events are emitted only when normalized economic state changes. Existing unbounded/bounded transition events remain transition-driven.

## 7. Telegram notification policy

Telegram receives the initial snapshot and meaningful risk events only. Identical five-second polling cycles are silent; `RISK_UNBOUNDED`, `POSITION_WITHOUT_STOP_LOSS`, and recovery transitions remain available for alerting.

## 8. Tests added

Added tests for initial/unchanged cycles, event/Telegram silence, position open/close, bounded↔unbounded, meaningful limit crossing, float tolerance, idempotent history, and fresh current-risk API results.

## 9. Exact regression results

Python: `58 passed, 2 warnings`. Ruff: `All checks passed`. Compileall: passed. Frontend validation remains green from the Phase 1.6 run: 4 tests passed, TypeScript passed, ESLint passed, and production build passed.

## 10. Manual verification still required

Repeat the live MT5 smoke test with the connected terminal: confirm one initial risk alert, no repeated unchanged alerts over several account intervals, and alerts on position/SL/limit transitions. Execution remains disabled.
