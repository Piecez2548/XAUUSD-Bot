from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from config.remote_read_only_policy import is_remote_path_allowed
from config.settings import Settings
from models.market import Timeframe
from persistence.database import Database
from persistence.orm import StrategyIntelligenceRecord
from services.authentication import AuthenticationService
from services.forward_shadow import ForwardInput, ForwardShadowWorker

INTELLIGENCE_ROUTES = (
    "/api/intelligence/candidate",
    "/api/intelligence/context",
    "/api/intelligence/evidence",
    "/api/intelligence/alerts",
)
TAILSCALE_HEADERS = {
    "Tailscale-User-Login": "operator@example.com",
    "Origin": "https://dashboard.tailnet.test",
}


def _database(tmp_path) -> Database:
    database = Database.for_test(f"sqlite:///{(tmp_path / 'phase30-boundaries.db').as_posix()}")
    database.create_test_schema()
    return database


def _insert_intelligence(database: Database, *, execution_allowed: bool = False, score: float = 72.5) -> None:
    with database.session() as session:
        session.add(
            StrategyIntelligenceRecord(
                candidate_id="candidate-boundary-1",
                symbol="XAUUSD",
                strategy="pair_zone_v1",
                strategy_version="pair_zone_v1",
                evidence_version="evidence_v1",
                detected_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
                timeframe="M15/M5",
                direction="BUY",
                state="CANDIDATE",
                score=score,
                confidence_band="HIGH",
                alert_decision="OBSERVE",
                blockers_json=[],
                warnings_json=["REVIEW_REQUIRED"],
                context_json={"data_status": "READY"},
                evidence_json=[{"rule_id": "STRUCTURE_ALIGNMENT"}],
                score_components_json=[{"category": "STRUCTURE", "weight": 0.25}],
                source="test",
                execution_allowed=execution_allowed,
            )
        )


@pytest.mark.parametrize("path", INTELLIGENCE_ROUTES)
def test_intelligence_routes_have_truthful_empty_state(tmp_path, path: str) -> None:
    database = _database(tmp_path)
    with TestClient(create_app(settings=Settings(), database=database)) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.json() == ([] if path.endswith("alerts") else None)
    database.dispose()


def test_intelligence_routes_return_deterministic_read_only_schema(tmp_path) -> None:
    database = _database(tmp_path)
    _insert_intelligence(database)
    with TestClient(create_app(settings=Settings(), database=database)) as client:
        candidate = client.get("/api/intelligence/candidate")
        context = client.get("/api/intelligence/context")
        evidence = client.get("/api/intelligence/evidence")
        alerts = client.get("/api/intelligence/alerts")
    assert candidate.status_code == context.status_code == evidence.status_code == alerts.status_code == 200
    candidate_payload = candidate.json()
    assert candidate_payload["candidate_id"] == "candidate-boundary-1"
    assert candidate_payload["execution_allowed"] is False
    assert context.json() == {"data_status": "READY"}
    assert evidence.json()["candidate_id"] == "candidate-boundary-1"
    assert evidence.json()["execution_allowed"] is False
    assert len(alerts.json()) == 1
    assert alerts.json()[0]["execution_allowed"] is False
    database.dispose()


@pytest.mark.parametrize("path", INTELLIGENCE_ROUTES)
def test_private_intelligence_boundary_requires_identity_and_allows_only_get(tmp_path, path: str) -> None:
    database = _database(tmp_path)
    _insert_intelligence(database)
    settings = Settings(
        remote_dashboard_mode=True,
        csrf_trusted_origins=("https://dashboard.tailnet.test",),
    )
    app = create_app(settings=settings, database=database)
    with TestClient(app, base_url="https://dashboard.tailnet.test") as client:
        AuthenticationService(database, settings).create_user_for_admin(
            login="operator@example.com",
            password="correct horse battery staple 123!",
            role="ADMIN",
            state="ACTIVE",
            bound_tailscale_login="operator@example.com",
        )
        assert client.get(path).status_code == 401
        login_response = client.post(
            "/api/auth/login",
            headers=TAILSCALE_HEADERS,
            json={"login": "operator@example.com", "password": "correct horse battery staple 123!"},
        )
        assert login_response.status_code == 200
        assert client.get(path, headers=TAILSCALE_HEADERS).status_code == 200
        assert client.post(path, headers=TAILSCALE_HEADERS).status_code == 404
    assert is_remote_path_allowed(path)
    database.dispose()


def test_private_intelligence_unknown_and_lifecycle_routes_remain_denied(tmp_path) -> None:
    database = _database(tmp_path)
    settings = Settings(
        remote_dashboard_mode=True,
        csrf_trusted_origins=("https://dashboard.tailnet.test",),
    )
    with TestClient(create_app(settings=settings, database=database)) as client:
        assert client.get("/api/intelligence/unknown", headers=TAILSCALE_HEADERS).status_code == 404
        for lifecycle_path in ("/api/start", "/api/stop", "/api/restart"):
            assert client.get(lifecycle_path, headers=TAILSCALE_HEADERS).status_code == 404
            assert client.post(lifecycle_path, headers=TAILSCALE_HEADERS).status_code == 404
    assert not is_remote_path_allowed("/api/intelligence/unknown")
    database.dispose()


