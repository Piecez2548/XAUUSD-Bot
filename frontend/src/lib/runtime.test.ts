import { describe, expect, it } from "vitest";

import { backendState, bangkokClock, sampleCheckpoint, stateLabel, thaiDateTime } from "./runtime";

describe("remote dashboard truth states", () => {
  it("distinguishes offline, unknown, stale, and connected", () => {
    expect(backendState(null, "BACKEND_NOT_CONFIGURED")).toBe("OFFLINE");
    expect(backendState(null, null)).toBe("UNKNOWN");
    const checked = new Date(Date.now() - 60_000).toISOString();
    expect(backendState({ checked_at: checked } as never, null)).toBe("STALE");
    expect(backendState({ checked_at: new Date().toISOString() } as never, null)).toBe("CONNECTED");
  });

  it("keeps sample checkpoints descriptive rather than profitable claims", () => {
    expect(sampleCheckpoint(0)).toContain("0 / 30");
    expect(sampleCheckpoint(30)).toContain("30 / 50");
    expect(sampleCheckpoint(200)).toBe("200+");
  });

  it("formats Bangkok time and preserves canonical state", () => {
    expect(thaiDateTime("2026-09-22T15:43:00Z")).toContain("22");
    expect(stateLabel("DEGRADED")).toContain("DEGRADED");
    expect(stateLabel("DISABLED")).toContain("ปิดใช้งาน");
  });

  it("uses IANA Bangkok time across +7 offset and calendar boundaries", () => {
    expect(bangkokClock("2026-09-22T15:43:00Z")).toBe("22:43:00");
    expect(bangkokClock("2026-09-22T23:30:00Z")).toBe("06:30:00");
    expect(thaiDateTime("2026-09-30T23:30:00Z")).toContain("01");
    expect(thaiDateTime("2026-12-31T23:30:00Z")).toContain("01");
    expect(bangkokClock("2026-09-22T15:43:00Z")).toBe("22:43:00");
  });
});
