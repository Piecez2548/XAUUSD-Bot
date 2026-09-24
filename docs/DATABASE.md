# Database and Event Model

## Storage

The default URL is `sqlite:///data/trading_observatory.db`. Relative SQLite paths resolve from the project root. Connections enable foreign keys, WAL mode, a 30-second busy timeout, pre-ping, and explicit transaction rollback on error.

All persisted timestamps are timezone-aware UTC in Python. SQLite stores normalized naive UTC text because SQLite has no native timezone type; `UtcDateTime` restores UTC awareness on read. PostgreSQL can use the same SQLAlchemy models with a PostgreSQL driver and production-grade migration revisions.

## Tables

| Area | Tables |
| --- | --- |
| Reference state | `accounts`, `symbols` |
| Observations | `account_snapshots`, `market_snapshots`, `candles` |
| Positions | `positions`, `position_snapshots` |
| Trading audit foundation | `trades`, `trade_events`, `ai_decisions` |
| Risk/performance | `risk_snapshots`, `performance_snapshots` |
| Operations | `system_events`, `system_health`, `control_audit` |
| Versioning | `configuration_versions`, `model_versions`, `prompt_versions` |

`SnapshotRepository` persists only closed candles (`candles[:-1]`) in the normalized candle table. The current forming candle remains in the market snapshot JSON for observation without being mistaken for a finalized bar. Candle uniqueness is enforced by symbol, timeframe, and timestamp.

Open broker positions are upserted by ticket and observation lifecycle. Each observation appends a position snapshot. A previously active ticket missing from the next verified observation is marked with `closed_observed_at`; it is not converted into a closed trade without real lifecycle data.

## Typed events

Every domain event includes UUID `event_id`, enum `event_type`, timezone-aware UTC `timestamp`, `source`, severity, optional `correlation_id`, discriminated typed payload with `kind`, and integer `schema_version`.

`EventRepository` uses `event_id` as the primary key and treats a repeated ID as an idempotent no-op. Correlation IDs connect all records produced by one observation. Phase 1.5 defines future lifecycle event types, but it never emits them to simulate trading.

## Migrations

```powershell
python main.py migrate
```

The historical bootstrap revisions use migration-local frozen schema definitions. **Versioned migrations must not depend on evolving current ORM metadata.** Future schema changes must use explicit Alembic operations or migration-local frozen definitions. `Base.metadata.create_all()` is reserved for explicitly disposable test-schema construction, not as migration authority. Test upgrades against disposable databases before considering a production migration.

## PostgreSQL migration path

1. Add a supported PostgreSQL DBAPI dependency.
2. Set `DATABASE_URL` to the PostgreSQL DSN through a secret manager.
3. Run migrations in a staging database.
4. Validate JSON, UUID, timestamp, and index behavior under representative load.
5. Move data with a controlled export/import process and compare table counts/checksums.
6. Run API and analytics regression tests before switching clients.

SQLite remains appropriate for the current single-node local observatory. PostgreSQL becomes preferable for multi-process writers, remote deployment, higher event volume, or operational HA.

## Retention

No automatic deletion is enabled. A future retention job should preserve trades, decisions, version records, and material audit events while allowing policy-based compaction of high-frequency market/position snapshots. Retention must be explicit, backed up, and audited.

## Phase 1.7 control audit

`control_audit` stores the command, allowlisted actor identifiers,
authorization result, bounded result code, correlation ID, and UTC timestamp.
It never stores bot tokens, chat credentials, message bodies, process command
lines, or broker secrets. Telegram update offsets and the supervisor process
registry are small local JSON files under `data/` and are not health payloads.

Phase 1.7.1 uses the existing `market_snapshots.id` as the observation-cycle
correlation ID. `position_snapshots.market_snapshot_id` and
`risk_snapshots.market_snapshot_id` must agree before a current view is marked
coherent. A missing open ticket is marked with `positions.closed_observed_at`;
historical broker deals remain in `broker_deals` and are not current positions.
# Phase 1.6 live tables

The `candle_cursors` table makes closed-candle publication idempotent per symbol
and timeframe. `broker_deals` stores immutable MT5 deal facts keyed by deal
ticket, and `history_cursors` stores the UTC reconciliation cursor. None of these
tables contain credentials or raw provider secrets.
