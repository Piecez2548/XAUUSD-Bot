from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from alembic import command
from api.app import create_app
from config.remote_read_only_policy import is_auth_admin_route_allowed
from config.settings import Settings
from persistence.database import Database
from persistence.orm import (
    AuthAuditRecord,
    AuthEnrollmentRecord,
    AuthSessionRecord,
    AuthUserRecord,
)
from services.authentication import (
    AuthenticationService,
)

OWNER_IDENTITY = "owner@tailnet.example"
ADMIN_IDENTITY = "admin@tailnet.example"
OWNER_LOGIN = "owner@example.test"
ADMIN_LOGIN = "admin@example.test"
PASSWORD = "Correct Horse Battery Staple 42!"
TAILSCALE_OWNER = {"Tailscale-User-Login": OWNER_IDENTITY}
TAILSCALE_ADMIN = {"Tailscale-User-Login": ADMIN_IDENTITY}


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database.for_test(f"sqlite:///{(tmp_path / 'phase353.db').as_posix()}")
    value.create_test_schema()
    yield value
    value.dispose()


def service(database: Database) -> AuthenticationService:
    return AuthenticationService(database, Settings())


def bootstrap_owner(database: Database) -> str:
    return service(database).bootstrap_owner(
        login=OWNER_LOGIN,
        password=PASSWORD,
        bound_tailscale_login=OWNER_IDENTITY,
    )


def client(database: Database) -> TestClient:
    return TestClient(
        create_app(settings=Settings(remote_dashboard_mode=True), database=database),
        base_url="https://dashboard.tailnet.test",
    )


def login(client_: TestClient, login_name: str, password: str, identity: str) -> None:
    response = client_.post(
        "/api/auth/login",
        headers={"Tailscale-User-Login": identity},
        json={"login": login_name, "password": password},
    )
    assert response.status_code == 200, response.text


def invite_admin(database: Database, owner_id: str, *, target_identity=ADMIN_IDENTITY):
    return service(database).create_invitation(
        actor_id=owner_id,
        tailscale_login=OWNER_IDENTITY,
        login=ADMIN_LOGIN,
        role="ADMIN",
        bound_tailscale_login=target_identity,
    )


def test_first_owner_bootstrap_persists_argon2id_and_audit(database: Database) -> None:
    owner_id = bootstrap_owner(database)
    with database.session() as session:
        owner = session.get(AuthUserRecord, owner_id)
        event = session.scalar(
            select(AuthAuditRecord).where(AuthAuditRecord.event_type == "OWNER_BOOTSTRAP")
        )
        assert owner is not None
        assert owner.role == "OWNER" and owner.state == "ACTIVE"
        assert owner.bound_tailscale_login == OWNER_IDENTITY
        assert owner.password_hash.startswith("$argon2id$")
        assert owner.password_hash != PASSWORD and PASSWORD not in owner.password_hash
        assert event and event.outcome == "SUCCEEDED"
        assert event.actor_user_id == owner_id and event.user_id == owner_id


def test_owner_bootstrap_refuses_second_owner(database: Database) -> None:
    bootstrap_owner(database)
    with pytest.raises(RuntimeError, match="already exists"):
        bootstrap_owner(database)
    with database.session() as session:
        owners = session.scalars(select(AuthUserRecord).where(AuthUserRecord.role == "OWNER")).all()
        assert len(owners) == 1


def test_owner_bootstrap_never_logs_password(database: Database, caplog) -> None:
    bootstrap_owner(database)
    assert PASSWORD not in caplog.text


def test_legacy_fixture_provisioner_cannot_create_accounts_in_normal_database(
    tmp_path: Path,
) -> None:
    ordinary = Database(f"sqlite:///{(tmp_path / 'ordinary.db').as_posix()}")
    try:
        with pytest.raises(PermissionError, match="disposable tests"):
            service(ordinary).create_user_for_admin(
                login=ADMIN_LOGIN,
                password=PASSWORD,
                role="ADMIN",
                state="ACTIVE",
                bound_tailscale_login=ADMIN_IDENTITY,
            )
    finally:
        ordinary.dispose()


