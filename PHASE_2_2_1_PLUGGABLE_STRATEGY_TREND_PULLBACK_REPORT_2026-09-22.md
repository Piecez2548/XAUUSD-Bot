# Phase 2.2.1 Pluggable Strategy + Trend Pullback Research Report

Date: 2026-09-22 UTC

## 1. Scope

Phase 2.2.1 introduces a replaceable, versioned strategy boundary and the first independent candidate, `trend_pullback_v1`. This is research/replay/shadow only. `baseline_v1` remains frozen, execution remains disabled, and no Candle Pair rules were implemented.

## 2. Repository baseline

- Root: `D:\Project_001\Nexus-Project\XAUUSD Bot`
- Branch: `main`
- Accepted baseline: tag `phase-2.1-accepted`, commit `837fd0297df23d06c41c9cd04b2654b9eabb375c`
- Remote: `https://github.com/Piecez2548/XAUUSD-Bot.git`
- No Nexus files were modified.

## 3. Architecture before

The live shadow worker directly constructed `ShadowDecisionEngine`, which embedded the frozen baseline identity. Replay also directly constructed that engine. Decisions then flowed to the existing repository, Phase 2.1 outcome evaluator, analytics and read-only surfaces.

## 4. Architecture after

`market data -> features -> StrategyRegistry plugin -> normalized ShadowDecision -> independent risk gate -> Phase 2.1 outcome evaluator -> analytics`.

The worker resolves one human-selected plugin using `SHADOW_STRATEGY`. The baseline adapter delegates to the original engine; the trend-pullback plugin owns only research intent. Neither plugin imports MT5 execution, supervisor, Telegram control or account mutation APIs.

## 5. Strategy interface

`services/strategy_platform.py` defines `StrategyMetadata`, `StrategyDecisionCandidate`, and the `Strategy` contract: metadata, configuration validation, required timeframes, evaluation and explanation. All returned decisions are `BUY`, `SELL` or `NO_TRADE`; `execution_allowed` is forced false.

## 6. Strategy registry

The registry currently resolves:

- `baseline_v1` — frozen adapter.
- `trend_pullback_v1` — independent research plugin.

Unknown identifiers fail closed. Candle Pair remains a future, unregistered strategy and was not implemented.

## 7. Version/config identity

Every new decision carries `strategy_version`, `config_version` and a SHA-256 canonical configuration hash. The trend configuration is stored in `config/strategies/trend_pullback_v1.yaml`. Migration `20260922_0007` adds these fields to `shadow_decisions` and creates append-only `strategy_activations`.

## 8. Activation semantics

`SHADOW_STRATEGY` is human-controlled configuration. Worker startup records the selected immutable identity and schedules its effective boundary at the next five-minute closed-candle boundary. Existing decisions are never relabeled. Telegram/API surfaces expose identity only; no autonomous strategy switching exists.

## 9. Rollback semantics

Rollback means selecting a previously registered immutable identifier for a future startup/boundary. It creates another activation record and does not rewrite decisions, outcomes or configuration history. No live execution rollback exists.

## 10. baseline_v1 regression evidence

The baseline adapter delegates to the original `ShadowDecisionEngine`. Existing deterministic baseline tests passed unchanged. The new adapter equivalence test verifies identical decision and price levels on the known trend fixture; only immutable config identity is added.

## 11. trend_pullback_v1 exact rules

This is explicitly an **INITIAL RESEARCH HYPOTHESIS**, not the trader's Candle Pair strategy:

1. H4 and H1 each require at least 50 closed candles.
2. Compute EMA20 and EMA50 over closed closes. BUY requires EMA20 > EMA50, positive EMA20 slope, and close above EMA20. SELL is the inverse.
3. M15 pullback is `abs(close - EMA20) <= 1.0 * ATR(14)`.
4. M5 confirmation is a closed directional candle whose close is beyond the M15 EMA20 in the trend direction.
5. Spread ceiling is 100 broker points.
6. Entry uses the M5 signal close as a deterministic `signal_close_proxy`; a true next-candle bid/ask is not available in this replay dataset.
7. Stop uses the latest five-bar M5 swing plus/minus `0.25 * ATR(14)`.
8. Target is fixed 2.0R for the primary experiment.
9. Existing independent shadow risk gate enforces 2% per-trade/6% aggregate limits and broker sizing.

No parameter was selected using outcomes. Values are fixed initial research hypotheses and must not be interpreted as trader-confirmed rules.

## 12. Funnel

The trend plugin returns explicit rejection stages in its feature context/reason codes: DATA, CONTEXT, RISK_GATE, HTF_TREND, M15_PULLBACK, M5_CONFIRMATION, SPREAD and GEOMETRY. The current 63-candle research run ended at the HTF stage for all observations:

| Stage/result | Count |
|---|---:|
| Eligible chronological candles | 63 |
| HTF trend aligned | 0 |
| M15 pullback | 0 |
| M5 confirmation | 0 |
| BUY | 0 |
| SELL | 0 |
| NO_TRADE (`HTF_TREND_NOT_ALIGNED`) | 63 |

## 13. Historical dataset audit

