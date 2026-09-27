/* @vitest-environment jsdom */

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let healthPayload: unknown = null;
vi.mock("../hooks/useApi", () => ({
  useApi: () => ({ data: healthPayload, loading: false, error: null, refresh: vi.fn() }),
}));

import { AppShell } from "./AppShell";

beforeEach(() => { healthPayload = null; });
afterEach(() => { cleanup(); });

describe("authoritative global shadow status", () => {
  it("renders CONNECTED from shadow_worker, not a shadow_engine alias", () => {
    healthPayload = { checked_at: new Date().toISOString(), database: "CONNECTED", services: { shadow_worker: "CONNECTED", shadow_outcome_worker: "CONNECTED" } };
    render(<MemoryRouter><AppShell /></MemoryRouter>);
    expect(screen.getByTitle("SHADOW: CONNECTED")).toBeTruthy();
    expect(screen.getByText("BKK · UTC+7")).toBeTruthy();
  });

  it("keeps unavailable shadow health UNKNOWN", () => {
    healthPayload = { checked_at: new Date().toISOString(), database: "CONNECTED", services: {} };
    render(<MemoryRouter><AppShell /></MemoryRouter>);
    expect(screen.getByTitle("SHADOW: UNKNOWN")).toBeTruthy();
  });
});

describe("sidebar active route ownership", () => {
  it.each([
    ["/", "Overview"],
    ["/trades", "Trades"],
    ["/trades/trade-1", "Trades"],
    ["/decisions", "AI Decisions"],
    ["/shadow", "Shadow Trading"],
    ["/research", "Strategy Research"],
    ["/research/roadmap", "Research Roadmap"],
    ["/research/roadmap/run-1", "Research Roadmap"],
    ["/forward", "Forward Validation"],
    ["/forward-validation", "Forward Validation"],
    ["/performance", "Performance"],
    ["/risk", "Risk"],
    ["/market", "Market"],
    ["/news", "News"],
    ["/health", "System Health"],
    ["/logs", "Logs"],
    ["/settings", "Settings"],
    ["/control", "Operator Control"],
  ])("marks only the owning item active for %s", (path, activeLabel) => {
    render(<MemoryRouter initialEntries={[path]}><AppShell /></MemoryRouter>);

    const links = screen.getAllByRole("link");
    const activeLinks = links.filter((link) => link.classList.contains("active"));
    expect(activeLinks).toHaveLength(1);
    expect(activeLinks[0].textContent).toContain(activeLabel);
    expect(activeLinks[0].getAttribute("aria-current")).toBe("page");

    if (path.startsWith("/research/roadmap")) {
      expect(screen.getByRole("link", { name: "Strategy Research" }).classList.contains("active")).toBe(false);
      expect(screen.getByRole("link", { name: "Research Roadmap" }).classList.contains("active")).toBe(true);
    }
  });
});
