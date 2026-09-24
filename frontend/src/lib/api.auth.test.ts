/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from "vitest";

import { getAuthSession, logoutRequest } from "./api";

const authenticatedPayload = {
  authenticated: true,
  user: { id: "owner-1", login: "owner@example.test", role: "OWNER", state: "ACTIVE" },
  session: {
    idle_expires_at: "2026-09-24T12:00:00Z",
    absolute_expires_at: "2026-09-25T00:00:00Z",
  },
};

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
      .mockResolvedValueOnce(new Response(JSON.stringify(authenticatedPayload), { status: 200 }))
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

  it("maps invalid credentials to the generic authentication failure", async () => {
    vi.stubEnv("VITE_PRIVATE_DASHBOARD", "true");
    vi.resetModules();
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ detail: "account-specific detail" }), { status: 401 }),
    ));
    const { loginRequest: request } = await import("./api");
    await expect(request("operator", "secret")).rejects.toMatchObject({
      name: "LoginRequestError",
      kind: "invalid_credentials",
    });
  });

  it("maps network failures to an unavailable service without exposing details", async () => {
    vi.stubEnv("VITE_PRIVATE_DASHBOARD", "true");
    vi.resetModules();
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockRejectedValue(
      new TypeError("sensitive transport diagnostic"),
    ));
    const { loginRequest: request } = await import("./api");
    const failure = await request("operator", "secret").catch((error: unknown) => error);
    expect(failure).toMatchObject({ kind: "unavailable" });
    expect(failure).not.toHaveProperty("message", "sensitive transport diagnostic");
  });

  it("rejects malformed successful login response envelopes", async () => {
    vi.stubEnv("VITE_PRIVATE_DASHBOARD", "true");
    vi.resetModules();
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(
      new Response("not-json", { status: 200 }),
    ));
    const { loginRequest: request } = await import("./api");
    await expect(request("operator", "secret")).rejects.toMatchObject({
      kind: "malformed_response",
    });
  });

  it("rejects structurally invalid login payloads before requesting CSRF authority", async () => {
    vi.stubEnv("VITE_PRIVATE_DASHBOARD", "true");
    vi.resetModules();
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ authenticated: true }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetcher);
    const { loginRequest: request } = await import("./api");
    await expect(request("operator", "secret")).rejects.toMatchObject({
      kind: "malformed_response",
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
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

  it("uses the centralized CSRF path for DELETE and does not retry rejection", async () => {
    vi.stubEnv("VITE_PRIVATE_DASHBOARD", "true");
    vi.resetModules();
    const csrf = "C".repeat(86);
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: csrf }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ code: "CSRF_REJECTED" }), { status: 403 }));
    vi.stubGlobal("fetch", fetcher);
    const { deleteJson } = await import("./api");

    await expect(deleteJson("/api/auth/admin/accounts/admin-1/sessions")).rejects.toThrow("CSRF_REJECTED");

    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenNthCalledWith(2,
      "/api/auth/admin/accounts/admin-1/sessions",
      expect.objectContaining({
        method: "DELETE",
        headers: expect.objectContaining({ "X-CSRF-Token": csrf }),
      }),
    );
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
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
