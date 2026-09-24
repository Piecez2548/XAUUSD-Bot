from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import api.app as app_module
from config.settings import Settings
from persistence.database import Database
from services.authentication import AuthenticationService


def _client(tmp_path: Path, *, private: bool = False) -> tuple[TestClient, Database]:
    (tmp_path / "frontend" / "dist" / "assets").mkdir(parents=True)
    (tmp_path / "frontend" / "dist" / "index.html").write_text(
        "<!doctype html><html><body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "dist" / "assets" / "app.js").write_text(
        "console.log('test');", encoding="utf-8"
    )
    database_factory = Database.for_test if private else Database
    database = database_factory(f"sqlite:///{(tmp_path / 'dashboard.db').as_posix()}")
    if private:
        database.create_test_schema()
    else:
        database.create_schema()
    original_root = app_module.PROJECT_ROOT
    app_module.PROJECT_ROOT = tmp_path
    app = app_module.create_app(
        settings=Settings(
            remote_dashboard_mode=private,
            csrf_trusted_origins=("https://dashboard.tailnet.test",) if private else (),
        ),
        database=database,
    )
    client = TestClient(app, base_url="https://dashboard.tailnet.test")
    client._dashboard_original_root = original_root
    return client, database


def _close(client: TestClient, database: Database, original_root: Path) -> None:
    client.close()
    database.dispose()
    app_module.PROJECT_ROOT = original_root


def test_static_root_routes_assets_and_api_precedence(tmp_path: Path) -> None:
    client, database = _client(tmp_path)
    original_root = client._dashboard_original_root
    try:
        for path in "/", "/research", "/forward", "/forward-validation":
            response = client.get(path)
            assert response.status_code == 200
            assert "id='root'" in response.text

        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.headers["content-type"].startswith("application/javascript")

        api = client.get("/api/health")
        assert api.status_code == 200
        assert api.headers["content-type"].startswith("application/json")

        missing_api = client.get("/api/does-not-exist")
        assert missing_api.status_code == 404
        assert missing_api.json() == {"detail": "Not Found"}
    finally:
        _close(client, database, original_root)


def test_missing_asset_is_not_spa_fallback(tmp_path: Path) -> None:
    client, database = _client(tmp_path)
    original_root = client._dashboard_original_root
    try:
        response = client.get("/assets/missing.js")
        assert response.status_code == 404
        assert "id='root'" not in response.text
    finally:
        _close(client, database, original_root)


def test_missing_dist_fails_gracefully_without_breaking_api(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'missing.db').as_posix()}")
    database.create_schema()
    original_root = app_module.PROJECT_ROOT
    app_module.PROJECT_ROOT = tmp_path
    client = TestClient(
        app_module.create_app(settings=Settings(remote_dashboard_mode=False), database=database)
    )
    try:
        assert client.get("/").status_code == 404
        assert client.get("/api/health").status_code == 200
    finally:
        _close(client, database, original_root)


def test_private_dashboard_auth_boundary_covers_static_and_api(
    tmp_path: Path,
) -> None:
    client, database = _client(tmp_path, private=True)
    original_root = client._dashboard_original_root
    try:
        assert client.get("/").status_code == 401
        assert client.get("/api/health").status_code == 401

        headers = {
            "Tailscale-User-Login": "operator@example.com",
            "Origin": "https://dashboard.tailnet.test",
        }
        assert client.get("/", headers=headers).status_code == 200
        assert client.get("/api/system/health", headers=headers).status_code == 401
        AuthenticationService(database, Settings()).create_user_for_admin(
            login="operator@example.com",
            password="correct horse battery staple 123!",
            role="ADMIN",
            state="ACTIVE",
            bound_tailscale_login="operator@example.com",
        )
        assert client.post(
            "/api/auth/login",
            headers=headers,
            json={"login": "operator@example.com", "password": "correct horse battery staple 123!"},
        ).status_code == 200
        assert client.get("/api/system/health", headers=headers).status_code == 200
        assert client.get("/api/health", headers=headers).status_code == 404
    finally:
        _close(client, database, original_root)
