# Phase 3.5.3 — OWNER Bootstrap and Account Approval

## Status and safety boundary

This phase adds a local-only first OWNER bootstrap, OWNER-authorized account
invitation/disable operations, one-time enrollment, and safe session
administration. It does not create an OWNER, migrate a production database, or
make the application ready for production mutation. Cookie-authenticated
mutations remain **NOT READY FOR PRODUCTION MUTATION** until the dedicated
CSRF phase is complete. The frontend remains Login-only; no public registration
or account-mutation form is present.

No production runtime, database, credentials, or trading state was used during
implementation or validation.

## First OWNER bootstrap

After the intended local database has been migrated through revision
`20260924_0015`, run this command from an interactive local PowerShell terminal:

```powershell
.venv\Scripts\python.exe -m scripts.bootstrap_owner
```

The command requires a TTY and the explicit confirmation phrase `BOOTSTRAP
OWNER`. It prompts for login and expected Tailscale identity, then reads the
password twice using `getpass`; password arguments and environment variables
are not accepted. The command refuses before password prompting if an OWNER
already exists. The service checks again inside its transaction, and a partial
unique database index permits at most one OWNER even when two bootstrap
attempts race. The stored password is Argon2id only. Successful bootstrap and
safe actor/target provenance are recorded in the auth audit table.

The initial Tailscale login is operator-supplied at bootstrap and is enforced
on every login/session authentication. Possession of the URL or tailnet access
does not create an account.

## Account states and role policy

The public account state vocabulary is `PENDING`, `ACTIVE`, and `DISABLED`.
For compatibility with frozen revision 0014 constraints, these map to stored
states `PENDING_APPROVAL`, `ACTIVE`, and `REVOKED` respectively. There are only
two roles: `OWNER` and `ADMIN`. The unique database invariant allows one OWNER;
the OWNER account cannot be disabled, deleted, or demoted through the available
operations. ADMIN has no account-management authority. Role changes and owner
rebind are intentionally not implemented.

An OWNER may issue an ADMIN invitation bound to a specific normalized
Tailscale login. The pending account has no usable password. The successful
OWNER-authorized issuance response discloses the newly generated 256-bit
CSPRNG enrollment secret exactly once, with a 15-minute expiry and
`Cache-Control: no-store`; only its SHA-256 digest is persisted. This is the
only raw enrollment-secret disclosure. No later account, session, or enrollment
status response can retrieve it. The intended user submits the secret and
chooses a password through the tailnet-gated enrollment route. Consumption is
a conditional database update in the activation transaction, so replay,
expiration, wrong secret, wrong target identity, and concurrent reuse fail
closed. Enrollment never grants OWNER and does not satisfy the application
session requirement for subsequent requests.

After issuance, OWNER account and session APIs expose only safe account and
session metadata. They support creating an ADMIN invitation, disabling an
account, listing sessions, revoking one session, and revoking all sessions for
an account. Disable revokes active sessions and unused enrollment records.
Password hashes, enrollment digests, session token hashes, and raw session
tokens are never included in these subsequent API payloads. Sensitive
operations record actor, target, action, timestamp, and result; plaintext
passwords and raw secrets are never intentionally logged or audited.

## API authorization and frontend

The existing Tailscale identity header/session binding remains mandatory. The
administrative route allowlist is exact and method-specific; each administrative
route additionally checks an ACTIVE, identity-bound OWNER on the backend.
Unknown paths and methods remain denied. `/api/auth/register` and
`/api/auth/signup` do not exist. Enrollment is limited to a previously
OWNER-issued secret and the exact invited Tailscale identity.

The frontend remains Login-only. Account-management controls are deferred
until CSRF protection is complete; this prevents the UI from implying that
these cookie-authenticated mutation APIs are production-ready.

## Migration

Revision `20260924_0015` follows 0014 without editing any historical revision.
It adds an `auth_enrollments` table, actor provenance for auth audit events, and
a partial unique OWNER index. Fresh `empty database -> head` and existing
`0014 -> head` paths are validated with disposable SQLite databases. Downgrade
removes the 0015 enrollment table, owner index, and actor-provenance column; any
issued invitations and that added provenance are therefore intentionally
discarded by a downgrade. No production migration was run.

## Validation boundary

Tests use disposable databases and synthetic identities/passwords. Real-money
execution remains disabled. This phase does not change Pair Zone, Forward
Shadow, Strategy Intelligence, risk calculation, DemoExecutionService, MT5,
supervisor/named-pipe authority, Telegram lifecycle, or broker behavior.
