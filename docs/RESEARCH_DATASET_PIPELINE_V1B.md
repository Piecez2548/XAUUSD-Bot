# Research Dataset Pipeline V1B

V1B is a bounded, read-only orchestration layer for the existing Model
Inference V2 Dataset Foundation.  The logical pipeline version is
`model_inference_dataset_pipeline_v1` and its stages are, in order:

`DATA_SOURCE` → `DATASET_BUILD` → `DATASET_AUDIT` → `ARTIFACT_VERIFY`

The V1A research pipeline tables remain the lifecycle authority.  The
orchestrator does not add model-training, profitability, approval, trading,
risk, broker, or production semantics.

## Source and build

`DATA_SOURCE` records only bounded logical contract references: dataset,
feature, label, builder-semantics, source-population, exact-symbol,
causal-cutoff, and Session-Aware V2 continuity policies.  It never stores
source-table contents, database paths, credentials, or arbitrary evidence.

`DATASET_BUILD` delegates row generation and JSONL/manifest creation to
`ModelInferenceDatasetBuilder` and `write_dataset_artifact`.  It does not
duplicate candidate identity, causal cutoff, feature whitelist, label,
eligibility, hashing, or atomic-file semantics.  Progress records real
processed candidates.  The total is `null` unless an exact caller-provided
total is available; no percentage is persisted.

The build gate stores reconciled bounded counts.  Trainable, non-trainable,
and unresolved rows reconcile to produced rows, and inspected candidates
reconcile to produced plus excluded rows.

## Audit and artifacts

`DATASET_AUDIT` rereads the JSONL and manifest and verifies readability,
artifact and manifest hashes, semantic hash, row counts, canonical JSONL
serialization, row fingerprints, contract versions, duplicate identities,
training-eligibility consistency, feature/label leakage, exact symbol, and
Session-Aware V2 acceptance.

Research artifact records contain only logical identities, hashes, sizes,
contract metadata, producer stage attempts, and bounded lineage references.
Bytes remain in the explicitly supplied research artifact directory.  Records
start `UNVALIDATED`/`UNPUBLISHED` and become `VALID`/`PUBLISHED` only after
the audit passes.

## Retry and reuse

Repeated immutable requests reuse a completed `PASS` run and never rebuild an
artifact.  A changed request conflicts through V1A idempotency.  Incomplete
or failed runs are not silently rebuilt; the explicit retry operation creates
stage attempt N+1 for the failed build, audit, or verification stage.
Attempt-specific artifact identities keep artifacts from a failed build
distinct from a successful retry; audit and verification retries preserve the
same immutable build artifacts and create new historical stage attempts.

The roadmap read model returns bounded pipeline, stage, artifact, and build
summary data.  It intentionally omits filesystem paths, database paths,
credentials, account information, and broker data.
