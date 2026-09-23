from __future__ import annotations

from api.app import _display_downsample


def test_research_curve_display_downsample_is_bounded_deterministic_and_preserves_extrema() -> None:
    points = [{"timestamp": str(index), "value": 0.0} for index in range(10_000)]
    points[2_345]["value"] = -100.0
    points[8_765]["value"] = 200.0
    first = _display_downsample(points, 600)
    second = _display_downsample(points, 600)
    assert len(first) <= 600
    assert first == second
    assert first[0] == points[0]
    assert first[-1] == points[-1]
    assert points[2_345] in first
    assert points[8_765] in first


def test_research_curve_display_downsample_keeps_small_series_unchanged() -> None:
    points = [{"timestamp": str(index), "value": float(index)} for index in range(3)]
    assert _display_downsample(points, 600) is points


def test_account_curve_display_downsample_uses_equity_extrema() -> None:
    points = [{"timestamp": str(index), "equity": 100.0} for index in range(2_000)]
    points[321]["equity"] = 50.0
    points[1_654]["equity"] = 180.0
    sampled = _display_downsample(points, 120, value_key="equity")
    assert len(sampled) <= 120
    assert sampled[0] == points[0]
    assert sampled[-1] == points[-1]
    assert points[321] in sampled
    assert points[1_654] in sampled
