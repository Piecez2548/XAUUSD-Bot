# Phase 2.2.2 Manual Research Validation — 2026-09-22

## Final status

**PHASE 2.2.2 ACCEPTED — RESEARCH SAMPLE SUFFICIENT**

This status means the immutable historical dataset contains enough canonical
BUY/SELL signals for statistical evaluation. It does **not** mean that
`trend_pullback_v1` is profitable or approved for live trading. Execution
remained disabled throughout (`execution_allowed=false`, orders sent `0`,
broker writes `0`). No strategy thresholds were changed.

## Implementation status

The research runner now performs one canonical strategy pass and reuses the
immutable signal intents across each RR value. NO_TRADE rows are represented by
an aggregate rejection funnel; only BUY/SELL decisions and their outcomes are
persisted. RR copies receive independent decision identifiers while retaining
the same signal timestamp and entry/stop geometry. Outcome evaluation uses a
bounded causal future horizon and does not scan beyond the configured horizon.

Abandoned-run recovery was executed before the benchmark and before the full
grid. It recovered `0` rows; no `QUEUED` or `RUNNING` rows remain. All 24
completed runs, including earlier immutable runs, were preserved.

## Dataset quality

Primary dataset:

- Dataset ID: `dataset_c1d7c37fc364`
- SHA-256: `c1d7c37fc364e43224099b198c9fda513e9c216047ae0c506acef9abcf41dd5b`
- Symbol/timeframe: `XAUUSDm` / M5
- Range: `2026-01-08T13:40:00Z` to `2026-09-22T12:05:00Z`
- Total candles: `50,000`
- Valid candles: `50,000`
- Duplicates: `0`
- Conflicts: `0`
- Invalid OHLC rows: `0`
- Gap/discontinuity count: `183`
- Missing interval count: `23,998`
- Approximate trading-day coverage: `183.5`

The connected terminal returned approximately 8.5 months at the bounded
50,000-candle request. A 100,000-candle request was rejected by MT5 as invalid
parameters, so no 12-month history was fabricated. Pagination was performed
with closed candles only, UTC normalization, chronological ordering,
timestamp deduplication, OHLC validation, and immutable SHA-256 provenance.

## Signal sample size

Full M5 funnel (50,000 candles):

| Result | Count |
|---|---:|
| BUY | 1,872 |
| SELL | 2,050 |
| Total BUY + SELL | 3,922 |
| NO_TRADE aggregate | 46,078 |

NO_TRADE rejection reasons:

| Reason | Count |
|---|---:|
| `DATA_INCOMPLETE` | 1 |
| `HTF_TREND_NOT_ALIGNED` | 27,000 |
| `M15_PULLBACK_NOT_DETECTED` | 9,819 |
| `M5_CONFIRMATION_MISSING` | 8,974 |
| `MIN_LOT_EXCEEDS_RISK_BUDGET` | 284 |
| Valid signal (`VALID_TREND_PULLBACK`) | 3,922 |

The sample-size gate is therefore passed (`3,922 >= 100`). No thresholds were
loosened to create trades.

## Small optimized benchmark

Representative bounded replay used the same immutable MT5 dataset, limited to
1,000 M5 candles:

- Canonical signals: BUY `145`, SELL `0`
- Aggregate NO_TRADE: `855`
- Feature/signal canonical pass: `0.712 s`
- Per-RR persistence: `0.031–0.035 s`
- Per-RR bounded outcome evaluation: `0.014–0.017 s`
- Five-RR grid wall time: `6.97 s` (pipeline-reported grid time `6.90 s`)
- Peak traced memory (single-RR representative run): `600,599,162` bytes
  (approximately `573 MiB`)

The previous deterministic signal generation was replayed on the same bounded
window. Counts and rejection reasons matched exactly. Stored outcomes matched
the reference by timestamp and realized result (`TP 15`, `SL 73`, `EXPIRED
57`). A repeated optimized RR2 run had identical summary values and dataset
hash.

## RR results

