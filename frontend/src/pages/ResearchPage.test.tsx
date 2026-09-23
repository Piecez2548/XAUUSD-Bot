/* @vitest-environment jsdom */

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const responses = new Map<string, unknown>();

vi.mock("../hooks/useApi", () => ({
  useApi: (path: string) => ({ data: responses.get(path) ?? null, loading: false, error: null, refresh: vi.fn() }),
}));

import { ResearchPage } from "./ResearchPage";

const completeRun = {
  run_id: "run-1", run_name: "rr-2", strategy_id: "pair_zone_v1", strategy_version: "1.0.0",
  status: "COMPLETED", dataset_id: "dataset-1", dataset_hash: "d".repeat(64), parameters: { rr: 2 },
  summary: { buy: 2, sell: 1, no_trade: 7, trades: 3, total_r: 1.2, expectancy: 0.4, profit_factor: 1.3 }, execution_allowed: false,
};

beforeEach(() => responses.clear());

describe("ResearchPage partial API resilience", () => {
  it("renders complete persisted evidence", () => {
    responses.set("/api/research/runs?limit=200", [completeRun]);
    responses.set("/api/research/compare?strategy_id=pair_zone_v1&rr=2.0", [completeRun]);
    responses.set("/api/research/compare?strategy_id=pair_zone_v1&strategy_id=trend_pullback_v1&rr=2.0", [completeRun]);
    responses.set("/api/research/datasets", [{ dataset_id: "dataset-1", symbol: "XAUUSD", timeframe: "M5", start_at: "2026-01-01T00:00:00Z", end_at: "2026-01-02T00:00:00Z", row_count: 10 }]);
    responses.set("/api/research/strategies", { active_strategy: "pair_zone_v1", activation: { strategy_version: "1.0.0", config_hash: "a".repeat(64) } });
    responses.set("/api/research/robustness?strategy_id=pair_zone_v1&limit=1", []);
    responses.set("/api/research/runs/run-1/curve?display_limit=600", { equity: [{ value: 1 }], drawdown: [{ value: 0 }] });
    render(<ResearchPage />);
    expect(screen.getByText("Strategy Lab")).toBeTruthy();
    expect(screen.getAllByText("pair_zone_v1").length).toBeGreaterThan(0);
  });

  it.each(["undefined optional arrays", "null optional data", "empty datasets and runs", "legacy incomplete run"])("renders safely with %s", () => {
    responses.set("/api/research/runs?limit=200", [{ run_id: "legacy", strategy_id: "pair_zone_v1", status: "COMPLETED" }]);
    responses.set("/api/research/compare?strategy_id=pair_zone_v1&rr=2.0", [{ run_id: "legacy", strategy_id: "pair_zone_v1", status: "COMPLETED" }]);
    responses.set("/api/research/compare?strategy_id=pair_zone_v1&strategy_id=trend_pullback_v1&rr=2.0", null);
    responses.set("/api/research/datasets", undefined);
    responses.set("/api/research/strategies", { active_strategy: "pair_zone_v1", activation: null });
    responses.set("/api/research/robustness?strategy_id=pair_zone_v1&limit=1", null);
    responses.set("/api/research/runs/legacy/curve?display_limit=600", { equity: undefined, drawdown: null });
    expect(() => render(<ResearchPage />)).not.toThrow();
    expect(screen.getAllByText("ข้อมูลนี้ไม่มีใน Research Run นี้").length).toBeGreaterThan(0);
  });
});
