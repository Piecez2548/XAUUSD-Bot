/* @vitest-environment jsdom */

import { act, render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const healthPayload: unknown = { checked_at: new Date().toISOString(), database: "CONNECTED", services: {} };
vi.mock("../hooks/useApi", () => ({
  useApi: () => ({ data: healthPayload, loading: false, error: null, refresh: vi.fn() }),
}));

import { AppShell } from "./AppShell";

describe("AppShell render containment", () => {
  afterEach(() => vi.useRealTimers());

  it("does not rerender the Outlet when only the Bangkok clock ticks", () => {
    vi.useFakeTimers();
    let outletRenders = 0;
    function OutletProbe() {
      outletRenders += 1;
      return <div>outlet</div>;
    }
    render(<MemoryRouter initialEntries={["/"]}><Routes><Route element={<AppShell />}><Route path="/" element={<OutletProbe />} /></Route></Routes></MemoryRouter>);
    expect(outletRenders).toBe(1);
    act(() => vi.advanceTimersByTime(5_000));
    expect(outletRenders).toBe(1);
  });
});
