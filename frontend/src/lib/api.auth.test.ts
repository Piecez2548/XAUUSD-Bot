/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from "vitest";

import { getAuthSession, logoutRequest } from "./api";

describe("browser authentication API contract", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.resetModules();
  });

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

  it("fetches CSRF after login and attaches it to logout without persistent storage", async () => {
    vi.stubEnv("VITE_PRIVATE_DASHBOARD", "true");
    vi.resetModules();
    const csrf = "A".repeat(86);
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response("{}", { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: csrf }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ authenticated: false }), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const { loginRequest, logoutRequest } = await import("./api");

    await loginRequest("operator", "passphrase");
    await logoutRequest();

    expect(fetcher).toHaveBeenNthCalledWith(1, "/api/auth/login", expect.objectContaining({
      method: "POST",
      headers: expect.not.objectContaining({ "X-CSRF-Token": expect.anything() }),
    }));
    expect(fetcher).toHaveBeenNthCalledWith(2, "/api/auth/csrf", {
      credentials: "same-origin",
    });
    expect(fetcher).toHaveBeenNthCalledWith(3, "/api/auth/logout", expect.objectContaining({
      headers: expect.objectContaining({ "X-CSRF-Token": csrf }),
    }));
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("obtains in-memory CSRF authority before a private mutation and does not retry rejection", async () => {
    vi.stubEnv("VITE_PRIVATE_DASHBOARD", "true");
    vi.resetModules();
    const csrf = "B".repeat(86);
    const rejected = new Response(JSON.stringify({ code: "CSRF_REJECTED" }), { status: 403 });
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: csrf }), { status: 200 }))
      .mockResolvedValueOnce(rejected);
    vi.stubGlobal("fetch", fetcher);
    const dispatch = vi.spyOn(window, "dispatchEvent");
    const { postJson } = await import("./api");

    await expect(postJson("/api/control/start", { operation_id: "op" })).rejects.toThrow("CSRF_REJECTED");

    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenNthCalledWith(2, "/api/control/start", expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ "X-CSRF-Token": csrf }),
    }));
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: "xauusd:csrf-rejected" }));
  });

  it("keeps GET requests free of mutation-token acquisition", async () => {
    vi.stubEnv("VITE_PRIVATE_DASHBOARD", "true");
    vi.resetModules();
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ status: "healthy" }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetcher);
    const { requestJson } = await import("./api");

    await requestJson("/api/system/health", {
      apiBase: "", privateDashboard: true, production: true,
    });

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith("/api/system/health", {
      credentials: "same-origin", signal: undefined,
    });
  });
});
