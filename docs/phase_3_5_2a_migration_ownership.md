# Phase 3.5.2A — Migration Ownership and Deployment Consistency

This correction makes Alembic the owner of the Phase 3.5.2 auth tables while
preserving the repository's legacy non-auth schema bootstrap.

## Schema ownership

- `Database.create_schema()` creates legacy/non-auth ORM tables but filters
  `auth_users`, `auth_sessions`, and `auth_audit_events`.
- Private-mode FastAPI initialization checks that Alembic is at
  `20260924_0014` or a descendant, that all three auth tables and ORM-required
  columns exist, and that login/token uniqueness and the session-user foreign
  key are present.
- Missing/behind/partial/unknown schema or database-access failure prevents
  private app startup. There is no header-only fallback, auto-migration, or
  silent stamp.
- `Database.for_test()` is an explicit test factory restricted to SQLite
  in-memory or OS-temporary-directory databases. Only those instances can call
  `create_test_schema()`, and that helper accepts only a database with no
  existing tables. It refuses normal `Database` instances, non-disposable
  targets, and any existing schema/revision; only a fresh disposable test DB
  receives a synthetic 0014 marker.
- Revisions 0001–0003 use migration-local frozen schema definitions and
  explicit operations; they do not import or execute current ORM metadata.
  Revision 0014 remains the sole owner of the auth tables.
- Fresh empty-database replay to head, all revision-boundary ownership checks,
  and the disposable 0013-to-head data-preservation test pass. Historical
  revisions must remain independent of future ORM evolution.

## Private backend/frontend publication order

FastAPI serves `frontend/dist` at request time. Do not build validation output
into that directory. Routine `npm run build` and
`scripts/build_frontend.ps1` target ignored `frontend/dist-validation/`.

For a separately authorized deployment:

1. Preserve a production DB backup, current backend code, and current `dist`.
2. Build and validate the new frontend in staging; do not publish it yet.
3. Apply Alembic migrations before loading the new private backend.
4. Reload only the authorized backend generation while the old frontend remains
   published. Verify the API is healthy and `/api/auth/session` returns the
   expected unauthenticated envelope when no session exists. Old frontend
   requests must fail closed during this interval.
5. Publish the staged frontend only after backend verification. On Windows,
   same-volume directory renames preserve a rollback copy but are not a single
   atomic operation; a short static 404 gap is possible. Hashed assets avoid a
   mixed manifest/cache generation.
6. Verify direct navigation, refresh, private API authentication, and the
   expected frontend bundle. Only then consider the release accepted.

If backend verification fails, restore the previous backend and leave the old
frontend in place. If publication fails, restore the prior `dist`. Keep the
additive DB migration and backup; do not downgrade auth tables as an automatic
rollback. This is a controlled deployment contract, not permission to deploy.

No production runtime or served frontend was inspected or changed by this
implementation. No first OWNER bootstrap exists yet, and this work does not
authorize production Web mutations. Cookie-authenticated Control and Demo POST
routes and auth logout remain **NOT READY FOR PRODUCTION MUTATION** until
explicit CSRF protection and the remaining rollout controls are implemented.
