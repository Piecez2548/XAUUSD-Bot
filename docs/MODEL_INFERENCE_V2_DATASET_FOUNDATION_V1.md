# Model Inference V2 Dataset Foundation V1

This foundation is an offline, read-only dataset view. It does not train a
model and is not called by Live, Forward Shadow, Pair Zone, risk, Demo, or
broker execution code.

## Contract

- Canonical identity is `StrategyIntelligenceRecord.candidate_id`.
- The causal cutoff is `StrategyIntelligenceRecord.detected_at`, normalized
  to UTC. Features are selected only from the explicit
  `model_training_features_v1` whitelist and must be present in the persisted
  READY Strategy Intelligence context. Terminal outcome fields are labels,
  never features.
- Only TP and SL with complete signal/trade/session provenance and complete
  causal features are trainable. OPEN/PENDING, AMBIGUOUS, EXPIRED,
  NOT_ELIGIBLE, pre-signal observations, malformed rows, and causal violations
  remain auditable but non-trainable or unresolved.
- Symbols are exact strings. No suffix, broker, or case inference is applied.
- Session-Aware Continuity V2 is not accepted by default. A caller must pass
  `session_aware_v2_accepted=True` to allow otherwise valid TP/SL rows to be
  considered trainable; the default keeps them auditable and non-trainable.

The current implementation streams JSONL and a JSON manifest because the
declared project dependencies do not include a Parquet writer. An explicit
Parquet request fails with an actionable dependency error; no dependency is
silently added. Artifact bytes are SHA-256 hashed, temporary files are
promoted only after completion, and existing artifacts/manifests are never
silently overwritten.

## Bounded and read-only behavior

Candidates are paged by `(detected_at, id)` keyset order, with a page size
bounded to 1..500. Each page uses one joined read for source, Forward,
Pair Zone, and advisory evaluation provenance. The source transaction enables
SQLite `query_only`, rolls back, and restores the connection setting before it
returns to the pool. Only the caller-selected artifact directory is written.
Dataset identity includes the contract versions, builder semantics version,
content-affecting parameters, and ordered row identities/fingerprints; it
does not include build time or filesystem path.

## Leakage matrix

| Fixture | Expected result |
| --- | --- |
| valid TP / valid SL | trainable only when V2 acceptance is explicit |
| OPEN | OUTCOME_PENDING, `OPEN/PENDING`, non-trainable |
| AMBIGUOUS / EXPIRED | completed audit row, non-trainable |
| no signal | PRE_SIGNAL_OBSERVATION, `NOT_ELIGIBLE`, non-trainable |
| malformed signal or trade | UNRESOLVED |
| future candle/context timestamp | UNRESOLVED causal violation |
| post-cutoff signal evidence | UNRESOLVED causal violation |
| future Pair Zone evidence | UNRESOLVED causal violation |
| exact symbol mismatch | UNRESOLVED provenance mismatch |
| duplicate candidate identity | UNRESOLVED |
| changed evidence for same identity | changed row fingerprint; never silently merged |
| unordered source timestamps | UNRESOLVED causal violation |
| missing feature | non-trainable with missing-feature audit reason |
| incomplete provenance | UNRESOLVED |
| V2 pre-acceptance dependency | non-trainable with explicit reason |
| keyset page boundary tie | stable `(detected_at, id)` cursor |
| large fixture | bounded page reads; no offset or full-history query |
