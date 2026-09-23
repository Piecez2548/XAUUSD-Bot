import { describe, expect, it, vi } from "vitest";

import { apiUrl, isApiConfigured, requestJson } from "./api";

describe("dashboard API deployment context", () => {
  it("uses same-origin relative polling for the private production dashboard", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ status: "healthy" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const result = await requestJson<{ status: string }>(
      "/api/system/health",
      { apiBase: "", privateDashboard: true, production: true },
      undefined,
      fetcher,
    );

    expect(result).toEqual({ status: "healthy" });
    expect(fetcher).toHaveBeenCalledWith("/api/system/health", {
      credentials: "same-origin",
      signal: undefined,
    });
  });

  it("keeps an unconfigured public production build offline-safe", async () => {
    const fetcher = vi.fn<typeof fetch>();

    await expect(
      requestJson(
        "/api/system/health",
        { apiBase: "", privateDashboard: false, production: true },
        undefined,
        fetcher,
      ),
    ).rejects.toThrow("BACKEND_NOT_CONFIGURED");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("never invents a localhost fallback for same-origin production", () => {
    expect(apiUrl("/api/system/health", "")).toBe("/api/system/health");
    expect(apiUrl("/api/system/health", "")).not.toContain("127.0.0.1");
    expect(apiUrl("/api/system/health", "")).not.toContain("localhost");
  });

  it("requires either an explicit API base or the private deployment marker", () => {
    expect(isApiConfigured({ apiBase: "", privateDashboard: true, production: true })).toBe(true);
    expect(isApiConfigured({ apiBase: "", privateDashboard: false, production: true })).toBe(false);
    expect(isApiConfigured({ apiBase: "https://example.invalid", privateDashboard: false, production: true })).toBe(true);
  });
});
