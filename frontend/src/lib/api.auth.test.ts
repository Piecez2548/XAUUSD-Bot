/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from "vitest";

import { getAuthSession, logoutRequest } from "./api";

describe("browser authentication API contract", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("rejects malformed session JSON instead of trusting a truthy flag", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ authenticated: "false" }), { status: 200 }),
    ));
    await expect(getAuthSession()).rejects.toThrow("Invalid session response");
  });

  it("accepts an unauthenticated session envelope", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ authenticated: false }), { status: 200 }),
    ));
    await expect(getAuthSession()).resolves.toEqual({ authenticated: false });
  });

  it("does not announce logout success when the server rejects revocation", async () => {
    const dispatch = vi.spyOn(window, "dispatchEvent");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response("unavailable", { status: 503 }),
    ));
    await expect(logoutRequest()).rejects.toThrow("session state is unknown");
    expect(dispatch).not.toHaveBeenCalledWith(expect.objectContaining({ type: "xauusd:auth-required" }));
  });

  it("announces logout only after a successful server response", async () => {
    const dispatch = vi.spyOn(window, "dispatchEvent");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ authenticated: false }), { status: 200 }),
    ));
    await expect(logoutRequest()).resolves.toBeUndefined();
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: "xauusd:auth-required" }));
  });
});
