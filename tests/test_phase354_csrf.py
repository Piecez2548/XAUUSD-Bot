from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.app import create_app
from config.csrf_policy import MutationClass, classify_mutation, is_trusted_origin, origin_key
from config.settings import ConfigurationError, Settings
from persistence.database import Database
from persistence.orm import AuthAuditRecord, AuthSessionRecord, AuthUserRecord
from services.authentication import SESSION_COOKIE_NAME, AuthenticationService
from services.csrf import validate_csrf_token

ORIGIN = "https://dashboard.tailnet.test"
OWNER_IDENTITY = "owner@tailnet.test"
ADMIN_IDENTITY = "admin@tailnet.test"
OWNER_LOGIN = "owner@example.test"
ADMIN_LOGIN = "admin@example.test"
PASSWORD = "A sufficiently long test-only password 48!"
OWNER_HEADERS = {"Tailscale-User-Login": OWNER_IDENTITY, "Origin": ORIGIN}
ADMIN_HEADERS = {"Tailscale-User-Login": ADMIN_IDENTITY, "Origin": ORIGIN}


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
def database(tmp_path: Path) -> Database:
    value = Database.for_test(f"sqlite:///{(tmp_path / 'phase354-csrf.db').as_posix()}")
    value.create_test_schema()
    yield value
    value.dispose()


@pytest.fixture
def setup(database: Database):
    ipc = FakeControlIpc()
    settings = Settings(
        remote_dashboard_mode=True,
        csrf_trusted_origins=(ORIGIN,),
    )
    owner_id = AuthenticationService(database, settings).bootstrap_owner(
        login=OWNER_LOGIN,
        password=PASSWORD,
        bound_tailscale_login=OWNER_IDENTITY,
    )
    app = create_app(settings=settings, database=database, control_ipc=ipc)
    return database, ipc, owner_id, app


