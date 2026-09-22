# Phase 2.2 Strategy Validation & Strategy V2 Research Report

Date: 2026-09-22 UTC

## 1. Scope

This phase audited the frozen `baseline_v1` strategy and the available trader-rule evidence. It is research/shadow only. No broker writes, execution, order authorization, historical-data mutation, optimization, or Strategy V2 implementation was performed.

## 2. Repository baseline

- Git root: `D:\Project_001\Nexus-Project\XAUUSD Bot`
- Branch: `main`
- Accepted baseline tag: `phase-2.1-accepted`
- Accepted baseline commit: `837fd0297df23d06c41c9cd04b2654b9eabb375c`
- Remote: `https://github.com/Piecez2548/XAUUSD-Bot.git`
- Working tree was clean before this report-only change.

## 3. baseline_v1 exact rule audit

The decision path is `MarketSnapshot -> closed-only features -> H4/H1/M15/M5 context -> deterministic filters -> independent risk gate -> ShadowDecision`.

`services/shadow_engine.py` and `services/features.py` show:

1. Reject stale/incomplete data, disconnected MT5/runtime, or unknown risk.
2. Require closed H4, H1, M15 and M5 windows.
3. Compute ATR, EMA(20), EMA slope, price-vs-EMA, five-bar swing structure, volatility and relative volatility.
4. H4 and H1 must have the same directional bias. Bias requires EMA side, slope direction, and higher-high/higher-low or lower-high/lower-low structure.
5. High relative volatility (`>= 2.0`) and low relative volatility (`<= 0.5`) are rejected.
6. M15 setup and M5 timing must match the H4/H1 direction.
7. Spread must be at or below `SHADOW_MAX_SPREAD_POINTS` (default 100).
8. Entry is ask for BUY and bid for SELL.
9. Stop is five-bar swing low/high plus/minus `0.25 * ATR`; target is fixed `target_rr` (default 2.0); invalid geometry is rejected.
10. `ShadowRiskGate` enforces max 2% per trade, max 6% aggregate, broker tick/value sizing, volume step and minimum/maximum lot constraints.

BUY and SELL are emitted only after every stage passes. Every other branch emits `NO_TRADE` with a reason code. `execution_allowed` remains false.

## 4. Baseline rejection funnel

The current implementation does not persist stage-level funnel counters. It persists the final reason code, so a truthful stage funnel beyond the final rejection cannot be reconstructed without changing the frozen strategy instrumentation. The available persisted funnel is:

| Final reason | Count |
|---|---:|
| TREND_NOT_ALIGNED | 33 |
| MARKET_REGIME_UNCERTAIN | 15 |
| SPREAD_TOO_HIGH | 14 |
| Other recorded final reasons | 0 |

These counts are descriptive final rejection counts, not claims that they identify the earliest internal bottleneck.

## 5. Baseline signal frequency

Persisted baseline decisions cover `2026-09-21 16:40 UTC` through `2026-09-22 09:45 UTC`:

- Persisted decisions: 62
- BUY: 0
- SELL: 0
- NO_TRADE: 62
- Approximate observed window: 17.1 hours
- Observed signal frequency: 0 BUY/SELL; no signals/day or signals/week should be inferred from this small, non-segmented window.
- Regimes: `TREND_DOWN` 47, `UNCERTAIN` 15.

The causal replay smoke run processed 1,000 persisted snapshot rows, generated 23 unique chronological decisions, skipped 977 duplicate logical keys, and produced BUY 0 / SELL 0 / NO_TRADE 23 / errors 0. Replay output is not a profitability result.

## 6. Existing trader-rule evidence found

The repository contains baseline MTF alignment and an ATR/structure stop. It does not contain authoritative numeric rules for the trader's M15 zone/order-block selection, break/retest, touch/bounce, M5 rejection confirmation, zone invalidation, zone expiry, or manual-vs-automatic zone ownership.

## 7. Strategy V2 specification

`strategy_v2_research` was not implemented because the required trader rules are unresolved. No placeholder thresholds or optimized substitutes were introduced.

## 8. Unresolved trader-rule questions

1. How exactly is an M15 zone selected: one candle, an impulse range, an order block, or a manually supplied region?
2. Which candle(s) define `zone_low` and `zone_high`?
3. Is the zone manual/external or automatically discovered? If manual, how is it supplied to replay?
4. What exact event creates `CREATED`, `APPROACHING`, `ENTERED`, `CONFIRMED`, `INVALIDATED`, and `EXPIRED`?
5. What numeric rule defines a valid break and retest?
6. What exact condition defines `INVALID_BREAK`?
7. What exact M5 rejection properties and thresholds are required (wick, body/range, close location, penetration, prior candle, swing relation)?
8. Does entry occur at rejection close, next candle open, a limit level, or another price?
9. Where exactly is SL placed, and what unit is any wick buffer expressed in (price, points, ticks, or ATR)?
10. What primary RR is documented for the trader strategy?
11. When does an unconfirmed setup expire?
12. Are session, news, spread, or symbol-specific filters part of the actual strategy?

