/* @vitest-environment jsdom */

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

let healthPayload: unknown = null;
vi.mock("../hooks/useApi", () => ({
  useApi: () => ({ data: healthPayload, loading: false, error: null, refresh: vi.fn() }),
}));

import { AppShell } from "./AppShell";

beforeEach(() => { healthPayload = null; });

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