def owner_client(app) -> TestClient:
    client = TestClient(app, base_url=ORIGIN)
    response = client.post(
        "/api/auth/login",
        headers=OWNER_HEADERS,
        json={"login": OWNER_LOGIN, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return client


def csrf_headers(client: TestClient, identity: dict[str, str] = OWNER_HEADERS) -> dict[str, str]:
    response = client.get("/api/auth/csrf", headers=identity)
    assert response.status_code == 200, response.text
    return {**identity, "X-CSRF-Token": response.json()["csrf_token"]}


def control_start(client: TestClient, headers: dict[str, str]) -> object:
    return client.post(
        "/api/control/start",
        headers=headers,
        json={"operation_id": str(uuid4())},
    )


def test_csrf_tokens_are_random_session_bound_and_not_persisted(
    setup, caplog: pytest.LogCaptureFixture
) -> None:
    database, _ipc, _owner_id, app = setup
    with owner_client(app) as client:
        first_response = client.get("/api/auth/csrf", headers=OWNER_HEADERS)
        second_response = client.get("/api/auth/csrf", headers=OWNER_HEADERS)
        assert first_response.status_code == second_response.status_code == 200
        assert first_response.headers["cache-control"] == "no-store"
        first = first_response.json()["csrf_token"]
        second = second_response.json()["csrf_token"]
        assert len(first) == 86 and len(base64.urlsafe_b64decode(first + "==")) == 64
        assert first != second
        cookie = client.cookies.get(SESSION_COOKIE_NAME)
        assert cookie and validate_csrf_token(cookie, first)
        assert not validate_csrf_token("another-session-cookie", first)
        assert not validate_csrf_token(cookie, "malformed")
        assert first not in caplog.text
        with database.session() as session:
            auth_sessions = session.scalars(select(AuthSessionRecord)).all()
            audit_events = session.scalars(select(AuthAuditRecord)).all()
            assert auth_sessions
            assert all(first not in repr(row.__dict__) for row in auth_sessions)
            assert all(first not in repr(row.__dict__) for row in audit_events)
            session_columns = {
                column.name.lower() for column in AuthSessionRecord.__table__.columns
            }
            assert "csrf" not in session_columns


def test_csrf_token_requires_active_authenticated_session(setup) -> None:
    _database, _ipc, _owner_id, app = setup
    with TestClient(app, base_url=ORIGIN) as client:
        missing = client.get("/api/auth/csrf", headers=OWNER_HEADERS)
        assert missing.status_code == 401 and missing.json()["code"] == "AUTH_REQUIRED"


def test_valid_csrf_origin_and_session_reach_control_authority(setup) -> None:
    _database, ipc, _owner_id, app = setup
    with owner_client(app) as client:
        response = control_start(client, csrf_headers(client))
    assert response.status_code == 200
    assert len(ipc.calls) == 1 and ipc.calls[0][0] == "start"


@pytest.mark.parametrize("csrf_header", [None, "", "malformed", "A" * 86])
def test_missing_or_invalid_csrf_never_reaches_control(setup, csrf_header: str | None) -> None:
    _database, ipc, _owner_id, app = setup
    with owner_client(app) as client:
        headers = dict(OWNER_HEADERS)
        if csrf_header is not None:
            headers["X-CSRF-Token"] = csrf_header
        response = control_start(client, headers)
    assert response.status_code == 403 and response.json() == {"code": "CSRF_REJECTED"}
    assert ipc.calls == []


def test_csrf_query_parameter_is_not_accepted(setup) -> None:
    _database, ipc, _owner_id, app = setup
    with owner_client(app) as client:
        token = csrf_headers(client)["X-CSRF-Token"]
        response = client.post(
            f"/api/control/start?csrf_token={token}",
            headers=OWNER_HEADERS,
            json={"operation_id": str(uuid4())},
        )
    assert response.status_code == 403 and response.json()["code"] == "CSRF_REJECTED"
    assert ipc.calls == []


@pytest.mark.parametrize(
    "origin",
    [None, "null", "https://dashboard.tailnet.test.evil", "http://dashboard.tailnet.test",
     "https://dashboard.tailnet.test:444", "https://dashboard.tailnet.test/path",
     "https://user@dashboard.tailnet.test", "not an origin"],
)
def test_untrusted_or_malformed_origin_never_reaches_control(setup, origin: str | None) -> None:
    _database, ipc, _owner_id, app = setup
    with owner_client(app) as client:
        headers = csrf_headers(client)
        if origin is None:
            headers.pop("Origin")
        else:
            headers["Origin"] = origin
        response = control_start(client, headers)
    assert response.status_code == 403 and response.json() == {"code": "CSRF_REJECTED"}
    assert ipc.calls == []


def test_fetch_metadata_rejects_cross_site_before_control_authority(setup) -> None:
    _database, ipc, _owner_id, app = setup
    with owner_client(app) as client:
        headers = {**csrf_headers(client), "Sec-Fetch-Site": "cross-site"}
        response = control_start(client, headers)
    assert response.status_code == 403 and response.json()["code"] == "CSRF_REJECTED"
    assert ipc.calls == []


def test_session_a_token_and_session_b_cookie_are_not_interchangeable(setup) -> None:
    _database, ipc, _owner_id, app = setup
    with owner_client(app) as client:
        token_a = csrf_headers(client)["X-CSRF-Token"]
        cookie_a = client.cookies.get(SESSION_COOKIE_NAME)
        replacement = client.post(
            "/api/auth/login",
            headers=OWNER_HEADERS,
            json={"login": OWNER_LOGIN, "password": PASSWORD},
        )
        assert replacement.status_code == 200
        token_b = csrf_headers(client)["X-CSRF-Token"]
        cookie_b = client.cookies.get(SESSION_COOKIE_NAME)
        assert cookie_a and cookie_b and cookie_a != cookie_b
        for cookie, csrf in ((cookie_b, token_a), (cookie_a, token_b)):
            response = control_start(
                client,
                {
                    **OWNER_HEADERS,
                    "Cookie": f"{SESSION_COOKIE_NAME}={cookie}",
                    "X-CSRF-Token": csrf,
                },
            )
            assert response.status_code == 403
    assert ipc.calls == []


@pytest.mark.parametrize("invalidator", ["revoked", "expired", "disabled", "wrong_identity"])
def test_invalid_session_or_identity_denies_mutation_even_with_csrf(
    setup, invalidator: str
) -> None:
    database, ipc, owner_id, app = setup
    with owner_client(app) as client:
        token = csrf_headers(client)["X-CSRF-Token"]
        cookie = client.cookies.get(SESSION_COOKIE_NAME)
        assert cookie
        service = AuthenticationService(database, Settings())
        if invalidator == "revoked":
            service.revoke(cookie, OWNER_IDENTITY)
        elif invalidator == "expired":
            with database.session() as session:
                row = session.scalar(select(AuthSessionRecord))
                assert row
                row.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif invalidator == "disabled":
            with database.session() as session:
                user = session.get(AuthUserRecord, owner_id)
                assert user
                user.state = "REVOKED"
        headers = {
            **(ADMIN_HEADERS if invalidator == "wrong_identity" else OWNER_HEADERS),
            "Cookie": f"{SESSION_COOKIE_NAME}={cookie}",
            "X-CSRF-Token": token,
        }
        response = control_start(client, headers)
    assert response.status_code == 401 and response.json()["code"] == "AUTH_REQUIRED"
    assert ipc.calls == []


def test_logout_requires_csrf_revokes_session_and_invalidates_old_authority(setup) -> None:
    _database, ipc, _owner_id, app = setup
    with owner_client(app) as client:
        cookie = client.cookies.get(SESSION_COOKIE_NAME)
        csrf = csrf_headers(client)["X-CSRF-Token"]
        assert cookie
        denied = client.post("/api/auth/logout", headers=OWNER_HEADERS, json={})
        assert denied.status_code == 403
        logged_out = client.post(
            "/api/auth/logout",
            headers={**OWNER_HEADERS, "X-CSRF-Token": csrf},
            json={},
        )
        assert logged_out.status_code == 200
        assert logged_out.headers.get("set-cookie")
        old_authority = control_start(
            client,
            {
                **OWNER_HEADERS,
                "Cookie": f"{SESSION_COOKIE_NAME}={cookie}",
                "X-CSRF-Token": csrf,
            },
        )
        assert old_authority.status_code == 401
    assert ipc.calls == []


def test_login_and_enrollment_are_narrow_origin_protected_pre_auth_exceptions(
    setup, database: Database
) -> None:
    _database, _ipc, owner_id, app = setup
    with TestClient(app, base_url=ORIGIN) as client:
        bad_origin = client.post(
            "/api/auth/login",
            headers={"Tailscale-User-Login": OWNER_IDENTITY, "Origin": "https://evil.test"},
            json={"login": OWNER_LOGIN, "password": PASSWORD},
        )
        assert bad_origin.status_code == 403
        login = client.post(
            "/api/auth/login", headers=OWNER_HEADERS,
            json={"login": OWNER_LOGIN, "password": PASSWORD},
        )
        assert login.status_code == 200
        issue = AuthenticationService(database, Settings()).create_invitation(
            actor_id=owner_id,
            tailscale_login=OWNER_IDENTITY,
            login=ADMIN_LOGIN,
            role="ADMIN",
            bound_tailscale_login=ADMIN_IDENTITY,
        )
        enrollment = client.post(
            "/api/auth/enroll",
            headers=ADMIN_HEADERS,
            json={"enrollment_secret": issue.secret, "password": PASSWORD},
        )
        assert enrollment.status_code == 200
        wrong_origin_enrollment = client.post(
            "/api/auth/enroll",
            headers={"Tailscale-User-Login": ADMIN_IDENTITY, "Origin": "https://evil.test"},
            json={"enrollment_secret": "x" * 64, "password": PASSWORD},
        )
        assert wrong_origin_enrollment.status_code == 403


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/api/auth/logout", {}),
        (
            "POST",
            "/api/auth/admin/accounts",
            {"login": "x", "role": "ADMIN", "bound_tailscale_identity": "x"},
        ),
        ("POST", "/api/auth/admin/accounts/123e4567-e89b-12d3-a456-426614174000/disable", {}),
        ("DELETE", "/api/auth/admin/accounts/123e4567-e89b-12d3-a456-426614174000/sessions", {}),
        (
            "DELETE",
            "/api/auth/admin/accounts/123e4567-e89b-12d3-a456-426614174000/sessions/123e4567-e89b-12d3-a456-426614174001",
            {},
        ),
    ],
)
def test_all_owner_mutations_reject_missing_csrf(setup, method: str, path: str, body: dict) -> None:
    _database, _ipc, _owner_id, app = setup
    with owner_client(app) as client:
        response = client.request(method, path, headers=OWNER_HEADERS, json=body)
    assert response.status_code == 403 and response.json() == {"code": "CSRF_REJECTED"}