## 9. Zone semantics

No zone state machine was added. The required causal states and transitions remain a design checkpoint pending the answers above. Future implementation must keep manual zones separate from automatic discovery.

## 10. M5 confirmation semantics

No V2 confirmation rule was invented. OHLC-derived body, wick, range, close-location, penetration and local-structure fields are available in baseline features, but no threshold is authoritative for the trader's setup.

## 11. Entry semantics

Baseline uses ask for BUY and bid for SELL. V2 entry timing/price is unresolved and must be confirmed before implementation.

## 12. Stop semantics

Baseline uses five-bar swing plus/minus `0.25 * ATR`. This is not claimed to represent the trader's wick-buffer rule. The unit and placement for V2 are unresolved.

## 13. Target/RR semantics

Baseline projects a deterministic 2R target. V2's primary RR is unresolved; no RR sensitivity or selection was performed.

## 14. Causality protections

Existing replay uses closed candles, chronological M5 event time, logical `(symbol, M5 timestamp, strategy_version)` deduplication, and no insertion-time ordering. Phase 2.1 outcome evaluation uses only candles strictly after the decision candle. No V2 replay rows were created.

## 15. Dataset coverage

Available persisted shadow decision coverage is approximately 17.1 hours, with 62 decisions and no eligible BUY/SELL. This is insufficient for performance inference or regime/session conclusions.

## 16. Train/validation/holdout design

`INSUFFICIENT DATA FOR HOLDOUT VALIDATION`. The available sample is too short and has zero eligible trades. No chronological train/validation/holdout split was fabricated.

## 17. Cost assumptions

No V2 result exists. Baseline has a spread gate and uses broker bid/ask at decision time, but the replay dataset does not establish historical execution spread/slippage/commission reconstruction. Any future strategy result must be labelled `PRE-COST RESEARCH RESULT` unless those fields are proven.

## 18. Baseline results

There are no resolved baseline BUY/SELL outcomes in the available sample. TP, SL, AMBIGUOUS, EXPIRED, win rate, expectancy, profit factor, drawdown, MFE and MAE are therefore `UNKNOWN / N/A`, not zero. `NO_TRADE` is not a loss.

## 19. V2 results

Not run. `strategy_v2_research` is blocked pending trader-rule confirmation.

## 20. Descriptive comparison

| Metric | baseline_v1 | strategy_v2_research |
|---|---:|---:|
| Decisions | 62 persisted / 23 replay-unique | NOT RUN |
| BUY | 0 | BLOCKED |
| SELL | 0 | BLOCKED |
| NO_TRADE | 62 persisted / 23 replay-unique | BLOCKED |
| Resolved N | 0 | BLOCKED |
| Expectancy R | UNKNOWN / N/A | BLOCKED |
| Max DD R | UNKNOWN / N/A | BLOCKED |

This is descriptive evidence only; no winner is declared and V2 is not promoted.

## 21. Session analysis

Not meaningful with zero BUY/SELL and less than one day of coverage. No session filter was optimized.

## 22. Sample-size limitations

The outcome sample is `N=0`, below the requested evidence bands. The dataset is also insufficient for holdout validation. No profitability or edge claim is made.

## 23. Tests

Existing tests cover baseline deterministic decisions, closed-candle behavior, feature validation, replay ordering/deduplication, risk-gate safety, and Phase 2.1 causal outcome semantics. No V2 tests were added because no faithful V2 rules exist to test.

## 24. Exact validation results

- `134 passed, 2 warnings` — `.venv\Scripts\python.exe -m pytest -q --disable-warnings`
- Ruff: passed — `.venv\Scripts\python.exe -m ruff check .`
- Compileall: passed — `python -m compileall -q .`
- Baseline replay: completed with 0 errors; execution disabled.
- No frontend changes were made in Phase 2.2; the accepted Phase 2.1 frontend validation remains the baseline.

## 25. Safety scan

Execution remains disabled. No `mt5.order_send()` was added or called. No broker-write path, execution control, historical-data mutation, optimization, or production decision-row insertion was introduced. The Nexus repository was not modified.

## 26. Known limitations

- Stage-level funnel counters are not persisted by frozen baseline code.
- Historical coverage is short and contains no eligible trades.
- Cost reconstruction is incomplete.
- Trader-specific M15 zone and M5 rejection semantics are absent.
- V2 performance, Monte Carlo, session outcomes and holdout metrics cannot be truthfully produced.

## 27. Recommended next research step

Obtain explicit trader answers to the twelve unresolved questions in Section 8. Then implement a separately versioned, research-isolated `strategy_v2_research` with fixed documented rules, causal zone state transitions, and a chronological train/validation/holdout plan. Do not alter `baseline_v1`.

STRATEGY_V2_RESEARCH_BLOCKED_BY_TRADER_RULES — exact M15 zone, break/retest, M5 rejection, entry, stop-buffer, RR, expiry, and filter rules are not specified authoritatively
