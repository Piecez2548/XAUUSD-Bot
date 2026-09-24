"""One-time local repair for the initial OWNER bootstrap identity typo.

Run interactively with ``.venv\\Scripts\\python.exe -m scripts.correct_owner_identity``.
This command is deliberately bound to one known bootstrap correction; it is not
a general-purpose identity-rebinding interface.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from config.settings import load_settings
from persistence.database import AUTH_SCHEMA_MINIMUM_REVISION, AuthSchemaError, Database
from persistence.orm import AuthAuditRecord, AuthUserRecord
from services.authentication import normalize_login

EXPECTED_OWNER_ID = "cdd40386-c961-4b8b-8e2c-2c5c51a095ae"
OLD_LOGIN = "pattaraponkhunnaronginvesting@"
OLD_IDENTITY = "pattaraponkhunnaronginvesting@"
NEW_LOGIN = "pattaraponkhunnaronginvesting@gmail.com"
NEW_IDENTITY = "pattaraponkhunnaronginvesting@gmail.com"
CONFIRMATION_PHRASE = "CORRECT INITIAL OWNER IDENTITY"


class OwnerCorrectionError(RuntimeError):
    """The known one-time correction preconditions were not satisfied."""


class OwnerAlreadyCorrected(OwnerCorrectionError):
    """The one-time correction was already applied; no mutation was made."""


def correct_owner_identity(
    database: Database,
    *,
    owner_id: str,
    old_login: str,
    old_identity: str,
    new_login: str,
    new_identity: str,
) -> None:
    """Atomically apply only the fixed bootstrap correction after strict checks."""

    supplied = (owner_id, old_login, old_identity, new_login, new_identity)
    expected = (EXPECTED_OWNER_ID, OLD_LOGIN, OLD_IDENTITY, NEW_LOGIN, NEW_IDENTITY)
    if supplied != expected:
        raise OwnerCorrectionError("Correction values do not match the approved one-time repair")

    try:
        with database.engine.connect() as connection:
            revisions = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalars().all()
    except Exception as exc:
        raise OwnerCorrectionError("Expected production auth schema is unavailable") from exc
    if revisions != [AUTH_SCHEMA_MINIMUM_REVISION]:
        raise OwnerCorrectionError("Database revision does not match the approved correction")
    try:
        database.require_auth_schema()
    except AuthSchemaError as exc:
        raise OwnerCorrectionError("Production auth schema could not be verified") from exc

    old_normalized = normalize_login(OLD_LOGIN)
    new_normalized = normalize_login(NEW_LOGIN)
    new_bound_identity = normalize_login(NEW_IDENTITY)

    try:
        with database.session() as session:
            owners = session.scalars(
                select(AuthUserRecord).where(AuthUserRecord.role == "OWNER")
            ).all()
            if len(owners) != 1:
                raise OwnerCorrectionError("Expected exactly one OWNER account")

            owner = owners[0]
            if owner.id != EXPECTED_OWNER_ID:
                raise OwnerCorrectionError("OWNER account ID does not match the approved account")
            if owner.state != "ACTIVE":
                raise OwnerCorrectionError("OWNER account is not ACTIVE")

            if (
                owner.normalized_login == new_normalized
                and owner.bound_tailscale_login == new_bound_identity
            ):
                raise OwnerAlreadyCorrected("OWNER identity is already corrected; no changes made")
            if (
                owner.normalized_login != old_normalized
                or owner.bound_tailscale_login != normalize_login(OLD_IDENTITY)
            ):
                raise OwnerCorrectionError("OWNER does not match the exact expected old identity")
            if not owner.password_hash:
                raise OwnerCorrectionError("OWNER password verifier is missing")

            login_collision = session.scalar(
                select(AuthUserRecord.id).where(
                    AuthUserRecord.normalized_login == new_normalized,
                    AuthUserRecord.id != owner.id,
                )
            )
            identity_collision = session.scalar(
                select(AuthUserRecord.id).where(
                    AuthUserRecord.bound_tailscale_login == new_bound_identity,
                    AuthUserRecord.id != owner.id,
                )
            )
            if login_collision or identity_collision:
                raise OwnerCorrectionError("Target login or Tailscale identity is already assigned")

            changed = session.execute(
                update(AuthUserRecord)
                .where(
                    AuthUserRecord.id == EXPECTED_OWNER_ID,
                    AuthUserRecord.role == "OWNER",
                    AuthUserRecord.state == "ACTIVE",
                    AuthUserRecord.normalized_login == old_normalized,
                    AuthUserRecord.bound_tailscale_login == normalize_login(OLD_IDENTITY),
                )
                .values(
                    normalized_login=new_normalized,
                    bound_tailscale_login=new_bound_identity,
                )
            ).rowcount
            if changed != 1:
                raise OwnerCorrectionError("OWNER changed concurrently; correction was not applied")

            session.add(
                AuthAuditRecord(
                    event_type="OWNER_IDENTITY_CORRECTION",
                    actor_user_id=None,
                    user_id=EXPECTED_OWNER_ID,
                    normalized_login=new_normalized,
                    tailscale_login=new_bound_identity,
                    outcome="SUCCEEDED",
                    reason=f"BOOTSTRAP_TYPO_FROM:{old_normalized}",
                )
            )
    except IntegrityError as exc:
        raise OwnerCorrectionError(
            "A uniqueness or concurrency conflict prevented the correction"
        ) from exc


def main() -> int:
    if len(sys.argv) != 1:
        print("This one-time repair accepts no command-line arguments.", file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("OWNER identity repair requires an interactive local terminal.", file=sys.stderr)
        return 2

    print("One-time correction of the initial OWNER login and Tailscale identity.")
    print("No password, session, or authentication secret is requested.")
    if input(f'Type "{CONFIRMATION_PHRASE}" to continue: ').strip() != CONFIRMATION_PHRASE:
        print("Correction cancelled.")
        return 1

    values = {
        "owner_id": input("Expected OWNER account ID: ").strip(),
        "old_login": input("Expected current OWNER login: ").strip(),
        "old_identity": input("Expected current Tailscale identity: ").strip(),
        "new_login": input("New full OWNER login: ").strip(),
        "new_identity": input("New full Tailscale identity: ").strip(),
    }
    release_root = Path(__file__).resolve().parents[1]
    settings = load_settings(env_file=release_root / ".env")
    database = Database(settings.database_url, project_root=release_root)
    try:
        database.require_auth_schema()
        correct_owner_identity(database, **values)
    except OwnerAlreadyCorrected as exc:
        print(str(exc))
        return 1
    except OwnerCorrectionError as exc:
        print(f"OWNER identity correction refused: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "OWNER identity correction failed safely; inspect local database availability.",
            file=sys.stderr,
        )
        return 1
    finally:
        database.dispose()

    print("OWNER login and Tailscale identity corrected; account and password verifier preserved.")
    print("A secret-free audit event was recorded. Existing sessions were not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
