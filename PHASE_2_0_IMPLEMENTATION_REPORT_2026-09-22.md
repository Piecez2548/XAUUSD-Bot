# Phase 2.0 Shadow Decision Engine Report — 2026-09-22

## 1. Scope and safety

Phase 2.0 adds deterministic shadow analysis only. The input boundary is an
immutable `MarketSnapshot`; the output boundary is an immutable
`ShadowDecision`. Decision, feature, and risk-gate modules do not import
MetaTrader5 and no broker write operation exists. `execution_allowed` is
persisted and returned as `false`; execution remains **DISABLED**.

## 2. Pipeline

`completed MarketSnapshot -> deterministic features -> H4/H1/M15/M5 context ->
baseline strategy -> independent shadow risk gate -> ShadowDecision -> append-only
database/API/Telegram/dashboard`.

The live engine submits completed account-cycle snapshots to a bounded async
shadow worker. A worker failure records `shadow_engine=DEGRADED` and does not
stop MT5 observation. At most one decision is persisted for each
`symbol + M5 closed candle + strategy_version`.

## 3. Deterministic rules

- Only closed bars are analysed; the latest snapshot bar is excluded as the
  forming candle. Required timeframes are M5, M15, H1, and H4.
- Features include OHLC structure, returns, true range, ATR, EMA, EMA slope,
  price-vs-EMA, swing highs/lows, higher/lower structure, range, body/wicks,
  body-to-range, spread, and relative volatility.
- H4 and H1 must agree on directional bias from EMA/slope and higher-high /
  higher-low or lower-high / lower-low structure.
- M15 setup and M5 timing must agree with that bias. Conflicts become
  `NO_TRADE` with machine-readable reason codes.
- Baseline target is an explicit 2R projection; configurable minimum RR defaults
  to 2.0. Invalid levels or lower RR become `NO_TRADE`.
- High- and low-volatility regimes are filtered conservatively to `NO_TRADE`.
- Market regimes are deterministic: `TREND_UP`, `TREND_DOWN`, `RANGE`,
  `HIGH_VOLATILITY`, `LOW_VOLATILITY`, or `UNCERTAIN`.

## 4. Independent risk gate

The gate blocks MT5/runtime non-connected state, non-live data, unknown or
unbounded existing risk, invalid stops, excessive spread, invalid sizing,
minimum-lot-over-budget, per-trade risk above 2%, or aggregate risk above 6%.
Volume is calculated from broker tick size/value and equity, then normalized
downward to the broker volume step. No rounding can increase the approved risk.

## 5. Persistence and API

Migration `20260922_0005_shadow_decisions` adds append-only `shadow_decisions`
with snapshot/candle correlation, strategy version, feature context, levels,
RR, risk gate, reason codes, confidence, and `PENDING` outcome status.

Read-only endpoints:

- `GET /api/shadow/decision`
- `GET /api/shadow/decisions`
- `GET /api/shadow/summary`

No secret or execution capability is returned.

## 6. Telegram and dashboard

Telegram commands `/decision`, `/shadow`, and `/strategy` expose the latest
decision, bounded counts, and exact baseline rules. Signal notifications are
configurable and default on for BUY/SELL, off for NO_TRADE; every signal says
`SHADOW ONLY — NO ORDER SENT`.

The dashboard includes a Shadow Trading page with latest decision, regime,
strategy, entry/SL/TP, RR, hypothetical volume, risk, reason codes, snapshot,
recent decisions, and an unmistakable `EXECUTION: DISABLED` state. No order
buttons were added.

## 7. Replay smoke test

`python main.py shadow-replay` processed persisted closed-candle snapshots in
chronological order:

- Candles processed: **386**
- Decisions generated: **15**
- BUY: **0**
- SELL: **0**
- NO_TRADE: **15**
- Errors: **0**
- Duplicates skipped: **371**

This is a determinism/data-path smoke test, not optimization and not a
profitability report.

## 8. Validation

- Python: **109 passed, 2 warnings**
- Ruff: **passed**
- `compileall`: **passed**
- Frontend Vitest: **4 passed**
- Frontend TypeScript: **passed**
- Frontend ESLint: **passed**
- Frontend production build: **passed**
- Migration `20260922_0005`: **applied successfully**
- Source scan: no `order_send`, trade action, shell execution, `eval`, or
  `exec` in production code.

## 9. Manual Phase 2 validation

```powershell
cd "D:\Project_001\Nexus-Project\XAUUSD Bot"
python main.py migrate
python main.py shadow-replay
python main.py control
```

Restart the supervised children with Telegram `/restart`, then observe several
closed M5 cycles. Verify `/decision`, `/shadow`, the dashboard Shadow Trading
page, and `/api/shadow/decision` agree on the same persisted decision and
snapshot. Confirm that any BUY/SELL output remains hypothetical and that no
broker order appears. Keep a manually opened demo position out of the engine's
control path; this phase never opens, modifies, closes, or cancels it.

## 10. Limitations and completion gate

Outcome evaluation remains `PENDING`; no fake SL/TP performance is generated.
The baseline is intentionally conservative and has not been optimized. The
phase is **not live validated** until the operator observes several connected
Demo MT5 M5 cycles.

Phase 2.1 was not started. Execution remains **DISABLED**.
