# Phase 2.3 Pair Zone V1 Research Report — 2026-09-22

## Status

**PHASE 2.3 RESEARCH COMPLETE — SAMPLE SUFFICIENT — EVIDENCE ONLY — EXECUTION DISABLED**

`pair_zone_v1` is a separate registry plugin. `trend_pullback_v1` and
`baseline_v1` were not modified. The result below is not an automatic strategy
winner declaration and is not a production-profitability claim.

## Dataset

- Dataset ID: `dataset_c1d7c37fc364`
- Dataset SHA-256: `c1d7c37fc364e43224099b198c9fda513e9c216047ae0c506acef9abcf41dd5b`
- Symbol/timeframe: `XAUUSDm` / M5
- Closed candles: `50,000`
- Range: `2026-01-08T13:40:00Z` → `2026-09-22T12:05:00Z`
- Valid/duplicate/conflicting/invalid: `50,000 / 0 / 0 / 0`
- Gaps remain recorded; no synthetic candles were added.

## Pair definition

The candidate is two consecutive closed M15 candles whose timestamps are exactly
15 minutes apart. The first candle is the base and the second is the
displacement candle.

- Minimum body size for each candle: `1.0` price units.
- Base body geometry: `base_body / base_range <= 0.75`.
- Displacement body geometry: `displacement_body / displacement_range >= 0.55`.
- Directional displacement: second close is above the first close for bullish
  pairs, or below it for bearish pairs; absolute close displacement is at least
  `2.0` price units.
- Range overlap: intersection of the two full OHLC ranges must be at least
  `0.25` price units and at least `10%` of the narrower range.
- Bullish pair: second candle closes above its open and above the first close.
- Bearish pair: second candle closes below its open and below the first close.
- Any pair failing one rule is an invalid pair. There is no visual/manual
  interpretation.

## Zone definition

For every valid pair, the immutable zone is:

- `zone_id`: deterministic SHA-256-derived ID of symbol, pair timestamps and
  direction;
- direction, pair timestamps and complete pair OHLC;
- lower bound: `max(first.low, second.low)`;
- upper bound: `min(first.high, second.high)`;
- width, displacement and overlap statistics.

The zone `created_at` is the second M15 candle timestamp plus 15 minutes. It is
therefore unavailable before the second candle has closed; no future candle is
used to construct it.

Lifecycle rules are deterministic: `CREATED` before first interaction,
`ACTIVE` after creation, `TOUCHED` on a distinct outside-to-intersection M5
touch, `CONFIRMED` on the first valid M5 rejection close, `INVALIDATED` when a
closed M5 candle closes through the opposite zone boundary, and `EXPIRED` after
360 minutes or more than two touches. Lifecycle aggregate statistics are stored
in each research run.

## Entry definition

The H1 filter uses only closed candles and an EMA10/EMA20 direction plus fast-EMA
slope. A mismatch is recorded as `HTF_DIRECTION_MISMATCH`.

Confirmation is one conservative closed-M5 rule:

- BUY: candle intersects the zone, closes above the upper bound, closes bullish,
  and lower wick is at least `0.5 × body`;
- SELL: candle intersects the zone, closes below the lower bound, closes bearish,
  and upper wick is at least `0.5 × body`.

Entry is the confirmation close. BUY stop is zone lower bound minus `0.50`;
SELL stop is zone upper bound plus `0.50`. RR changes only take-profit geometry,
never canonical signal generation.

## Sample size and rejection funnel

Full dataset results:

- Candidate pairs: `16,767`
- Valid zones: `4,326`
- Invalid pairs: `12,441`
- Zone touches: `7,086`
- Lifecycle confirmations before filters: `1,576`
- Canonical BUY: `307`
- Canonical SELL: `261`
- Canonical trade signals: `568`
- Aggregate NO_TRADE: `49,432`

Rejection funnel:

| Reason | Count |
|---|---:|
| `HTF_DIRECTION_MISMATCH` | 15,017 |
| `ZONE_CONFIRMATION_MISSING` | 34,378 |
| `MIN_LOT_EXCEEDS_RISK_BUDGET` | 37 |
| `PAIR_ZONE_CONFIRMED` | 568 |

The sample gate passed (`568 >= 100`). Rules were not loosened to manufacture
signals.

## RR results

Canonical signals were generated once and reused for all five RR values.

