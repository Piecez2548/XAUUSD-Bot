# Phase 2.4 — Pair Zone V1 Robustness & Cost Validation

Date: 2026-09-22  
Status: **ROBUSTNESS TESTS COMPLETE — DESCRIPTIVE EVIDENCE ONLY**

This report evaluates the exact persisted `pair_zone_v1` signal set. The YAML
and strategy implementation were frozen before analysis and were not changed.
This phase is research-only: execution is DISABLED, orders sent are 0, broker
writes are 0, and no `mt5.order_send()` path was added or called.

## Frozen identity

- Strategy: `pair_zone_v1` version `1.0.0`
- Frozen config: `config/strategies/pair_zone_v1.yaml`
- Exact file SHA-256: `fd2d73b9aa0d21004653e455263107caf727ac552fd204486f363cb7c25b7ded`
- Parsed-config SHA-256: `9e62fe34da4c24567d4a3f771efa91fcd01b35996d8ce48f23fb7edc2969f29b`
- Persisted source config hash: `50db903c7f284d82793508613739f36b43e2c7ce13730d8e32162d085d58e92b`
- Source RR2 run: `4f159b9b-dd55-4638-98d1-bb9c3d6811e1`
- Robustness run: `robustness_7f310459-64a4-464f-ae96-23072936f115`

## Dataset and canonical signals

- Dataset: `dataset_c1d7c37fc364`
- Dataset SHA-256: `c1d7c37fc364e43224099b198c9fda513e9c216047ae0c506acef9abcf41dd5b`
- Symbol/timeframe: `XAUUSDm` / `M5`
- Range: `2026-01-08 13:40 UTC` → `2026-09-22 12:05 UTC`
- Candles: 50,000 valid; duplicates 0; conflicts 0; invalid 0
- Canonical signals: BUY 307, SELL 261, NO_TRADE 49,432 (568 total signals)
- Dataset gaps: 183 discontinuities and 23,998 missing expected M5 intervals;
  145 were classified as intraday and 38 as session/weekend. No synthetic
  candles were added. 19 of 568 signals were within 30 minutes of a gap.

## Cost model

The model is applied after the frozen outcome evaluation, so it does not alter
signals or TP/SL/AMBIGUOUS/EXPIRED classification. Net R = Gross R − Cost R.
Cost R uses adverse full spread plus entry and exit slippage divided by the
trade's initial risk distance, plus the explicit commission assumption.

| Scenario | Spread | Entry slip | Exit slip | Commission | Net total R | Net expectancy | Net PF | Net max DD R |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Zero | 0 | 0 | 0 | 0.00 R | 73.550 | 0.1297 | 1.285 | 13.279 |
| Normal | 20 pt | 0.5 pt | 0.5 pt | 0.02 R | 46.571 | 0.0821 | 1.171 | 14.592 |
| Elevated | 40 pt | 1.0 pt | 1.0 pt | 0.04 R | 19.592 | 0.0346 | 1.068 | 17.691 |
| Stress | 80 pt | 2.0 pt | 2.0 pt | 0.08 R | -34.366 | -0.0606 | 0.892 | 55.101 |

Gross results remain 73.550 R across scenarios; normal cost is 26.979 R.
BUY normal net was 18.396 R and SELL normal net was 28.175 R. This is a
descriptive comparison, not a strategy score or ranking.

### RR sensitivity using the same canonical signals

| RR | TP | SL | AMBIGUOUS | EXPIRED | Normal net R | Normal expectancy | Normal PF | Normal max DD R |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1.0 | 257 | 191 | 2 | 118 | 40.898 | 0.0723 | 1.182 | 12.494 |
| 1.5 | 166 | 222 | 1 | 179 | 30.516 | 0.0538 | 1.116 | 15.277 |
| 2.0 | 117 | 231 | 1 | 219 | 46.571 | 0.0821 | 1.171 | 14.592 |
| 2.5 | 76 | 237 | 1 | 254 | 44.464 | 0.0784 | 1.159 | 17.097 |
| 3.0 | 63 | 240 | 1 | 264 | 57.976 | 0.1023 | 1.205 | 16.868 |

No RR was selected from win rate alone.

