from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api.app import create_app
from config.remote_read_only_policy import is_private_control_path_allowed
from config.settings import ConfigurationError, Settings, _origins
from persistence.database import Database


@pytest.fixture
def remote_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'remote.db').as_posix()}")
    database.create_schema()
    yield database
    database.dispose()


def test_production_cors_rejects_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "*")
    with pytest.raises(ConfigurationError, match="wildcard"):
        _origins("CORS_ORIGINS", ("http://localhost:5173",))
    with pytest.raises(ConfigurationError, match="wildcard"):
        Settings(cors_origins=("*",))


def test_cors_allows_only_configured_dashboard_origin(remote_database: Database) -> None:
    app = create_app(
        settings=Settings(cors_origins=("https://dashboard.example",)),
        database=remote_database,
    )
    with TestClient(app) as client:
        allowed = client.options(
            "/api/system/health",
            headers={"Origin": "https://dashboard.example", "Access-Control-Request-Method": "GET"},
        )
        denied = client.options(
            "/api/system/health",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
    assert allowed.headers.get("access-control-allow-origin") == "https://dashboard.example"
    assert "access-control-allow-origin" not in denied.headers


def test_remote_api_surface_is_read_only_and_payloads_are_safe(remote_database: Database) -> None:
    app = create_app(
        settings=Settings(cors_origins=("https://dashboard.example",)),
        database=remote_database,
    )
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        for method in route.methods:
            if method == "POST":
                assert is_private_control_path_allowed(route.path, method)
            else:
                assert method in {"GET", "HEAD", "OPTIONS"}

    forbidden = {
        "telegram_bot_token", "telegram_chat_id", "mt5_password", "api_key", "database_url"
    }
    with TestClient(app) as client:
        payloads = [
            client.get("/api/health").json(),
            client.get("/api/system/health").json(),
            client.get("/api/config/public").json(),
            client.get("/api/supervisor/status").json(),
        ]
    for payload in payloads:
        assert not forbidden.intersection(payload)
        assert "127.0.0.1" not in str(payload)


def test_system_health_exposes_supervisor_from_fresh_registry(
    remote_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_dir = tmp_path / "data"
    registry_dir.mkdir()
    now = datetime.now(UTC).isoformat()
    (registry_dir / "process_registry.json").write_text(
        json.dumps({
            "api": {"state": "RUNNING", "last_heartbeat": now},
            "live": {"state": "RUNNING", "last_heartbeat": now},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr("api.app.PROJECT_ROOT", tmp_path)
    app = create_app(settings=Settings(), database=remote_database)
    with TestClient(app) as client:
        assert client.get("/api/system/health").json()["services"]["supervisor"] == "DEGRADED"