def test_admin_with_valid_csrf_still_cannot_perform_owner_operation(
    setup, database: Database
) -> None:
    _database, _ipc, owner_id, app = setup
    issue = AuthenticationService(database, Settings()).create_invitation(
        actor_id=owner_id,
        tailscale_login=OWNER_IDENTITY,
        login=ADMIN_LOGIN,
        role="ADMIN",
        bound_tailscale_login=ADMIN_IDENTITY,
    )
    assert AuthenticationService(database, Settings()).activate_enrollment(
        secret=issue.secret, password=PASSWORD, tailscale_login=ADMIN_IDENTITY
    )
    with TestClient(app, base_url=ORIGIN) as admin:
        login = admin.post(
            "/api/auth/login", headers=ADMIN_HEADERS,
            json={"login": ADMIN_LOGIN, "password": PASSWORD},
        )
        assert login.status_code == 200
        response = admin.post(
            "/api/auth/admin/accounts",
            headers=csrf_headers(admin, ADMIN_HEADERS),
            json={
                "login": "other@example.test",
                "role": "ADMIN",
                "bound_tailscale_identity": "other@tailnet.test",
            },
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "FORBIDDEN"


@pytest.mark.parametrize(
    "path",
    [
        "/api/control/start",
        "/api/control/stop",
        "/api/control/restart",
        "/api/control/demo-on",
        "/api/control/demo-off",
    ],
)
def test_lifecycle_and_demo_csrf_rejection_does_not_reach_authority(setup, path: str) -> None:
    _database, ipc, _owner_id, app = setup
    with owner_client(app) as client:
        response = client.post(path, headers=OWNER_HEADERS, json={"operation_id": str(uuid4())})
        assert response.status_code == 403
    assert ipc.calls == []


def test_safe_methods_and_unknown_mutations_never_reach_lifecycle_authority(setup) -> None:
    _database, ipc, _owner_id, app = setup
    with owner_client(app) as client:
        assert client.get("/api/control/status", headers=OWNER_HEADERS).status_code == 200
        for method in ("HEAD", "OPTIONS"):
            response = client.request(
                method,
                "/api/control/start",
                headers={**OWNER_HEADERS, "X-CSRF-Token": "not-used"},
            )
            assert response.status_code in {404, 405}
        unknown = client.post(
            "/api/control/unknown",
            headers=csrf_headers(client),
            json={"operation_id": str(uuid4())},
        )
        assert unknown.status_code == 404
    assert all(command == "status" for command, _operation, _actor in ipc.calls)


def test_route_table_mutations_are_all_explicitly_classified(setup) -> None:
    _database, _ipc, _owner_id, app = setup
    inventory: list[tuple[str, str, MutationClass]] = []
    for route in app.routes:
        for method in sorted(getattr(route, "methods", None) or ()):
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                classification = classify_mutation(method, route.path)
                assert classification is not None, (
                    f"unclassified private mutation: {method} {route.path}"
                )
                inventory.append((method, route.path, classification))
    assert {entry[2] for entry in inventory} == {
        MutationClass.CSRF_REQUIRED,
        MutationClass.PRE_AUTH_EXCEPTION,
    }
    assert ("POST", "/api/auth/login", MutationClass.PRE_AUTH_EXCEPTION) in inventory
    assert ("POST", "/api/auth/enroll", MutationClass.PRE_AUTH_EXCEPTION) in inventory
    assert sum(entry[2] == MutationClass.CSRF_REQUIRED for entry in inventory) == 10


def test_origin_parser_is_exact_and_settings_reject_wildcards() -> None:
    assert origin_key(ORIGIN) == ("https", "dashboard.tailnet.test", 443)
    assert is_trusted_origin("https://dashboard.tailnet.test:443", (ORIGIN,))
    assert is_trusted_origin("http://dashboard.tailnet.test:80", ("http://dashboard.tailnet.test",))
    assert not is_trusted_origin("http://dashboard.tailnet.test", (ORIGIN,))
    assert not is_trusted_origin("https://dashboard.tailnet.test.evil", (ORIGIN,))
    assert not is_trusted_origin("https://dashboard.tailnet.test/path", (ORIGIN,))
    with pytest.raises(ConfigurationError):
        Settings(csrf_trusted_origins=("https://*.tailnet.test",))


@pytest.mark.parametrize(
    "origin",
    [
        "https://dashboard.tailnet.test:",
        "http://dashboard.tailnet.test:",
        "https://dashboard.tailnet.test:abc",
        "https://dashboard.tailnet.test:65536",
        "https://user@dashboard.tailnet.test",
        "https://dashboard.tailnet.test/path",
        "https://dashboard.tailnet.test?query=1",
        "https://dashboard.tailnet.test?",
        "https://dashboard.tailnet.test#fragment",
        "https://dashboard.tailnet.test#",
        "https://[2001:db8::1",
        "https://[not-ipv6]",
        "ftp://dashboard.tailnet.test",
        "https://dashboard.tailnet.test:443:444",
        "https://dashboard.tailnet.test::443",
    ],
)
def test_origin_parser_rejects_malformed_or_ambiguous_authorities(origin: str) -> None:
    assert origin_key(origin) is None
    assert not is_trusted_origin(origin, (ORIGIN,))


@pytest.mark.parametrize(
    "path",
    [
        "/api/control/start",
        "/api/control/stop",
        "/api/control/restart",
        "/api/control/demo-on",
        "/api/control/demo-off",
    ],
)
def test_empty_origin_port_is_rejected_before_control_or_demo_authority(setup, path: str) -> None:
    _database, ipc, _owner_id, app = setup
    with owner_client(app) as client:
        headers = csrf_headers(client)
        headers["Origin"] = "https://dashboard.tailnet.test:"
        response = client.post(
            path,
            headers=headers,
            json={"operation_id": str(uuid4())},
        )
    assert response.status_code == 403
    assert response.json() == {"code": "CSRF_REJECTED"}
    assert ipc.calls == []


def test_csrf_rejections_are_not_audited_or_logged(setup, caplog: pytest.LogCaptureFixture) -> None:
    database, ipc, _owner_id, app = setup
    with owner_client(app) as client:
        with database.session() as session:
            before_count = len(session.scalars(select(AuthAuditRecord)).all())
        response = control_start(client, OWNER_HEADERS)
    assert response.status_code == 403 and not ipc.calls
    with database.session() as session:
        after_count = len(session.scalars(select(AuthAuditRecord)).all())
        assert after_count == before_count
    assert "CSRF" not in caplog.text
