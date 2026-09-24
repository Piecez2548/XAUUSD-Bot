from __future__ import annotations

import hmac
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import scripts.correct_owner_identity as correction_module
from config.settings import Settings
from persistence.database import Database
from persistence.orm import AuthAuditRecord, AuthSessionRecord, AuthUserRecord
from scripts.correct_owner_identity import (
    EXPECTED_OWNER_ID,
    NEW_IDENTITY,
    NEW_LOGIN,
    OLD_IDENTITY,
    OLD_LOGIN,
    OwnerAlreadyCorrected,
    OwnerCorrectionError,
    correct_owner_identity,
    main,
)
from services.authentication import AuthenticationService

TEST_PASSWORD = "Existing owner password stays private 42!"


@pytest.fixture
def database(tmp_path: Path):
    value = Database.for_test(f"sqlite:///{(tmp_path / 'owner-correction.db').as_posix()}")
    value.create_test_schema()
    yield value
    value.dispose()


def seed_owner(database: Database, *, state: str = "ACTIVE") -> tuple[str, datetime]:
    created_at = datetime(2026, 9, 24, 12, 30, tzinfo=UTC)
    password_hash = AuthenticationService.hash_password(TEST_PASSWORD)
    with database.session() as session:
        session.add(
            AuthUserRecord(
                id=EXPECTED_OWNER_ID,
                normalized_login=OLD_LOGIN,
                password_hash=password_hash,
                role="OWNER",
                state=state,
                bound_tailscale_login=OLD_IDENTITY,
                created_at=created_at,
                updated_at=created_at,
            )
        )
    return password_hash, created_at


def run_correction(database: Database, **overrides) -> None:
    values = {
        "owner_id": EXPECTED_OWNER_ID,
        "old_login": OLD_LOGIN,
        "old_identity": OLD_IDENTITY,
        "new_login": NEW_LOGIN,
        "new_identity": NEW_IDENTITY,
    }
    values.update(overrides)
    correct_owner_identity(database, **values)


def add_admin(database: Database, *, login: str, identity: str) -> str:
    account_id = "admin-collision-000000000000000000000000"
    with database.session() as session:
        session.add(
            AuthUserRecord(
                id=account_id,
                normalized_login=login,
                password_hash=AuthenticationService.hash_password(TEST_PASSWORD),
                role="ADMIN",
                state="ACTIVE",
                bound_tailscale_login=identity,
            )
        )
    return account_id


def test_exact_correction_preserves_account_verifier_role_state_time_and_sessions(database):
    password_hash, created_at = seed_owner(database)
    session_id = "session-preserved-0000000000000000000000"
    now = datetime.now(UTC)
    with database.session() as session:
        session.add(
            AuthSessionRecord(
                id=session_id,
                user_id=EXPECTED_OWNER_ID,
                token_hash="a" * 64,
                created_at=now,
                last_seen_at=now,
                idle_expires_at=now + timedelta(hours=1),
                absolute_expires_at=now + timedelta(hours=8),
            )
        )

    run_correction(database)

    with database.session() as session:
        owner = session.get(AuthUserRecord, EXPECTED_OWNER_ID)
        assert owner is not None
        assert owner.id == EXPECTED_OWNER_ID
        assert owner.normalized_login == "pattaraponkhunnaronginvesting@gmail.com"
        assert owner.bound_tailscale_login == "pattaraponkhunnaronginvesting@gmail.com"
        assert owner.role == "OWNER"
        assert owner.state == "ACTIVE"
        assert owner.created_at == created_at
        assert AuthenticationService.verify_password(owner.password_hash, TEST_PASSWORD)
        assert hmac.compare_digest(owner.password_hash, password_hash)
        assert session.get(AuthSessionRecord, session_id) is not None
        event = session.scalar(
            select(AuthAuditRecord).where(
                AuthAuditRecord.event_type == "OWNER_IDENTITY_CORRECTION"
            )
        )
        assert event is not None
        assert event.user_id == EXPECTED_OWNER_ID
        assert event.normalized_login == NEW_LOGIN
        assert event.tailscale_login == NEW_IDENTITY
        assert event.outcome == "SUCCEEDED"
        assert event.reason == f"BOOTSTRAP_TYPO_FROM:{OLD_LOGIN}"
        audit_values = " ".join(
            str(getattr(event, column.key)) for column in event.__table__.columns
        )
        assert TEST_PASSWORD not in audit_values
        assert password_hash not in audit_values

    auth = AuthenticationService(database, Settings())
    assert auth.login(NEW_LOGIN, TEST_PASSWORD, NEW_IDENTITY).user is not None
    assert auth.login(OLD_LOGIN, TEST_PASSWORD, NEW_IDENTITY).user is None
    assert auth.login(NEW_LOGIN, TEST_PASSWORD, OLD_IDENTITY).user is None


