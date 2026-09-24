/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OperatorControlPage } from "./OperatorControlPage";

const hookState = vi.hoisted(() => ({
  data: null as unknown,
  loading: false,
  error: null as string | null,
  refresh: vi.fn(),
}));
const postJson = vi.hoisted(() => vi.fn());

vi.mock("../hooks/useApi", () => ({
  useApi: () => hookState,
}));

const status = {
      checked_at: "2026-09-24T00:00:00Z",
      control: "CONNECTED",
      supervisor: "CONNECTED",
      api: "RUNNING",
      live: "STOPPED",
      mt5: "CONNECTED",
      database: "CONNECTED",
      telegram: "CONNECTED",
      forward_shadow: "CONNECTED",
      execution: {
        demo_execution_enabled: true,
        demo_kill_switch_armed: true,
        real_money_execution: "DISABLED",
      },
      strategy: {
        pair_zone_state: "UNKNOWN",
        current_direction: "UNKNOWN",
        latest_canonical_signal_id: null,
        latest_canonical_direction: null,
        latest_canonical_signal_at: null,
        latest_forward_session_id: "forward-test",
      },
};

vi.mock("../lib/api", () => ({
  postJson: (...args: unknown[]) => postJson(...args),
}));

describe("OperatorControlPage", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    hookState.data = { ok: true, duplicate: false, operation_id: "status", command: "status", message: "ok", status };
    hookState.loading = false;
    hookState.error = null;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    postJson.mockResolvedValue({
      ok: true,
      duplicate: false,
      operation_id: "00000000-0000-4000-8000-000000000001",
      command: "stop",
      message: "accepted",
      status: {},
    });
  });

  it("renders verified system state and permanently disables real-money execution", () => {
    render(<OperatorControlPage />);
    expect(screen.getByText("Web Control Plane")).toBeTruthy();
    expect(screen.getAllByText("STOPPED").length).toBeGreaterThan(0);
    expect(screen.getByText("REAL MONEY EXECUTION: DISABLED")).toBeTruthy();
    expect(screen.queryByText(/ENABLE LIVE|LIVE MONEY/i)).toBeNull();
    expect(screen.getByText("forward-test")).toBeTruthy();
  });

  it("renders a verified healthy no-active-zone observation explicitly", () => {
    hookState.data = {
      ok: true,
      status: {
        ...status,
        strategy: {
          ...status.strategy,
          pair_zone_state: "HEALTHY_NO_ACTIVE_ZONE",
          current_direction: "NONE",
          pair_zone_reason: "NO_VALID_ZONE",
          pair_zone_evaluated_m15_timestamp: "2026-09-24T00:00:00Z",
        },
      },
    };
    render(<OperatorControlPage />);
    expect(screen.getByText("HEALTHY_NO_ACTIVE_ZONE")).toBeTruthy();
    expect(screen.getByText("NO_VALID_ZONE")).toBeTruthy();
    expect(screen.getByText("Evaluated M15 candle")).toBeTruthy();
  });

  it("renders an active Pair Zone direction without substituting unknown data", () => {
    hookState.data = {
      ok: true,
      status: {
        ...status,
        strategy: {
          ...status.strategy,
          pair_zone_state: "ACTIVE_ZONE",
          current_direction: "BUY",
          pair_zone_reason: "ZONE_CONFIRMATION_MISSING",
          pair_zone_id: "pz-test",
          pair_zone_lower: 2300,
          pair_zone_upper: 2301,
          pair_zone_evaluated_m15_timestamp: "2026-09-24T00:00:00Z",
        },
      },
    };
    render(<OperatorControlPage />);
    expect(screen.getByText("ACTIVE_ZONE")).toBeTruthy();
    expect(screen.getByText("BUY")).toBeTruthy();
    expect(screen.getByText("ZONE_CONFIRMATION_MISSING")).toBeTruthy();
  });

  it.each([
    ["ACTIVE_ZONE", "ACTIVE_ZONE"],
    ["HEALTHY_NO_ACTIVE_ZONE", "HEALTHY_NO_ACTIVE_ZONE"],
    ["UNKNOWN", "UNKNOWN"],
    ["malformed", "UNKNOWN"],
    [undefined, "UNKNOWN"],
    [null, "UNKNOWN"],
    ["UNRECOGNIZED_FUTURE_STATE", "UNKNOWN"],
  ])("normalizes Pair Zone state %s to the exact fail-closed contract", (rawState, expected) => {
    hookState.data = {
      ok: true,
      status: {
        ...status,
        strategy: { ...status.strategy, pair_zone_state: rawState },
      },
    };
    render(<OperatorControlPage />);
    if (expected === "UNKNOWN") {
      expect(screen.getAllByText("UNKNOWN").length).toBeGreaterThan(0);
    } else {
      expect(screen.getByText(expected)).toBeTruthy();
    }
    if (typeof rawState === "string" && !["ACTIVE_ZONE", "HEALTHY_NO_ACTIVE_ZONE", "UNKNOWN"].includes(rawState)) {
      expect(screen.queryByText(rawState)).toBeNull();
    }
  });

  it("confirms destructive actions and sends only the fixed control route", async () => {
    render(<OperatorControlPage />);
    fireEvent.click(screen.getByRole("button", { name: "STOP SYSTEM" }));
    await waitFor(() => expect(postJson).toHaveBeenCalledTimes(1));
    expect(window.confirm).toHaveBeenCalledTimes(1);
    expect(postJson.mock.calls[0][0]).toBe("/api/control/stop");
    expect(postJson.mock.calls[0][1]).toEqual({ operation_id: expect.any(String) });
    expect(hookState.refresh).toHaveBeenCalledTimes(1);
  });

  it("renders unavailable optional data and disables unsafe controls", () => {
    hookState.data = {
      ok: true,
      status: {
        control: "CONNECTED",
        supervisor: "DEGRADED",
        api: "RUNNING",
        live: "STOPPED",
      },
    };
    render(<OperatorControlPage />);
    expect(screen.getAllByText("UNAVAILABLE").length).toBeGreaterThan(0);
    expect((screen.getByRole("button", { name: "START SYSTEM" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "DISARM DEMO" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("fails safely on a malformed control response", () => {
    hookState.data = { ok: true, status: "malformed" };
    render(<OperatorControlPage />);
    expect(screen.getByText("Data unavailable")).toBeTruthy();
    expect((screen.getByRole("button", { name: "RESTART SYSTEM" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/response was unavailable or malformed/i)).toBeTruthy();
  });

  it("renders degraded Demo state without treating it as disarmed", () => {
    hookState.data = {
      ok: true,
      status: {
        ...status,
        execution: { demo_execution_enabled: true },
      },
    };
    render(<OperatorControlPage />);
    expect(screen.getAllByText("UNAVAILABLE").length).toBeGreaterThan(0);
    expect((screen.getByRole("button", { name: "ARM DEMO" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "DISARM DEMO" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
