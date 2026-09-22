from datetime import UTC, datetime, timedelta

from services.research_robustness import (
    COST_SCENARIOS,
    CostScenario,
    TradeSample,
    _walk_forward,
    adverse_slippage_prices,
    concentration_metrics,
    metrics,
    monte_carlo,
    monthly_metrics,
    parameter_variant_config,
    side_metrics,
)


def sample_set() -> list[TradeSample]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        TradeSample(start, "BUY", 100.0, 99.0, 1.0, "TP_HIT"),
        TradeSample(start + timedelta(days=1), "SELL", 100.0, 101.0, -1.0, "SL_HIT"),
        TradeSample(start + timedelta(days=35), "BUY", 100.0, 99.0, 0.5, "EXPIRED"),
        TradeSample(start + timedelta(days=36), "SELL", 100.0, 101.0, None, "AMBIGUOUS"),
    ]


def test_cost_accounting_and_gross_net_separation() -> None:
    result = metrics(sample_set(), COST_SCENARIOS[0])
    assert result["trades"] == 4
    assert result["gross_total_r"] == 0.5
    assert result["net_total_r"] == 0.5
    normal = metrics(sample_set(), COST_SCENARIOS[1])
    assert normal["gross_total_r"] == result["gross_total_r"]
    assert normal["net_total_r"] < normal["gross_total_r"]
    assert normal["cost_total_r"] > 0


def test_slippage_direction_and_commission_are_conservative() -> None:
    scenario = CostScenario("test", 0, 1, 1, 0.1)
    assert adverse_slippage_prices("BUY", 100, 101, scenario) == (100.01, 100.99)
    assert adverse_slippage_prices("SELL", 100, 99, scenario) == (99.99, 99.01)
    assert metrics(sample_set(), scenario)["cost_total_r"] > 0.2


def test_monthly_and_side_segments_are_deterministic() -> None:
    samples = sample_set()
    monthly = monthly_metrics(samples, COST_SCENARIOS[0])
    assert list(monthly) == ["2026-01", "2026-02"]
    assert monthly["2026-01"]["trades"] == 2
    sides = side_metrics(samples, COST_SCENARIOS[0])
    assert sides["BUY"]["trades"] == 2
    assert sides["SELL"]["trades"] == 2


def test_concentration_and_walk_forward_boundaries() -> None:
    samples = sample_set()
    concentration = concentration_metrics(samples, COST_SCENARIOS[0])
    assert concentration["top_1_net_r"] == 1.0
    assert concentration["remove_top_1_total_net_r"] == -0.5
    windows = _walk_forward(samples, COST_SCENARIOS[0], samples[0].timestamp, samples[-1].timestamp)
    assert windows[0]["start"] == samples[0].timestamp.isoformat()
    assert windows[0]["end"] < windows[1]["start"]


def test_monte_carlo_reproducibility_includes_seed_and_count() -> None:
    first = monte_carlo(sample_set(), COST_SCENARIOS[0], seed=42, simulations=20)
    second = monte_carlo(sample_set(), COST_SCENARIOS[0], seed=42, simulations=20)
    assert first == second
    assert first["seed"] == 42
    assert first["simulations"] == 20


def test_parameter_perturbation_isolation_does_not_mutate_base() -> None:
    base = {"zone_max_age_minutes": 360, "stop_buffer_points": 0.5}
    variant = parameter_variant_config(base, "zone_max_age_minutes", -0.10)
    assert variant["zone_max_age_minutes"] == 324
    assert base == {"zone_max_age_minutes": 360, "stop_buffer_points": 0.5}
