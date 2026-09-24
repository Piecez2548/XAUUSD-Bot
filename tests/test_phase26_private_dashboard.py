from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.app import create_app
from config.settings import Settings
from persistence.database import Database
from services.authentication import AuthenticationService


@pytest.fixture
def private_database(tmp_path: Path) -> Database:
    database = Database.for_test(f"sqlite:///{(tmp_path / 'private-dashboard.db').as_posix()}")
    database.create_test_schema()
    yield database
    database.dispose()


@pytest.fixture
def private_client(private_database: Database) -> TestClient:
    app = create_app(
        settings=Settings(
            remote_dashboard_mode=True,
            csrf_trusted_origins=("https://dashboard.tailnet.test",),
        ),
        database=private_database,
    )
    with TestClient(app, base_url="https://dashboard.tailnet.test") as client:
        yield client


def test_private_dashboard_requires_tailscale_identity(private_client: TestClient) -> None:
    response = private_client.get("/api/system/health")
    assert response.status_code == 401
    assert response.json() == {"detail": "Tailscale identity required"}


def test_private_dashboard_allows_only_authenticated_allowlisted_get(
    private_client: TestClient,
) -> None:
    headers = {
        "Tailscale-User-Login": "operator@example.com",
        "Origin": "https://dashboard.tailnet.test",
    }
    AuthenticationService(private_client.app.state.database, Settings()).create_user_for_admin(
        login="operator@example.com",
        password="correct horse battery staple 123!",
        role="ADMIN",
        state="ACTIVE",
        bound_tailscale_login="operator@example.com",
    )
    assert private_client.post(
        "/api/auth/login",
        headers=headers,
        json={"login": "operator@example.com", "password": "correct horse battery staple 123!"},
    ).status_code == 200
    allowed = private_client.get("/api/system/health", headers=headers)
    denied = private_client.get("/api/health", headers=headers)
    unknown = private_client.get("/api/unknown", headers=headers)

    assert allowed.status_code == 200
    assert denied.status_code == 404
    assert unknown.status_code == 404


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_private_dashboard_denies_mutating_methods(
    private_client: TestClient, method: str
) -> None:
    response = getattr(private_client, method)(
        "/api/system/health",
        headers={"Tailscale-User-Login": "operator@example.com"},
    )
    assert response.status_code == 404


def test_private_dashboard_denies_websocket_phase_until_wss_is_explicitly_secured(
    private_client: TestClient,
) -> None:
    with pytest.raises(WebSocketDisconnect) as error, private_client.websocket_connect(
        "/ws/live",
        headers={"Tailscale-User-Login": "operator@example.com"},
    ):
        pass
    assert error.value.code == 1008
