/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OperatorControlPage } from "./OperatorControlPage";

const refresh = vi.fn();
const postJson = vi.fn();

vi.mock("../hooks/useApi", () => ({
  useApi: () => ({
    data: {
      checked_at: "2026-09-24T00:00:00Z",
      control: "CONNECTED",
      supervisor: "CONNECTED",
      api: "RUNNING",
      live: "RUNNING",
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
    },
    loading: false,
    error: null,
    refresh,
  }),
}));

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
    expect(screen.getByText("REAL MONEY EXECUTION: DISABLED")).toBeTruthy();
    expect(screen.queryByText(/ENABLE LIVE|LIVE MONEY/i)).toBeNull();
    expect(screen.getByText("forward-test")).toBeTruthy();
  });

  it("confirms destructive actions and sends only the fixed control route", async () => {
    render(<OperatorControlPage />);
    fireEvent.click(screen.getByRole("button", { name: "STOP SYSTEM" }));
    await waitFor(() => expect(postJson).toHaveBeenCalledTimes(1));
    expect(window.confirm).toHaveBeenCalledTimes(1);
    expect(postJson.mock.calls[0][0]).toBe("/api/control/stop");
    expect(postJson.mock.calls[0][1]).toEqual({ operation_id: expect.any(String) });
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});