## Temporal, side, concentration, and expiry evidence

Normal-cost monthly net R by UTC month:

| Month | Signals | Net R | Net expectancy |
|---|---:|---:|---:|
| 2026-01 | 54 | -3.997 | -0.0740 |
| 2026-02 | 68 | 3.800 | 0.0567 |
| 2026-03 | 71 | 8.947 | 0.1260 |
| 2026-04 | 59 | -4.493 | -0.0762 |
| 2026-05 | 61 | 7.112 | 0.1166 |
| 2026-06 | 65 | 1.973 | 0.0304 |
| 2026-07 | 64 | -4.094 | -0.0640 |
| 2026-08 | 73 | 23.413 | 0.3207 |
| 2026-09 | 53 | 13.909 | 0.2624 |

There are both positive and negative months. Normal-cost SELL expectancy was
0.1079 R versus BUY 0.0601 R; both sides were evaluated independently.

Trade concentration is material but not exclusive: top 1 contributed 1.970 R,
top 5 9.847 R, and top 10 19.684 R (42.27% of normal net total). Removing the
top 1/top 5/top 10 left 44.601/36.724/26.887 R respectively. Best month was
August (+23.413 R); worst was April (-4.493 R).

There were 219 EXPIRED outcomes (38.56% of signals). Their mean gross result
was +0.3221 R and median +0.3169 R. Including expired outcomes produced
73.550 gross R; excluding them produced 3.000 gross R. Expiry is therefore a
material policy dependency and must not be hidden.

## Monte Carlo

Seed `240922`, 1,000 deterministic reshuffles of normal-cost resolved net R:

- Max drawdown R percentiles P50/P90/P95/P99: 18.627 / 26.705 / 29.698 / 34.630
- Maximum loss-streak percentiles P50/P90/P95/P99: 9 / 12 / 13 / 16
- Ending total R is invariant under reshuffle: 46.571 at all reported percentiles

The ending-total invariance is expected because reshuffling changes order, not
the multiset of trades.

## Parameter perturbation diagnostic (not tuning)

Each variant used a temporary in-memory/YAML copy and the exact frozen strategy
code; the canonical file was never changed and no variant was persisted as a
strategy configuration. RR2 signal counts were:

| Parameter | -10% | +10% |
|---|---:|---:|
| `zone_max_age_minutes` | 303 BUY / 258 SELL | 307 / 261 |
| `stop_buffer_points` | 307 / 261 | 306 / 261 |
| `confirmation_wick_to_body` | 318 / 275 | 297 / 253 |
| `displacement_min_points` | 307 / 262 | 307 / 260 |

The diagnostic shows sensitivity, especially in confirmation strictness, but it
does not alter or recommend a new parameter set.

## Walk-forward and platform consistency

Frozen chronological 30-day observation windows were generated from the
persisted RR2 outcomes, with no random shuffle and no parameter fitting. The
window evidence is persisted in the robustness run under
`walk_forward_normal_cost`. The same persisted record is exposed by:

- FastAPI: `GET /api/research/robustness?strategy_id=pair_zone_v1`
- Telegram: `/robustness pair_zone_v1`, `/costs pair_zone_v1`,
  `/stability pair_zone_v1`
- Strategy Lab dashboard: Robustness and Cost Validation panel

All three surfaces return the same run, dataset hash, cost scenarios, seed,
and `execution_allowed=false`.

## Safety and validation

- `execution_allowed`: false in robustness manifest, API, Telegram, and UI
- Orders sent: 0; broker writes: 0
- Production source search for `mt5.order_send`: no matches
- Frozen `pair_zone_v1` config and implementation: unchanged
- `trend_pullback_v1`: unchanged
- No Nexus repository changes
- Runtime database/history files remain local and ignored

Automated validation after implementation:

- Full pytest: **153 passed, 2 warnings**
- Ruff: **passed**
- compileall: **passed**
- Frontend Vitest: **4 passed**
- TypeScript typecheck: **passed**
- ESLint: **passed**
- Production build: **passed**

This report intentionally does not claim live trading validation or a strategy
selection. Phase 2.4 is robustness evidence only; execution remains DISABLED.