@pytest.mark.parametrize("kwargs", [{"execution_allowed": True}, {"score": 101}])
def test_malformed_persisted_intelligence_fails_closed(tmp_path, kwargs) -> None:
    database = _database(tmp_path)
    _insert_intelligence(database, **kwargs)
    with TestClient(create_app(settings=Settings(), database=database)) as client:
        response = client.get("/api/intelligence/candidate")
    assert response.status_code == 503
    assert response.json() == {"detail": "Intelligence state unavailable"}
    database.dispose()


def test_forward_shadow_intelligence_is_additive_and_never_controls_original_decision(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'phase30-forward.db').as_posix()}")
    worker = ForwardShadowWorker(settings, database, logger=logging.getLogger("phase30-boundary"))
    timestamp = datetime.now(UTC) + timedelta(minutes=5)
    worker.session = SimpleNamespace(id="forward-session-1", started_at=timestamp - timedelta(minutes=5))
    original_decision = SimpleNamespace(
        decision=SimpleNamespace(value="WAIT"),
        m5_candle_timestamp=timestamp,
        feature_context={},
    )
    calls: dict[str, object] = {}

    async def no_open_trade_evaluation() -> None:
        return None

    def record_health(*_args, **_kwargs) -> None:
        return None

    class FakeIntelligence:
        def __init__(self, _settings) -> None:
            pass

        def evaluate(self, snapshot, **kwargs):
            calls["snapshot"] = snapshot
            calls["kwargs"] = kwargs
            return SimpleNamespace(candidate=SimpleNamespace(execution_allowed=False))

    def persist(database_arg, result, **kwargs):
        calls["database"] = database_arg
        calls["result"] = result
        calls["persist_kwargs"] = kwargs

    monkeypatch.setattr(worker, "_ensure_session_symbol", lambda _symbol: None)
    monkeypatch.setattr(worker.strategy, "evaluate", lambda *args, **kwargs: original_decision)
    monkeypatch.setattr(worker, "_evaluate_open_trades", no_open_trade_evaluation)
    monkeypatch.setattr(worker, "_record_health", record_health)
    monkeypatch.setattr("services.intelligence.StrategyIntelligenceEngine", FakeIntelligence)
    monkeypatch.setattr("services.intelligence.persist_intelligence_record", persist)

    snapshot = SimpleNamespace(
        symbol=SimpleNamespace(name="XAUUSD"),
        candles={Timeframe.M5: (SimpleNamespace(timestamp=timestamp),)},
    )
    asyncio.run(worker._process(ForwardInput(snapshot=snapshot, market_snapshot_id="market-1", risk=None)))

    assert calls["database"] is database
    assert calls["persist_kwargs"] == {"forward_session_id": "forward-session-1"}
    assert original_decision.decision.value == "WAIT"
    assert worker._last_signal is None
    database.dispose()


def test_forward_shadow_preserves_canonical_cycle_when_intelligence_is_unavailable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'phase30-forward-failure.db').as_posix()}")
    worker = ForwardShadowWorker(settings, database, logger=logging.getLogger("phase30-boundary"))
    timestamp = datetime.now(UTC) + timedelta(minutes=5)
    worker.session = SimpleNamespace(id="forward-session-2", started_at=timestamp - timedelta(minutes=5))
    original_decision = SimpleNamespace(
        decision=SimpleNamespace(value="WAIT"),
        m5_candle_timestamp=timestamp,
        feature_context={},
    )
    health_states: list[tuple[str, str, str | None]] = []

    async def no_open_trade_evaluation() -> None:
        return None

    class BrokenIntelligence:
        def __init__(self, _settings) -> None:
            pass

        def evaluate(self, *_args, **_kwargs):
            raise ValueError("invalid intelligence state")

    def record_health(state, message, **kwargs) -> None:
        health_states.append((state, message, kwargs.get("error_category")))

    monkeypatch.setattr(worker, "_ensure_session_symbol", lambda _symbol: None)
    monkeypatch.setattr(worker.strategy, "evaluate", lambda *args, **kwargs: original_decision)
    monkeypatch.setattr(worker, "_evaluate_open_trades", no_open_trade_evaluation)
    monkeypatch.setattr(worker, "_record_health", record_health)
    monkeypatch.setattr("services.intelligence.StrategyIntelligenceEngine", BrokenIntelligence)

    snapshot = SimpleNamespace(
        symbol=SimpleNamespace(name="XAUUSD"),
        candles={Timeframe.M5: (SimpleNamespace(timestamp=timestamp),)},
    )
    asyncio.run(worker._process(ForwardInput(snapshot=snapshot, market_snapshot_id="market-2", risk=None)))

    assert original_decision.decision.value == "WAIT"
    assert worker._last_success_at is not None
    assert health_states[-1] == (
        "DEGRADED",
        "Forward shadow cycle completed; strategy intelligence unavailable",
        "INTELLIGENCE_UNAVAILABLE",
    )
    database.dispose()
