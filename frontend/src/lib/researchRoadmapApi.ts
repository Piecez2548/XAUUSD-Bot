import { getJson, ApiRequestError } from "./api";
import type { RoadmapResponse, RoadmapTimeline } from "../types/researchRoadmap";

const SAFE_ROADMAP_ERRORS = new Set([
  "ROADMAP_RUN_NOT_FOUND",
  "ROADMAP_INVALID_LIMIT",
  "ROADMAP_INVALID_CURSOR",
  "ROADMAP_QUERY_FAILED",
]);

export function roadmapOverview(limit = 20, signal?: AbortSignal): Promise<RoadmapResponse> {
  return getJson<RoadmapResponse>(`/api/research/roadmap?limit=${limit}`, signal);
}

export function roadmapRun(runId: string, signal?: AbortSignal): Promise<RoadmapResponse> {
  return getJson<RoadmapResponse>(`/api/research/roadmap/runs/${encodeURIComponent(runId)}`, signal);
}

export function roadmapTimeline(
  runId: string,
  limit = 100,
  cursor?: string | null,
  signal?: AbortSignal,
): Promise<RoadmapTimeline> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  return getJson<RoadmapTimeline>(
    `/api/research/roadmap/runs/${encodeURIComponent(runId)}/timeline?${params.toString()}`,
    signal,
  );
}

export function sanitizeRoadmapError(reason: unknown): string {
  const code = reason instanceof ApiRequestError ? reason.code : reason instanceof Error ? reason.message : undefined;
  if (code && SAFE_ROADMAP_ERRORS.has(code)) {
    if (code === "ROADMAP_RUN_NOT_FOUND") return "The selected roadmap run is no longer available.";
    if (code === "ROADMAP_INVALID_LIMIT" || code === "ROADMAP_INVALID_CURSOR") return "The roadmap request could not be read.";
    return "The roadmap read service is temporarily unavailable.";
  }
  return "The roadmap read service is temporarily unavailable.";
}
