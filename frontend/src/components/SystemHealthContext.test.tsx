/* @vitest-environment jsdom */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

let payload: unknown;
let healthSubscriptions = 0;
vi.mock("../hooks/useApi", () => ({
  useApi: () => {
    healthSubscriptions += 1;
    return { data: payload, loading: false, error: null, refresh: vi.fn() };
  },
}));

import { SystemHealthProvider } from "./SystemHealthProvider";
import { useSystemHealth } from "./useSystemHealth";

function SnapshotConsumer({ label }: { label: string }) {
  const health = useSystemHealth();
  return <output data-testid={label}>{health.data?.services.shadow_worker ?? "UNKNOWN"}</output>;
}

describe("single authoritative system health snapshot", () => {
  it("shares one health payload across consumers", () => {
    payload = { services: { shadow_worker: "CONNECTED" } };
    render(<SystemHealthProvider><SnapshotConsumer label="header" /><SnapshotConsumer label="overview" /></SystemHealthProvider>);
    expect(healthSubscriptions).toBe(1);
    expect(screen.getByTestId("header").textContent).toBe("CONNECTED");
    expect(screen.getByTestId("overview").textContent).toBe("CONNECTED");
  });
});