def test_concurrent_first_owner_bootstrap_has_one_winner(database: Database) -> None:
    def attempt(index: int) -> str:
        try:
            return service(database).bootstrap_owner(
                login=f"{OWNER_LOGIN}{index}",
                password=PASSWORD,
                bound_tailscale_login=OWNER_IDENTITY,
            )
        except Exception as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))
    assert (
        sum(
            outcome not in {"RuntimeError", "OperationalError", "IntegrityError"}
            for outcome in outcomes
        )
        == 1
    )
    with database.session() as session:
        owners = session.scalars(select(AuthUserRecord).where(AuthUserRecord.role == "OWNER")).all()
        assert len(owners) == 1


def test_unknown_user_and_registration_routes_never_create_accounts(database: Database) -> None:
    with client(database) as app:
        identity = {"Tailscale-User-Login": OWNER_IDENTITY}
        for path in ("/api/auth/register", "/api/auth/signup"):
            assert app.post(path, headers=identity, json={"login": OWNER_LOGIN}).status_code == 404
        assert (
            app.post(
                "/api/auth/login",
                headers=identity,
                json={"login": OWNER_LOGIN, "password": PASSWORD},
            ).status_code
            == 401
        )
    with database.session() as session:
        assert session.scalar(select(AuthUserRecord.id)) is None


def test_pending_invitation_enrolls_once_and_binds_identity(database: Database) -> None:
    owner_id = bootstrap_owner(database)
    issue = invite_admin(database, owner_id)
    assert issue.account["state"] == "PENDING"
    assert issue.account["role"] == "ADMIN"
    assert issue.secret and issue.secret not in str(issue.account)
    digest = hashlib.sha256(issue.secret.encode()).hexdigest()
    with database.session() as session:
        row = session.scalar(select(AuthEnrollmentRecord))
        account = session.get(AuthUserRecord, issue.account["id"])
        assert row and row.token_hash == digest and issue.secret not in row.token_hash
        assert account and account.state == "PENDING_APPROVAL"
        assert not AuthenticationService.verify_password(account.password_hash, PASSWORD)

    auth = service(database)
    assert not auth.activate_enrollment(
        secret=issue.secret, password=PASSWORD, tailscale_login="wrong@tailnet.example"
    )
    assert auth.activate_enrollment(
        secret=issue.secret, password=PASSWORD, tailscale_login=ADMIN_IDENTITY
    )
    assert not auth.activate_enrollment(
        secret=issue.secret, password=PASSWORD, tailscale_login=ADMIN_IDENTITY
    )
    with database.session() as session:
        account = session.get(AuthUserRecord, issue.account["id"])
        enrollment = session.scalar(select(AuthEnrollmentRecord))
        assert account and account.state == "ACTIVE"
        assert AuthenticationService.verify_password(account.password_hash, PASSWORD)
        assert enrollment and enrollment.consumed_at is not None


def test_wrong_and_expired_enrollment_secrets_fail_closed(database: Database) -> None:
    owner_id = bootstrap_owner(database)
    issue = invite_admin(database, owner_id)
    auth = service(database)
    assert not auth.activate_enrollment(
        secret="x" * 64, password=PASSWORD, tailscale_login=ADMIN_IDENTITY
    )
    assert auth.activate_enrollment(
        secret=issue.secret, password=PASSWORD, tailscale_login=ADMIN_IDENTITY
    )
    assert not auth.activate_enrollment(
        secret=issue.secret, password=PASSWORD, tailscale_login=ADMIN_IDENTITY
    )
    with database.session() as session:
        failures = session.scalars(
            select(AuthAuditRecord).where(AuthAuditRecord.event_type == "ENROLLMENT_FAILURE")
        ).all()
        assert {event.reason for event in failures} >= {
            "INVALID_ENROLLMENT",
            "ENROLLMENT_ALREADY_USED",
        }