| RR | Trades | TP | SL | AMBIGUOUS | EXPIRED | Win rate | Average/Expectancy R | Median R | Total R | Profit factor | Max DD R | Max loss streak | Max win streak | Coverage |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1.0 | 568 | 257 | 191 | 2 | 118 | 0.5563 | 0.1198 | 0.4465 | 67.7933 | 1.3182 | 10.8986 | 6 | 9 | 1.0000 |
| 1.5 | 568 | 166 | 222 | 1 | 179 | 0.4771 | 0.1014 | -0.1136 | 57.4953 | 1.2317 | 13.3091 | 9 | 6 | 1.0000 |
| 2.0 | 568 | 117 | 231 | 1 | 219 | 0.4577 | 0.1297 | -0.2148 | 73.5497 | 1.2851 | 13.2790 | 9 | 6 | 1.0000 |
| 2.5 | 568 | 76 | 237 | 1 | 254 | 0.4437 | 0.1260 | -0.2854 | 71.4511 | 1.2705 | 14.8869 | 11 | 6 | 1.0000 |
| 3.0 | 568 | 63 | 240 | 1 | 264 | 0.4384 | 0.1498 | -0.2915 | 84.9632 | 1.3180 | 14.9189 | 11 | 6 | 1.0000 |

No automatic RR or strategy winner was selected. The positive holdout result is
reported as observed evidence, not as proof of persistent profitability.

## Development results

RR 2.0, chronological development window:

- 30,044 snapshots; BUY `190`, SELL `147`, NO_TRADE `29,707`
- 337 trades; TP `63`, SL `141`, AMBIGUOUS `1`, EXPIRED `132`
- Win rate `0.4481`; average/expectancy `0.0881R`; median `-0.2738R`
- Total `+29.5883R`; profit factor `1.1894`; max drawdown `13.2790R`

## Validation results

RR 2.0, chronological validation window:

- 9,881 snapshots; BUY `51`, SELL `54`, NO_TRADE `9,776`
- 105 trades; TP `17`, SL `45`, AMBIGUOUS `0`, EXPIRED `43`
- Win rate `0.4190`; average/expectancy `0.0009R`; median `-0.3182R`
- Total `+0.0938R`; profit factor `1.0019`; max drawdown `10.6485R`

## Holdout results

RR 2.0, untouched chronological holdout window:

- 10,075 snapshots; BUY `66`, SELL `60`, NO_TRADE `9,949`
- 126 trades; TP `37`, SL `45`, AMBIGUOUS `0`, EXPIRED `44`
- Win rate `0.5159`; average/expectancy `0.3482R`; median `0.2285R`
- Total `+43.8676R`; profit factor `1.8514`; max drawdown `6.7159R`

The holdout was not used to tune the pair rules or RR. One positive holdout on
this dataset is insufficient to claim general profitability or deployment
readiness.

## Strategy comparison evidence

At RR 2.0 on the same dataset:

| Strategy | Signals | Expectancy R | Total R | Profit factor | Max DD R | Win rate |
|---|---:|---:|---:|---:|---:|---:|
| `trend_pullback_v1` | 3,922 | -0.1063 | -416.5870 | 0.7931 | 452.0509 | 0.3923 |
| `pair_zone_v1` | 568 | 0.1297 | 73.5497 | 1.2851 | 13.2790 | 0.4577 |

Development, validation and holdout metrics are persisted independently and
exposed through the comparison API, Telegram `/compare`, and the read-only
Strategy Lab dashboard. The platform presents evidence and does not declare a
winner automatically.

## Safety status

- `execution_allowed=false` for every run and API payload;
- orders sent: `0`;
- broker writes: `0`;
- no production `mt5.order_send()`;
- no Telegram strategy-parameter mutation was added;
- no trading strategy baseline behavior was changed;
- no commit, push, tag, deploy, or Phase 2.4 work performed.

## Validation results

- Full pytest: **147 passed**, 2 deprecation warnings
- Ruff: **passed**
- Python compileall: **passed**
- Frontend Vitest: **4 passed**
- TypeScript typecheck: **passed**
- ESLint: **passed**
- Production build: **passed**
- Production-source `order_send` scan: **no matches**
- Forbidden tracked runtime/database/secrets files: **none**
- Abandoned research runs after recovery: **none**; persisted run records:
  `42 COMPLETED`