All rows below use the same 3,922 canonical signals and the same dataset hash.

| RR | TP | SL | AMBIGUOUS | EXPIRED | Pending | Win rate | Avg/Expectancy R | Median R | Total R | Profit factor | Max DD R | Max loss streak | Max win streak | Coverage |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1.0 | 1,266 | 1,636 | 13 | 1,005 | 2 | 0.4533 | -0.0931 | -0.2480 | -363.6090 | 0.7992 | 381.5849 | 25 | 18 | 0.9995 |
| 1.5 | 763 | 1,763 | 4 | 1,390 | 2 | 0.4102 | -0.0978 | -0.4285 | -382.8959 | 0.8039 | 417.3329 | 26 | 18 | 0.9995 |
| 2.0 | 458 | 1,817 | 2 | 1,643 | 2 | 0.3923 | -0.1063 | -0.5036 | -416.5870 | 0.7931 | 452.0509 | 28 | 18 | 0.9995 |
| 2.5 | 303 | 1,831 | 1 | 1,785 | 2 | 0.3888 | -0.0964 | -0.5200 | -377.8725 | 0.8136 | 417.4376 | 34 | 18 | 0.9995 |
| 3.0 | 215 | 1,837 | 1 | 1,867 | 2 | 0.3865 | -0.0913 | -0.5314 | -357.7785 | 0.8241 | 398.9242 | 34 | 18 | 0.9995 |

No RR was selected as a profitable candidate. Win rate was not used as a sole
selection criterion; every tested RR had negative total R and expectancy.

## Chronological validation

The dataset covers enough time for the deterministic 60/20/20 chronological
split. The holdout was not used to tune any strategy or RR parameter. RR 2.0
was evaluated independently in each window:

| Window | Snapshots | BUY | SELL | NO_TRADE | Trades | TP | SL | AMBIGUOUS | EXPIRED | Total R | Avg R |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Development | 30,044 | 1,271 | 1,212 | 27,561 | 2,483 | 278 | 1,178 | 1 | 1,026 | -307.1013 | -0.1237 |
| Validation | 9,881 | 136 | 499 | 9,246 | 635 | 82 | 300 | 0 | 253 | -64.9108 | -0.1022 |
| Holdout | 10,075 | 465 | 339 | 9,271 | 804 | 98 | 339 | 1 | 364 | -44.5750 | -0.0556 |

Holdout evidence is reported, not used to claim profitability or to alter the
strategy.

## Determinism and platform consistency

- Dataset hash, strategy configuration, RR, signal timestamps, terminal
  outcomes, and realized R values matched between the optimized full RR2 run
  and the prior deterministic RR2 run.
- Repeated small RR2 runs produced identical persisted summary values.
- CLI, `/api/research/datasets`, `/api/research/runs`, run detail/metrics/
  funnel endpoints, Telegram `/backtests`, and the Strategy Research page all
  read the same persisted research-run records. The research UI is explicitly
  marked offline/shadow-only.
- Research run payloads and decisions expose `execution_allowed=false`; no
  order execution path is invoked.

## Holdout results

Holdout evaluation completed as an independent chronological run. It produced
804 signals and negative total R (`-44.5750`) at RR 2.0. This is sufficient
evidence for sample-size evaluation, but not evidence that the strategy is
validated for deployment.

## Safety status

- Execution: **DISABLED**
- `mt5.order_send()`: not called; no production source implementation added
- Orders sent: `0`
- Broker writes: `0`
- No strategy-threshold changes
- No synthetic candles; gaps remain recorded honestly
- No commit, push, tag, deploy, or Phase 2.1 work performed

## Validation command results

- Full pytest: **144 passed**, 2 deprecation warnings
- Ruff: **passed**
- Python compileall: **passed**
- Frontend Vitest: **4 passed**
- TypeScript typecheck: **passed**
- ESLint: **passed**
- Production build: **passed**
- Production-source `order_send` scan: **no matches**
- Tracked sensitive/runtime artifact audit: **no forbidden tracked files**