def test_owner_only_api_and_admin_is_denied(database: Database) -> None:
    owner_id = bootstrap_owner(database)
    issue = invite_admin(database, owner_id)
    assert service(database).activate_enrollment(
        secret=issue.secret, password=PASSWORD, tailscale_login=ADMIN_IDENTITY
    )
    with client(database) as app:
        assert app.get("/api/auth/admin/accounts", headers=TAILSCALE_OWNER).status_code == 401
        login(app, OWNER_LOGIN, PASSWORD, OWNER_IDENTITY)
        accounts = app.get("/api/auth/admin/accounts", headers=TAILSCALE_OWNER)
        assert accounts.status_code == 200
        assert {row["role"] for row in accounts.json()["accounts"]} == {"OWNER", "ADMIN"}

    with client(database) as app:
        login(app, ADMIN_LOGIN, PASSWORD, ADMIN_IDENTITY)
        assert app.get("/api/auth/admin/accounts", headers=TAILSCALE_ADMIN).status_code == 403
        escalation = app.post(
            "/api/auth/admin/accounts",
            headers=TAILSCALE_ADMIN,
            json={
                "login": "new-owner@example.test",
                "role": "OWNER",
                "bound_tailscale_identity": "new-owner@tailnet.example",
            },
        )
        assert escalation.status_code == 403


def test_owner_invitation_returns_secret_once_and_never_persists_plaintext(
    database: Database,
    caplog,
) -> None:
    owner_id = bootstrap_owner(database)
    with client(database) as app:
        login(app, OWNER_LOGIN, PASSWORD, OWNER_IDENTITY)
        response = app.post(
            "/api/auth/admin/accounts",
            headers=TAILSCALE_OWNER,
            json={
                "login": ADMIN_LOGIN,
                "role": "ADMIN",
                "bound_tailscale_identity": ADMIN_IDENTITY,
            },
        )
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        secret = payload["enrollment_secret"]
        secret_hash = hashlib.sha256(secret.encode()).hexdigest()
        assert payload["secret_disclosure"] == "one_time"
        assert payload["account"]["state"] == "PENDING"
        assert "password_hash" not in response.text and "token_hash" not in response.text
        accounts = app.get("/api/auth/admin/accounts", headers=TAILSCALE_OWNER)
        assert accounts.status_code == 200
        assert secret not in accounts.text and secret_hash not in accounts.text
        assert app.get(
            f"/api/auth/admin/accounts/{payload['account']['id']}/enrollment",
            headers=TAILSCALE_OWNER,
        ).status_code == 404
        enroll = app.post(
            "/api/auth/enroll",
            headers=TAILSCALE_ADMIN,
            json={"enrollment_secret": secret, "password": PASSWORD},
        )
        assert enroll.status_code == 200
        with client(database) as admin_app:
            login(admin_app, ADMIN_LOGIN, PASSWORD, ADMIN_IDENTITY)
            admin_session_token = admin_app.cookies.get("__Host-xauusd_session")
            assert admin_session_token
            sessions_path = f"/api/auth/admin/accounts/{payload['account']['id']}/sessions"
            listed = app.get(sessions_path, headers=TAILSCALE_OWNER)
            assert listed.status_code == 200
            assert "token_hash" not in listed.text and "session token" not in listed.text.lower()
            assert secret not in listed.text and secret_hash not in listed.text
            assert admin_session_token not in listed.text
            assert hashlib.sha256(admin_session_token.encode()).hexdigest() not in listed.text
            assert len(listed.json()["sessions"]) == 1
            revoked = app.delete(sessions_path, headers=TAILSCALE_OWNER)
            assert revoked.status_code == 200 and revoked.json()["revoked_count"] == 1
            assert admin_app.get(
                "/api/auth/session", headers=TAILSCALE_ADMIN
            ).json() == {"authenticated": False}
    with database.session() as session:
        enrollment = session.scalar(select(AuthEnrollmentRecord))
        events = session.scalars(select(AuthAuditRecord)).all()
        assert enrollment and enrollment.token_hash == secret_hash
        assert secret not in str(enrollment.__dict__)
        assert all(
            secret not in str(event.__dict__) and PASSWORD not in str(event.__dict__)
            for event in events
        )
        assert session.get(AuthUserRecord, owner_id) is not None
    assert secret not in caplog.text


def test_enrollment_expiry_is_enforced(database: Database) -> None:
    owner_id = bootstrap_owner(database)
    issue = invite_admin(database, owner_id)
    with database.session() as session:
        enrollment = session.scalar(select(AuthEnrollmentRecord))
        assert enrollment
        enrollment.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not service(database).activate_enrollment(
        secret=issue.secret, password=PASSWORD, tailscale_login=ADMIN_IDENTITY
    )
    with database.session() as session:
        expired = session.scalar(
            select(AuthAuditRecord).where(
                AuthAuditRecord.event_type == "ENROLLMENT_FAILURE",
                AuthAuditRecord.reason == "ENROLLMENT_EXPIRED",
            )
        )
        assert expired is not None


