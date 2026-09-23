from __future__ import annotations

import pytest

from config.remote_read_only_policy import (
    REMOTE_FRONTEND_ORIGIN,
    authorize_remote_request,
    is_remote_path_allowed,
)


@pytest.mark.parametrize(
    "path",
    [
        "/api/system/health",
        "/api/live/status",
        "/api/forward/health?limit=50",
        "/api/research/runs/run-123/curve?display_limit=600",
        "/api/trades/trade-123/events",
        "/api/trades/trade-123",
    ],
)
def test_dashboard_observability_routes_are_allowlisted(path: str) -> None:
    decision = authorize_remote_request(
        method="GET", raw_target=path, authenticated=True, origin=REMOTE_FRONTEND_ORIGIN
    )
    assert decision.allowed
    assert decision.reason == "authenticated_read_only_route"
    assert is_remote_path_allowed(path)


@pytest.mark.parametrize("path", ["/start", "/stop", "/restart", "/api/start", "/api/stop"])
def test_lifecycle_routes_are_denied(path: str) -> None:
    decision = authorize_remote_request(
        method="GET", raw_target=path, authenticated=True, origin=REMOTE_FRONTEND_ORIGIN
    )
    assert not decision.allowed
    assert decision.reason == "path_not_allowlisted"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_mutating_and_unneeded_methods_are_denied(method: str) -> None:
    decision = authorize_remote_request(
        method=method,
        raw_target="/api/system/health",
        authenticated=True,
        origin=REMOTE_FRONTEND_ORIGIN,
    )
    assert not decision.allowed
    assert decision.reason == "method_not_allowed"


@pytest.mark.parametrize(
    "path",
    [
        "/api/unknown",
        "/api/history/deals",
        "/api/research/runs/run-123/metrics",
        "/api/shadow/performance/breakdown",
        "/api/export/trade-events.csv",
        "/api/system/health/extra",
    ],
)
def test_unknown_and_unneeded_routes_are_denied(path: str) -> None:
    assert not is_remote_path_allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        "/api/system/../config/public",
        "/api/system/%2e%2e/config/public",
        "/api/system/%2fhealth",
        "/api/system/%68ealth",
        "/api//system/health",
        "/api/system/health/./",
        "/api/system\\health",
        "//127.0.0.1:8000/api/system/health",
        "https://127.0.0.1:8000/api/system/health",
    ],
)
def test_traversal_encoded_and_absolute_target_bypasses_fail_closed(path: str) -> None:
    assert not is_remote_path_allowed(path)


def test_query_string_cannot_change_route_authorization() -> None:
    allowed = authorize_remote_request(
        method="GET",
        raw_target="/api/system/health?path=/start&upstream=https://example.invalid",
        authenticated=True,
        origin=REMOTE_FRONTEND_ORIGIN,
    )
    denied = authorize_remote_request(
        method="GET",
        raw_target="/api/unknown?path=/api/system/health",
        authenticated=True,
        origin=REMOTE_FRONTEND_ORIGIN,
    )
    assert allowed.allowed
    assert denied.reason == "path_not_allowlisted"


def test_broker_write_and_arbitrary_upstream_targets_cannot_pass() -> None:
    for path in (
        "/api/order_send",
        "/api/positions/modify",
        "/api/proxy?url=https://broker.example/order",
        "/proxy/https://127.0.0.1:8000/api/system/health",
    ):
        assert not is_remote_path_allowed(path)


def test_authentication_failure_fails_closed() -> None:
    decision = authorize_remote_request(
        method="GET",
        raw_target="/api/system/health",
        authenticated=False,
        origin=REMOTE_FRONTEND_ORIGIN,
    )
    assert not decision.allowed
    assert decision.reason == "authentication_required"


def test_cors_preflight_is_exact_origin_and_never_upstream_data() -> None:
    allowed = authorize_remote_request(
        method="OPTIONS",
        raw_target="/api/system/health",
        authenticated=False,
        origin=REMOTE_FRONTEND_ORIGIN,
    )
    denied = authorize_remote_request(
        method="OPTIONS",
        raw_target="/api/system/health",
        authenticated=False,
        origin="https://evil.example",
    )
    assert allowed.allowed
    assert allowed.reason == "cors_preflight_no_upstream"
    assert denied.reason == "origin_not_allowed"
