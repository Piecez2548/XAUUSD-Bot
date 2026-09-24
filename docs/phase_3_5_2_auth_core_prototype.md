# Phase 3.5.2 — Authentication Core Prototype

This prototype adds Argon2id password verification, user/session/audit persistence,
and opaque server-side sessions to the tailnet-private dashboard. The exact
Tailscale route gate remains mandatory. In `REMOTE_DASHBOARD_MODE`, every
allowlisted private API and Control route requires an active user session bound
to the current `Tailscale-User-Login` identity. Login/logout/session endpoints
are the only application-session exceptions and remain behind the Tailscale
gate. Static frontend files remain available only through that gate and contain
no credentials or private runtime data.

The cookie is `__Host-xauusd_session` with `Secure`, `HttpOnly`, `SameSite=Strict`,
`Path=/`, no `Domain`, a 30-minute idle expiry, and a 12-hour absolute expiry.
Only a SHA-256 digest of a high-entropy opaque token is persisted. Passwords use
Argon2id hashes. Login failures use a generic response. No registration,
provisioning, approval, recovery, or remote bootstrap endpoint exists.

## Deliberate limitations

This is not production rollout authorization. Phase 3.5.3 must implement local
first-owner bootstrap, explicit account approval, and access administration.
Phase 3.5.4 must add approved device records, session administration, and
explicit CSRF protection for cookie-authenticated mutations. Login throttling,
step-up authentication, and complete security audit coverage are also deferred.
While those controls are absent, do not deploy this prototype to authorize
production Web lifecycle or Demo operations.

The login route has no provisioning path: tests create users only in temporary
databases through an internal service primitive. A real account cannot be
created by HTTP in this phase.

## Migration and rollback

Alembic revision `20260924_0014` adds only `auth_users`, `auth_sessions`, and
`auth_audit_events`; it does not alter trading data. Validate it against a
temporary/copied database. Rollback of this revision drops those new tables and
therefore discards authentication state, so production rollback must preserve
the database backup and must not downgrade a database containing real accounts
without an explicit recovery plan. This phase does not run a migration on the
production database.

Normal application startup retains the legacy non-auth `create_all` bootstrap,
but explicitly excludes the three auth tables. Private-mode startup requires
the auth tables and an Alembic revision at `20260924_0014` or a descendant; it
fails closed with an operational error if the revision is behind, the schema is
partial/incompatible, or the database is unavailable. It verifies the ORM-
required auth columns, login/token uniqueness, and the session-user foreign key.
It never stamps or runs migrations. Disposable tests must use
`Database.for_test()` for SQLite in-memory or OS-temporary-directory databases;
only a fresh, empty database from that factory may call
`create_test_schema()` and receive a synthetic 0014 marker. Normal production
`Database` instances cannot invoke that helper.

Historical Alembic revisions 0001–0003 now use migration-local frozen schema
definitions and explicit operations; they do not consult evolving application
ORM metadata. Revision 0014 remains the sole owner of the auth tables. Empty
database replay through head and revision-boundary ownership checks pass. The
permanent policy is that versioned migrations must not depend on evolving
current ORM metadata.

## Deployment consistency

The API serves `frontend/dist` directly, so building into `dist` changes the
currently served UI without reloading the API. Routine `npm run build` and
`scripts/build_frontend.ps1` builds now write only to the ignored
`frontend/dist-validation/` staging directory; they do not publish it.

For a separately authorized rollout, preserve a DB backup and the current
backend/frontend release first. Build and validate the new frontend in staging,
then apply the Alembic migration, reload only the approved backend generation,
and verify the new backend's health and auth-session endpoint before publishing
the staged frontend. Publish by same-volume directory rename, keeping the prior
`dist` as a rollback copy. Windows requires two directory renames, so a brief
static 404 window is possible; this sequence prevents a new frontend from
reaching an old backend. During the backend-first interval the old frontend
will receive authorization failures and must fail closed; accept that brief
unavailability rather than publishing the new frontend early. If backend
verification fails, restore the prior backend and keep the old frontend. If
frontend publication fails, restore the previous `dist`; do not downgrade the
additive auth migration during rollback. These are deployment instructions,
not authorization to deploy or reload production.

At the time of the original prototype report, the served private `dist` and
running API generation were from different releases. This implementation did
not inspect or alter production runtime, frontend assets, or database state.
No OWNER was created. First-owner enrollment remains pending, and this phase
does not authorize Web lifecycle/Demo operations.

## CSRF limitation

Cookie-authenticated POST mutations remain **NOT READY FOR PRODUCTION
MUTATION** until explicit CSRF protection is implemented. This includes the
Control START/STOP/RESTART and Demo ARM/DISARM endpoints, plus auth logout.