def test_wrong_role_is_rejected_by_internal_invitation_primitive(database: Database) -> None:
    owner_id = bootstrap_owner(database)
    with pytest.raises(ValueError, match="ADMIN role"):
        service(database).create_invitation(
            actor_id=owner_id,
            tailscale_login=OWNER_IDENTITY,
            login="new-owner@example.test",
            role="OWNER",
            bound_tailscale_login="new-owner@tailnet.example",
        )


def test_disable_blocks_account_and_revokes_sessions(database: Database) -> None:
    owner_id = bootstrap_owner(database)
    issue = invite_admin(database, owner_id)
    auth = service(database)
    assert auth.activate_enrollment(
        secret=issue.secret, password=PASSWORD, tailscale_login=ADMIN_IDENTITY
    )
    result = auth.login(ADMIN_LOGIN, PASSWORD, ADMIN_IDENTITY)
    assert result.user and result.token
    assert auth.authenticate(result.token, ADMIN_IDENTITY)
    auth.disable_account(
        actor_id=owner_id, tailscale_login=OWNER_IDENTITY, target_id=issue.account["id"]
    )
    assert auth.authenticate(result.token, ADMIN_IDENTITY) is None
    with database.session() as session:
        user = session.get(AuthUserRecord, issue.account["id"])
        session_record = session.scalar(select(AuthSessionRecord))
        assert user and user.state == "REVOKED"
        assert session_record and session_record.revoked_at is not None
    with pytest.raises(ValueError, match="sole OWNER"):
        auth.disable_account(actor_id=owner_id, tailscale_login=OWNER_IDENTITY, target_id=owner_id)


def test_owner_session_views_and_revoke_selected_and_all(database: Database) -> None:
    owner_id = bootstrap_owner(database)
    issue = invite_admin(database, owner_id)
    auth = service(database)
    assert auth.activate_enrollment(
        secret=issue.secret, password=PASSWORD, tailscale_login=ADMIN_IDENTITY
    )
    first = auth.login(ADMIN_LOGIN, PASSWORD, ADMIN_IDENTITY)
    second = auth.login(ADMIN_LOGIN, PASSWORD, ADMIN_IDENTITY)
    assert first.user and first.token and second.user and second.token
    records = auth.list_account_sessions(
        actor_id=owner_id, tailscale_login=OWNER_IDENTITY, target_id=issue.account["id"]
    )
    assert len(records) == 2
    assert all("token_hash" not in row and "token" not in row for row in records)
    with database.session() as session:
        first_hash = hashlib.sha256(first.token.encode()).hexdigest()
        first_record = session.scalar(
            select(AuthSessionRecord).where(AuthSessionRecord.token_hash == first_hash)
        )
        assert first_record
        first_session_id = first_record.id
    assert auth.revoke_account_session(
        actor_id=owner_id,
        tailscale_login=OWNER_IDENTITY,
        target_id=issue.account["id"],
        session_id=first_session_id,
    )
    assert auth.authenticate(first.token, ADMIN_IDENTITY) is None
    assert auth.authenticate(second.token, ADMIN_IDENTITY) is not None
    count = auth.revoke_all_account_sessions(
        actor_id=owner_id, tailscale_login=OWNER_IDENTITY, target_id=issue.account["id"]
    )
    assert count == 1
    assert auth.authenticate(first.token, ADMIN_IDENTITY) is None
    assert auth.authenticate(second.token, ADMIN_IDENTITY) is None


def test_owner_must_be_active_and_identity_bound(database: Database) -> None:
    owner_id = bootstrap_owner(database)
    with pytest.raises(PermissionError):
        service(database).list_accounts(actor_id=owner_id, tailscale_login="other@tailnet.example")
    with database.session() as session:
        owner = session.get(AuthUserRecord, owner_id)
        assert owner
        owner.state = "REVOKED"
    with pytest.raises(PermissionError):
        service(database).list_accounts(actor_id=owner_id, tailscale_login=OWNER_IDENTITY)


