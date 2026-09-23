# Phase 2.5.6 Acceptance Evidence — Sanitized Manifest

Date: 2026-09-23  
Source label: `phase256-final-evidence-20260923`  
Harness: `tools/phase256_failure_harness.py`  
Harness SHA-256: `4E967EA84124C19DF392F53FBC99132CE85640C9EDC2DC2858DFACC699146D29`

This is a compact release-retention manifest. It does not copy the temporary
evidence directory, process dumps, SQLite files, raw logs, absolute local
paths, credentials, or process identifiers.

## Aggregate result

- `all_passed=true`
- Scenarios passed: 6
- Cleanup checks passed: 6
- Production runtime touched: no
- Telegram, MT5, broker execution, port 8000, and Forward Shadow state used: no

## Scenario results

| Scenario | Result | Sanitized assertion |
|---|---|---|
| API incomplete stop | PASS | Intentional stop remained `STOPPING`/desired `STOPPED`; restart aborted; no replacement, crash notification, or restart-count increase. |
| Live incomplete stop | PASS | Intentional stop remained `STOPPING`; restart aborted; no replacement, crash notification, or restart-count increase. |
| Ambiguous topology | PASS | Two independent matching roots remained distinct; no adoption, termination, or third topology. |
| Identity disagreement / PID reuse | PASS | Missing creation-time identity and reused PID identity were rejected as `DEGRADED`; PID alone was not trusted. |
| Pre-Popen startup failure | PASS | No child was created; result was `START INCOMPLETE`; failed stage was recorded; injected secret was absent from diagnostics. |
| Post-spawn verification failure | PASS | Only the newly spawned failing component was rolled back; the pre-existing healthy component was unchanged. |

## Operation references

The two incomplete-stop scenarios were linked to operation IDs in the raw
temporary evidence:

- API incomplete stop: `26ddf935-7e79-4655-9d2e-50c8b0d5a953`
- Live incomplete stop: `0120a545-f574-4bd7-8413-3f21f9814197`

These identifiers are retained only for local evidence correlation; no raw
process identity or machine path is retained here.

## Retention and handling

The full temporary evidence remains outside the repository for local audit
reference. This manifest is the retained repository artifact. The raw
evidence must not be staged as a release artifact because it contains
machine-specific paths, process topology, and runtime diagnostics.

Synthetic secret-like strings used by the harness are test fixtures only and
are not production credentials. No production secret value is recorded in
this manifest.
