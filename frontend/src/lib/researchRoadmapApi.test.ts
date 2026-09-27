/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { roadmapOverview, roadmapRun, roadmapTimeline } from "./researchRoadmapApi";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(() => Promise.resolve(new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } })));
  vi.stubGlobal("fetch", fetchMock);
});

describe("research roadmap API client", () => {
  it("uses the authenticated read transport for overview, historical run, and timeline", async () => {
    await roadmapOverview();
    await roadmapRun("run/1");
    await roadmapTimeline("run/1", 100, "cursor/value");
    expect(fetchMock).toHaveBeenCalledTimes(3);
    for (const [url, init] of fetchMock.mock.calls) {
      expect(String(url)).toContain("/api/research/roadmap");
      expect(init?.method).toBeUndefined();
    }
    expect(fetchMock.mock.calls[1][0]).toContain("/runs/run%2F1");
    expect(fetchMock.mock.calls[2][0]).toContain("cursor=cursor%2Fvalue");
  });
});