def test_audit_records_actor_target_without_password_or_enrollment_secret(
    database: Database,
) -> None:
    owner_id = bootstrap_owner(database)
    issue = invite_admin(database, owner_id)
    assert service(database).activate_enrollment(
        secret=issue.secret, password=PASSWORD, tailscale_login=ADMIN_IDENTITY
    )
    with database.session() as session:
        events = session.scalars(select(AuthAuditRecord)).all()
        assert {
            "OWNER_BOOTSTRAP",
            "ACCOUNT_CREATED",
            "ENROLLMENT_ISSUED",
            "ENROLLMENT_CONSUMED",
            "ACCOUNT_ACTIVATED",
        } <= {event.event_type for event in events}
        assert all(
            event.actor_user_id for event in events if event.event_type != "ENROLLMENT_FAILURE"
        )
        for event in events:
            content = str(event.__dict__)
            assert PASSWORD not in content
            assert issue.secret not in content


def test_session_and_account_api_routes_fail_closed_for_wrong_identity(database: Database) -> None:
    owner_id = bootstrap_owner(database)
    with client(database) as app:
        login(app, OWNER_LOGIN, PASSWORD, OWNER_IDENTITY)
        response = app.get(
            "/api/auth/admin/accounts",
            headers={"Tailscale-User-Login": "wrong@tailnet.example"},
        )
        assert response.status_code == 401
        assert (
            app.post(
                "/api/auth/admin/accounts/not-a-uuid/disable", headers=TAILSCALE_OWNER
            ).status_code
            == 404
        )
    assert owner_id


def test_owner_route_allowlist_is_exact_and_method_specific() -> None:
    account = "123e4567-e89b-12d3-a456-426614174000"
    session_id = "123e4567-e89b-12d3-a456-426614174001"
    assert is_auth_admin_route_allowed("/api/auth/admin/accounts", "GET")
    assert is_auth_admin_route_allowed("/api/auth/admin/accounts", "POST")
    assert is_auth_admin_route_allowed(
        f"/api/auth/admin/accounts/{account}/disable", "POST"
    )
    assert is_auth_admin_route_allowed(
        f"/api/auth/admin/accounts/{account}/sessions", "GET"
    )
    assert is_auth_admin_route_allowed(
        f"/api/auth/admin/accounts/{account}/sessions", "DELETE"
    )
    assert is_auth_admin_route_allowed(
        f"/api/auth/admin/accounts/{account}/sessions/{session_id}", "DELETE"
    )
    assert not is_auth_admin_route_allowed(
        f"/api/auth/admin/accounts/{account}/disable", "GET"
    )
    assert not is_auth_admin_route_allowed(
        f"/api/auth/admin/accounts/{account}/promote", "POST"
    )
    assert not is_auth_admin_route_allowed(
        f"/api/auth/admin/accounts/{account}/sessions/{session_id}/token", "GET"
    )


def test_0014_to_0015_migration_and_downgrade_preserve_existing_auth_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "phase353-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    command.upgrade(config, "20260924_0014")
    command.upgrade(config, "20260924_0015")
    migrated = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        tables = set(inspect(migrated.engine).get_table_names())
        assert "auth_enrollments" in tables
        with migrated.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO auth_users "
                "(id, normalized_login, password_hash, role, state, bound_tailscale_login, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "owner-id",
                    OWNER_LOGIN,
                    "$argon2id$test",
                    "OWNER",
                    "ACTIVE",
                    OWNER_IDENTITY,
                    "2026-09-24 00:00:00",
                    "2026-09-24 00:00:00",
                ),
            )
            connection.exec_driver_sql(
                "INSERT INTO auth_audit_events "
                "(id, timestamp, event_type, actor_user_id, user_id, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "audit-id",
                    "2026-09-24 00:00:00",
                    "OWNER_BOOTSTRAP",
                    "owner-id",
                    "owner-id",
                    "SUCCEEDED",
                ),
            )
        command.downgrade(config, "20260924_0014")
        with migrated.engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "20260924_0014"
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT normalized_login FROM auth_users WHERE id='owner-id'"
                ).scalar_one()
                == OWNER_LOGIN
            )
            assert "actor_user_id" not in {
                column["name"] for column in inspect(connection).get_columns("auth_audit_events")
            }
        assert "auth_enrollments" not in inspect(migrated.engine).get_table_names()
    finally:
        migrated.dispose()