@pytest.mark.parametrize(
    "override",
    [
        {"owner_id": "wrong-account-id"},
        {"old_login": "different-old-login"},
        {"old_identity": "different-old-identity"},
        {"new_login": "different-new-login@example.test"},
        {"new_identity": "different-new-identity@tailnet.test"},
    ],
)
def test_rejects_any_unapproved_correction_value(database, override):
    seed_owner(database)
    with pytest.raises(OwnerCorrectionError):
        run_correction(database, **override)
    with database.session() as session:
        owner = session.get(AuthUserRecord, EXPECTED_OWNER_ID)
        assert owner is not None and owner.normalized_login == OLD_LOGIN
        assert owner.bound_tailscale_login == OLD_IDENTITY
        assert session.scalar(
            select(AuthAuditRecord).where(
                AuthAuditRecord.event_type == "OWNER_IDENTITY_CORRECTION"
            )
        ) is None


def test_refuses_ambiguous_multiple_owner_state(database):
    seed_owner(database)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_auth_users_single_owner")
    with database.session() as session:
        session.add(
            AuthUserRecord(
                id="second-owner-0000000000000000000000000",
                normalized_login="second-owner@example.test",
                password_hash=AuthenticationService.hash_password(TEST_PASSWORD),
                role="OWNER",
                state="ACTIVE",
                bound_tailscale_login="second@tailnet.test",
            )
        )
    with pytest.raises(OwnerCorrectionError, match="auth schema"):
        run_correction(database)


def test_refuses_when_no_owner_exists(database):
    with pytest.raises(OwnerCorrectionError, match="exactly one OWNER"):
        run_correction(database)


def test_refuses_unexpected_database_revision(database):
    seed_owner(database)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE alembic_version SET version_num = ?", ("unknown",))
    with pytest.raises(OwnerCorrectionError, match="revision"):
        run_correction(database)
    with database.session() as session:
        owner = session.get(AuthUserRecord, EXPECTED_OWNER_ID)
        assert owner is not None and owner.normalized_login == OLD_LOGIN


@pytest.mark.parametrize("collision", ["login", "identity"])
def test_rejects_target_login_or_identity_collision(database, collision):
    seed_owner(database)
    add_admin(
        database,
        login=NEW_LOGIN if collision == "login" else "other-admin@example.test",
        identity=NEW_IDENTITY if collision == "identity" else "other@tailnet.test",
    )
    with pytest.raises(OwnerCorrectionError, match="already assigned"):
        run_correction(database)


def test_rejects_inactive_owner(database):
    seed_owner(database, state="LOCKED")
    with pytest.raises(OwnerCorrectionError, match="not ACTIVE"):
        run_correction(database)


def test_repeat_correction_refuses_without_second_mutation_or_audit(database):
    seed_owner(database)
    run_correction(database)
    with pytest.raises(OwnerAlreadyCorrected):
        run_correction(database)
    with database.session() as session:
        events = session.scalars(
            select(AuthAuditRecord).where(
                AuthAuditRecord.event_type == "OWNER_IDENTITY_CORRECTION"
            )
        ).all()
        assert len(events) == 1


def test_correction_api_requires_no_password_parameter_or_cli_secret(monkeypatch, capsys):
    import inspect

    assert "password" not in inspect.signature(correct_owner_identity).parameters
    monkeypatch.setattr(sys, "argv", ["correct_owner_identity", TEST_PASSWORD])
    assert main() == 2
    output = capsys.readouterr()
    assert TEST_PASSWORD not in output.out + output.err
    assert "command-line arguments" in output.err


def test_interactive_command_updates_without_prompting_for_or_printing_credentials(
    database, monkeypatch, capsys
):
    password_hash, _ = seed_owner(database)
    answers = iter(
        [
            "CORRECT INITIAL OWNER IDENTITY",
            EXPECTED_OWNER_ID,
            OLD_LOGIN,
            OLD_IDENTITY,
            NEW_LOGIN,
            NEW_IDENTITY,
        ]
    )
    prompts: list[str] = []

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    tty = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(
        correction_module,
        "sys",
        SimpleNamespace(argv=["correct_owner_identity"], stdin=tty, stdout=tty),
    )
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(correction_module, "load_settings", lambda **_kwargs: Settings())
    monkeypatch.setattr(correction_module, "Database", lambda *_args, **_kwargs: database)

    assert correction_module.main() == 0
    output = capsys.readouterr().out
    assert "password" not in " ".join(prompts).casefold()
    assert "secret" not in " ".join(prompts).casefold()
    assert TEST_PASSWORD not in output
    assert password_hash not in output
    assert "OWNER login and Tailscale identity corrected" in output
    with database.session() as session:
        owner = session.get(AuthUserRecord, EXPECTED_OWNER_ID)
        assert owner is not None and owner.normalized_login == NEW_LOGIN
        event = session.scalar(
            select(AuthAuditRecord).where(
                AuthAuditRecord.event_type == "OWNER_IDENTITY_CORRECTION"
            )
        )
        assert event is not None


def test_audit_schema_has_no_secret_or_verifier_fields():
    columns = {column.name for column in AuthAuditRecord.__table__.columns}
    assert not columns.intersection(
        {"password", "password_hash", "session_token", "token_hash", "csrf_token", "secret"}
    )
