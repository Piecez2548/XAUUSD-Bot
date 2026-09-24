from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from alembic import command
from api.app import create_app
from config.settings import Settings
from persistence.database import Database
from persistence.orm import AuthAuditRecord, AuthSessionRecord, AuthUserRecord
from services.authentication import (
    SESSION_COOKIE_NAME,
    AuthenticationService,
)

TAILSCALE = {
    "Tailscale-User-Login": "operator@example-tailnet.test",
    "Origin": "https://dashboard.tailnet.test",
}
PASSWORD = "Correct Horse Battery Staple 42!"


class FakeControlIpc:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def request(self, command: str, operation_id: str, *, actor: str) -> dict[str, object]:
        self.calls.append((command, operation_id, actor))
        return {
            "ok": True,
            "duplicate": False,
            "operation_id": operation_id,
            "command": command,
            "message": "ok",
            "status": {},
        }


@pytest.fixture
def auth_database(tmp_path: Path) -> Database:
    db = Database.for_test(f"sqlite:///{(tmp_path / 'auth.db').as_posix()}")
    db.create_test_schema()
    yield db
    db.dispose()


def create_active_user(database: Database, *, state: str = "ACTIVE") -> str:
    return AuthenticationService(database, Settings()).create_user_for_admin(
        login="Operator@Example.test",
        password=PASSWORD,
        role="ADMIN",
        state=state,
        bound_tailscale_login="operator@example-tailnet.test",
    )


def make_client(database: Database, ipc=None) -> TestClient:
    return TestClient(
        create_app(
            settings=Settings(
                remote_dashboard_mode=True,
                csrf_trusted_origins=("https://dashboard.tailnet.test",),
            ),
            database=database,
            control_ipc=ipc,
        ),
        base_url="https://dashboard.tailnet.test",
    )


def login(client: TestClient, password: str = PASSWORD):
    return client.post(
        "/api/auth/login",
        headers=TAILSCALE,
        json={"login": " OPERATOR@example.test ", "password": password},
    )


def mutation_headers(client: TestClient) -> dict[str, str]:
    response = client.get("/api/auth/csrf", headers=TAILSCALE)
    assert response.status_code == 200
    return {**TAILSCALE, "X-CSRF-Token": response.json()["csrf_token"]}


def test_argon2id_password_hash_and_verification(auth_database: Database) -> None:
    service = AuthenticationService(auth_database, Settings())
    encoded = service.hash_password(PASSWORD)
    assert encoded.startswith("$argon2id$")
    assert encoded != PASSWORD
    assert PASSWORD not in encoded
    assert service.verify_password(encoded, PASSWORD)
    assert not service.verify_password(encoded, "wrong password")


def test_login_sets_secure_cookie_and_never_returns_token(auth_database: Database) -> None:
    create_active_user(auth_database)
    with make_client(auth_database) as client:
        response = login(client)
        assert response.status_code == 200
        assert response.json()["authenticated"] is True
        assert response.json()["user"]["role"] == "ADMIN"
        assert SESSION_COOKIE_NAME not in response.text
        assert "token_hash" not in response.text and "password_hash" not in response.text
        cookie = response.headers["set-cookie"]
        assert f"{SESSION_COOKIE_NAME}=" in cookie
        assert "httponly" in cookie.lower()
        assert "secure" in cookie.lower()
        assert "samesite=strict" in cookie.lower()
        assert "path=/" in cookie.lower()
        assert "domain=" not in cookie.lower()
        assert client.get("/api/auth/session", headers=TAILSCALE).json()["authenticated"]


def test_unknown_and_wrong_password_are_generic(auth_database: Database) -> None:
    create_active_user(auth_database)
    with make_client(auth_database) as client:
        wrong = login(client, "incorrect password 123!")
        unknown = client.post(
            "/api/auth/login",
            headers=TAILSCALE,
            json={"login": "nobody@example.test", "password": "incorrect password 123!"},
        )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Invalid login or password"}
    assert SESSION_COOKIE_NAME not in wrong.headers.get("set-cookie", "")


