# Research Roadmap V1C

V1C exposes a bounded, read-only roadmap projection over the persisted V1A
Research Pipeline and V1B Dataset Pipeline metadata.  It does not start,
retry, cancel, build, train, or mutate research work.

## Endpoints

- `GET /api/research/roadmap?limit=20` returns the overview, selected current
  run, bounded recent run history, stage attempts, dataset summary, artifact
  metadata, safe source information, and the latest ten timeline events.
- `GET /api/research/roadmap/runs/{run_id}` returns the bounded detail view for
  one run.
- `GET /api/research/roadmap/runs/{run_id}/timeline?limit=100&cursor=...`
  returns a deterministic keyset-paginated event timeline.

The existing FastAPI middleware and remote read allowlist remain the
authentication and network boundary.  V1C adds no alternate authentication
mechanism.

## Selection and progress

The current run is the newest non-terminal run by `(created_at, run_id)`.
When no active run exists, it is the newest terminal run using the same tie
break.  No run is fabricated when the database is empty.

Progress is derived only for presentation.  A null total is indeterminate;
positive totals produce a clamped percentage; total zero produces no
percentage.  Completion remains represented by lifecycle state.  Percentages
are never persisted.

All attempts remain visible.  The highest attempt for each logical stage is
marked current, while earlier attempts retain their original state and retry
count.

## Timeline and privacy

Timeline events are derived from immutable pipeline, stage, gate, progress, and
artifact metadata already persisted by V1A/V1B.  V1C does not add an event
table and never invents intermediate progress events.  Timestamp ties use
event type, source identity, and attempt as deterministic tie-breakers.

Responses expose only whitelisted logical source references and metadata-only
artifact fields.  Filesystem paths, database paths, credentials, account or
broker data, artifact bytes, and raw metadata are excluded.  Errors use
bounded codes such as `ROADMAP_RUN_NOT_FOUND`, `ROADMAP_INVALID_CURSOR`,
`ROADMAP_INVALID_LIMIT`, and `ROADMAP_QUERY_FAILED`.