Raw closed-candle rows in the local store (duplicates are counted across persisted snapshot windows):

| Timeframe | Rows | Earliest UTC | Latest UTC | Duplicate timestamp rows |
|---|---:|---|---|---:|
| M5 | 2,188 | 2026-09-16 02:25 | 2026-09-22 10:00 | 944 |
| M15 | 2,062 | 2026-09-04 10:15 | 2026-09-22 09:45 | 905 |
| H1 | 1,014 | 2026-08-20 03:00 | 2026-09-22 09:00 | 452 |
| H4 | 1,003 | 2026-05-27 12:00 | 2026-09-22 04:00 | 483 |

The duplicate counts reflect repeated snapshot windows; replay deduplicates by logical event time and strategy version. A complete missing-interval audit was not fabricated from overlapping windows.

## 14. Data provenance

Data comes from the existing read-only MT5 observation database and persisted closed candles. No historical rows were edited, deleted or overwritten. Research replay is ephemeral and does not insert production shadow decisions or outcomes.

## 15. Causality protections

Replay orders snapshots by closed M5 event time, deduplicates logical keys, excludes forming candles, and supplies each strategy only the snapshot's available history. Trend calculations use no future bars. Phase 2.1 outcome semantics remain strictly `timestamp > decision timestamp`, with same-bar TP/SL ambiguous and insufficient horizon pending.

## 16. Cost model

The dataset does not prove historical bid/ask, slippage or commission for every replay candle. Trend entry is therefore a signal-close proxy and all results are `PRE-COST RESEARCH RESULT`. No live profitability inference is valid.

## 17. Primary 2R result

`trend_pullback_v1` on the available chronological period produced 63 NO_TRADE decisions and zero BUY/SELL. Resolved N is zero; TP, SL, ambiguous, expired, expectancy, profit factor, drawdown, MFE and MAE are `UNKNOWN / N/A`, not zero.

## 18. Limited RR sensitivity

Skipped. The primary 2R experiment produced no eligible trades, so inspecting 1.5R/2.5R/3.0R would not be meaningful and would risk false optimization.

## 19. Train/validation/holdout

`INSUFFICIENT DATA FOR HOLDOUT VALIDATION`. The available overlapping candle store and zero candidate trades do not support a defensible chronological split.

## 20. Walk-forward

Skipped for the same reason. No parameter was changed between windows.

## 21. Baseline descriptive comparison

| Metric | baseline_v1 | trend_pullback_v1 |
|---|---:|---:|
| Unique replay candles | 23 | 63 |
| BUY | 0 | 0 |
| SELL | 0 | 0 |
| NO_TRADE | 23 | 63 |
| Resolved N | 0 | 0 |
| Expectancy R | UNKNOWN / N/A | UNKNOWN / N/A |
| Max DD R | UNKNOWN / N/A | UNKNOWN / N/A |

The samples are descriptive and not directly comparable in coverage because baseline replay was limited by its existing CLI default while research replay uses the full persisted snapshot set. No winner or ranking is declared.

## 22. Performance metrics

Both strategies have N=0 eligible/resolved trades in this data. NO_TRADE is not a loss; no win rate, loss rate, R metrics, streaks or Monte Carlo distribution is manufactured.

## 23. Sample-size limitations

Evidence is `VERY LOW / NO TRADE SAMPLE`. Approximately 17 hours of persisted decision history is not enough to infer edge, stability, session behavior or expected live return.

## 24. Tests

Added registry resolution/unknown rejection, deterministic config hashing, baseline adapter equivalence, activation audit safety and immutable identity coverage. Existing baseline, replay, outcome and safety tests remain active.

## 25. Exact validation output

- Python regression: `138 passed, 2 warnings`.
- Ruff: passed.
- `compileall`: passed.
- Baseline research CLI: completed, BUY 0 / SELL 0 / NO_TRADE 63, execution disabled.
- Trend research CLI: completed, BUY 0 / SELL 0 / NO_TRADE 63, execution disabled.
- Migration `20260922_0007`: applied additively after database backup.
- Frontend baseline validation: Vitest 4 passed, TypeScript passed, ESLint passed, production build passed.

## 26. Safety scan

Execution remains disabled. No `mt5.order_send()` or broker-write method was added. Strategy plugins cannot set `execution_allowed=true`. No historical production rows were modified and no execution control was added.

## 27. Known limitations

- Trend pullback values are an initial research hypothesis, not trader-confirmed rules.
- No eligible trades exist in the available dataset.
- Overlapping snapshot windows contain duplicate timestamps.
- Costs and next-candle bid/ask are not fully reconstructable.
- No holdout or walk-forward result is scientifically supportable.
- No dashboard Strategy Research page was added; read-only API/Telegram identity surfaces are available.

## 28. Recommended next step

Obtain a substantially longer causal XAUUSD history and define a chronological holdout plan. Separately, validate the fixed trend-pullback hypothesis with the same dataset before any sensitivity experiment. Keep Candle Pair implementation deferred until the trader supplies its exact rules.

PHASE 2.2.1 IMPLEMENTED — INSUFFICIENT HISTORICAL DATA