@pytest.mark.parametrize("state", ["PROVISIONED", "PENDING_APPROVAL", "LOCKED", "REVOKED"])
def test_non_active_user_cannot_login(auth_database: Database, state: str) -> None:
    create_active_user(auth_database, state=state)
    with make_client(auth_database) as client:
        response = login(client)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid login or password"}


def test_login_requires_matching_tailscale_identity(auth_database: Database) -> None:
    create_active_user(auth_database)
    with make_client(auth_database) as client:
        response = client.post(
            "/api/auth/login",
            headers={
                "Tailscale-User-Login": "different@example-tailnet.test",
                "Origin": "https://dashboard.tailnet.test",
            },
            json={"login": "operator@example.test", "password": PASSWORD},
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid login or password"}


def test_login_requires_tailscale_and_no_registration_exists(auth_database: Database) -> None:
    create_active_user(auth_database)
    with make_client(auth_database) as client:
        assert client.post(
            "/api/auth/login", json={"login": "operator@example.test", "password": PASSWORD}
        ).status_code == 401
        assert client.post("/api/auth/register", headers=TAILSCALE).status_code == 404
        assert client.post("/api/register", headers=TAILSCALE).status_code == 404


def test_malformed_login_never_echoes_submitted_password(auth_database: Database) -> None:
    secret = "do-not-reflect-this-password"
    with make_client(auth_database) as client:
        response = client.post(
            "/api/auth/login",
            headers=TAILSCALE,
            json={"login": "operator", "password": secret, "extra": secret},
        )
    assert response.status_code == 400
    assert secret not in response.text


def test_private_read_and_control_routes_require_app_session(auth_database: Database) -> None:
    create_active_user(auth_database)
    ipc = FakeControlIpc()
    with make_client(auth_database, ipc) as client:
        assert client.get("/api/system/health", headers=TAILSCALE).status_code == 401
        assert client.get("/api/control/status", headers=TAILSCALE).status_code == 401
        assert client.post(
            "/api/control/start", headers=TAILSCALE, json={"operation_id": str(uuid4())}
        ).status_code == 401
        assert not ipc.calls
        assert login(client).status_code == 200
        assert client.get("/api/system/health", headers=TAILSCALE).status_code == 200
        assert client.get("/api/control/status", headers=TAILSCALE).status_code == 200
        started = client.post(
            "/api/control/start",
            headers=mutation_headers(client),
            json={"operation_id": str(uuid4())},
        )
        assert started.status_code == 200
        assert ipc.calls[0][0] == "status"
        assert ipc.calls[1][0] == "start"
        assert ipc.calls[1][2] == "operator@example.test"


def test_auth_database_lookup_failure_fails_closed(auth_database: Database, monkeypatch) -> None:
    create_active_user(auth_database)
    app = create_app(
            settings=Settings(
                remote_dashboard_mode=True,
                csrf_trusted_origins=("https://dashboard.tailnet.test",),
            ),
        database=auth_database,
    )

    def fail_lookup(*_args, **_kwargs):
        raise RuntimeError("simulated storage outage")

    monkeypatch.setattr(app.state.auth_service, "authenticate", fail_lookup)
    with TestClient(app, base_url="https://dashboard.tailnet.test") as client:
        response = client.get("/api/system/health", headers=TAILSCALE)
    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication service unavailable"}


@pytest.mark.parametrize("cookie", [None, "not-a-valid-token", "random-session-token"])
def test_missing_malformed_and_random_sessions_deny_private_api(
    auth_database: Database, cookie: str | None
) -> None:
    create_active_user(auth_database)
    headers = dict(TAILSCALE)
    if cookie:
        headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie}"
    with make_client(auth_database) as client:
        assert client.get("/api/system/health", headers=headers).status_code == 401


def _set_session_time(
    database: Database, token: str, *, idle: datetime, absolute: datetime | None = None
) -> None:
    import hashlib

    with database.session() as db_session:
        record = db_session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hashlib.sha256(token.encode()).hexdigest()
            )
        )
        assert record is not None
        record.idle_expires_at = idle
        if absolute is not None:
            record.absolute_expires_at = absolute


