from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.app import create_app
from config.settings import Settings
from persistence.database import Database


@pytest.fixture
def private_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'private-dashboard.db').as_posix()}")
    database.create_schema()
    yield database
    database.dispose()


@pytest.fixture
def private_client(private_database: Database) -> TestClient:
    app = create_app(
        settings=Settings(remote_dashboard_mode=True),
        database=private_database,
    )
    with TestClient(app) as client:
        yield client


def test_private_dashboard_requires_tailscale_identity(private_client: TestClient) -> None:
    response = private_client.get("/api/system/health")
    assert response.status_code == 401
    assert response.json() == {"detail": "Tailscale identity required"}


def test_private_dashboard_allows_only_authenticated_allowlisted_get(
    private_client: TestClient,
) -> None:
    headers = {"Tailscale-User-Login": "operator@example.com"}
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
    with pytest.raises(WebSocketDisconnect) as error:
        with private_client.websocket_connect(
            "/ws/live",
            headers={"Tailscale-User-Login": "operator@example.com"},
        ):
            pass
    assert error.value.code == 1008
