# Model Inference V2 Future-Only Causal Contract V1

Future records emitted by the Pair Zone Strategy Intelligence integration use
`strategy_intelligence_provenance_v2`.  The primary source is M15 and the
confirmation stream is M5; the historical overloaded `M15/M5` label remains
legacy data and is never rewritten.

`detected_at` and `signal_decision_at` are event labels.  They identify the
latest closed source/confirmation candle used by the deterministic producer.
`observation_available_at` and `decision_available_at` are deterministic
closed-candle availability boundaries, derived from candle timestamps and
timeframe duration.  `created_at` remains persistence metadata only and is not
used as a causal boundary.

Each actionable future signal carries the legacy raw `zone_id` plus a separately
named immutable `pair_zone_decision_evidence_id`.  That evidence identity is a
canonical SHA-256 of the contract version, exact symbol, session, zone,
direction, strategy hash, and causal Pair Zone timestamps.  It never contains
wall-clock or persistence metadata.  The explicit timeframe contract,
decision boundary, and bounded pair timestamps are immutable after insertion.
The mutable latest `PairZoneEvaluationRecord` is not used as the V2 training
join.  A V2 dataset row is eligible only when its immutable signal, candidate,
session, trade, strategy/configuration, symbol, direction, and event identity
agree.  Outcomes are restricted to future `TP`, `SL`, `AMBIGUOUS`, and
`EXPIRED` semantics already enforced by Forward Shadow.  A terminal candle
label is an event timestamp; its `outcome_available_at` is the candle close
boundary and must be strictly after decision availability.  `evaluated_at` is
processing metadata and is not causal.

V1 rows and the existing historical 451-row population remain readable under
`model_inference_dataset_v1`, with their original overloaded timeframe and
detected-at cutoff semantics.  No migration backfills or fabricates V2
provenance.  V2 rows use `model_inference_dataset_v2`; the additional temporal,
timeframe, and event fields are included in the canonical row fingerprint.

`ForwardSignalRecord.created_at` and other persistence timestamps are excluded
from the V2 semantic fingerprint.  Service-level conflict checks, ORM guards,
and database triggers reject immutable provenance mutation.

This contract is additive and research-only.  It does not change Pair Zone
decisions, Forward Shadow outcomes, Strategy Intelligence scoring, model
runtime authority, risk, Demo, broker access, or candle ingestion.