@pytest.mark.parametrize("expiry_kind", ["idle", "absolute"])
def test_expired_session_denied_and_audited(auth_database: Database, expiry_kind: str) -> None:
    create_active_user(auth_database)
    with make_client(auth_database) as client:
        response = login(client)
        assert response.status_code == 200
        token = client.cookies.get(SESSION_COOKIE_NAME)
        assert token
        past = datetime.now(UTC) - timedelta(seconds=1)
        if expiry_kind == "idle":
            _set_session_time(auth_database, token, idle=past)
        else:
            _set_session_time(
                auth_database,
                token,
                idle=datetime.now(UTC) + timedelta(minutes=1),
                absolute=past,
            )
        assert client.get("/api/system/health", headers=TAILSCALE).status_code == 401
    with auth_database.session() as session:
        assert session.scalar(
            select(AuthAuditRecord).where(AuthAuditRecord.event_type == "SESSION_EXPIRED")
        ) is not None


def test_revoked_session_user_state_change_and_identity_mismatch_deny(
    auth_database: Database,
) -> None:
    user_id = create_active_user(auth_database)
    with make_client(auth_database) as client:
        assert login(client).status_code == 200
        token = client.cookies.get(SESSION_COOKIE_NAME)
        assert token
        other_identity = {"Tailscale-User-Login": "other@example-tailnet.test"}
        assert client.get("/api/system/health", headers=other_identity).status_code == 401
        assert client.get("/api/system/health", headers=TAILSCALE).status_code == 200
        with auth_database.session() as session:
            user = session.get(AuthUserRecord, user_id)
            assert user
            user.state = "LOCKED"
        assert client.get("/api/system/health", headers=TAILSCALE).status_code == 401
        with auth_database.session() as session:
            user = session.get(AuthUserRecord, user_id)
            assert user
            user.state = "ACTIVE"
        client.post("/api/auth/logout", headers=mutation_headers(client))
        assert client.get("/api/system/health", headers=TAILSCALE).status_code == 401
        assert client.get("/api/auth/session", headers=TAILSCALE).json() == {"authenticated": False}


def test_deleted_user_session_fails_closed(auth_database: Database) -> None:
    user_id = create_active_user(auth_database)
    with make_client(auth_database) as client:
        assert login(client).status_code == 200
        with auth_database.session() as session:
            session.delete(session.get(AuthUserRecord, user_id))
        assert client.get("/api/system/health", headers=TAILSCALE).status_code == 401


def test_login_logout_audit_contains_no_password_or_token(auth_database: Database) -> None:
    create_active_user(auth_database)
    with make_client(auth_database) as client:
        assert login(client, "wrong secret 123!").status_code == 401
        assert login(client).status_code == 200
        token = client.cookies.get(SESSION_COOKIE_NAME)
        assert token
        assert client.post(
            "/api/auth/logout", headers=mutation_headers(client)
        ).status_code == 200
    with auth_database.session() as session:
        events = list(session.scalars(select(AuthAuditRecord)))
        assert {event.event_type for event in events} >= {
            "LOGIN_FAILURE",
            "LOGIN_SUCCESS",
            "LOGOUT",
            "SESSION_REVOKED",
        }
        audit_text = repr(events)
        assert PASSWORD not in audit_text
        assert token not in audit_text


def test_auth_migration_upgrades_temporary_database(tmp_path: Path) -> None:
    db_path = tmp_path / "migration.db"
    prior_schema = Database(f"sqlite:///{db_path.as_posix()}")
    prior_schema.create_schema()
    with prior_schema.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES ('20260924_0013')"
        )
    prior_schema.dispose()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    command.upgrade(config, "20260924_0014")
    migrated = Database(f"sqlite:///{db_path.as_posix()}")
    try:
        with migrated.engine.connect() as connection:
            table_names = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).scalars()
            tables = set(table_names)
        assert {"auth_users", "auth_sessions", "auth_audit_events"} <= tables
    finally:
        migrated.dispose()
