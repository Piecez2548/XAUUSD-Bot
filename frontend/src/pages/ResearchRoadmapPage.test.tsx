/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RoadmapResponse, RoadmapTimeline } from "../types/researchRoadmap";

const { overviewState, roadmapRun, roadmapTimeline } = vi.hoisted(() => ({
  overviewState: { data: null as RoadmapResponse | null, loading: false, error: null as string | null, refresh: vi.fn() },
  roadmapRun: vi.fn(),
  roadmapTimeline: vi.fn(),
}));

vi.mock("../hooks/useApi", () => ({ useApi: () => overviewState }));
vi.mock("../lib/researchRoadmapApi", () => ({
  roadmapRun,
  roadmapTimeline,
  sanitizeRoadmapError: () => "The roadmap read service is temporarily unavailable.",
}));

import { ResearchRoadmapPage } from "./ResearchRoadmapPage";

const event = (eventType: string, message: string, timestamp: string): RoadmapResponse["timeline_preview"][number] => ({
  event_type: eventType,
  timestamp,
  stage_key: null,
  attempt: null,
  message,
  metadata: {},
});

const fullResponse: RoadmapResponse = {
  contract_version: "research_roadmap_api_v1",
  current_run: {
    run_id: "run-current-1234567890",
    pipeline_key: "dataset_foundation_v1",
    pipeline_contract_version: "research_pipeline_v1a",
    lifecycle_state: "RUNNING",
    created_at: "2026-09-27T01:00:00Z",
    started_at: "2026-09-27T01:01:00Z",
    completed_at: null,
    current_stage_key: "DATASET_BUILD",
    execution_allowed: false,
  },
  stages: [
    { stage_key: "DATA_SOURCE", attempt: 1, lifecycle_state: "PASS", processed: 100, total: 100, unit: "rows", progress_mode: "DETERMINATE", display_percentage: 100, started_at: "2026-09-27T01:01:00Z", completed_at: "2026-09-27T01:02:00Z", reason: null, is_current_attempt: true, retry_count: 0, execution_allowed: false },
    { stage_key: "DATASET_BUILD", attempt: 2, lifecycle_state: "RUNNING", processed: 50, total: 100, unit: "rows", progress_mode: "DETERMINATE", display_percentage: 50, started_at: "2026-09-27T01:03:00Z", completed_at: null, reason: null, is_current_attempt: true, retry_count: 1, execution_allowed: false },
    { stage_key: "DATASET_AUDIT", attempt: 1, lifecycle_state: "WAITING", processed: 0, total: null, unit: "rows", progress_mode: "INDETERMINATE", display_percentage: null, started_at: null, completed_at: null, reason: null, is_current_attempt: true, retry_count: 0, execution_allowed: false },
    { stage_key: "ARTIFACT_VERIFY", attempt: 1, lifecycle_state: "BLOCKED", processed: 0, total: 0, unit: "artifacts", progress_mode: "DETERMINATE", display_percentage: null, started_at: null, completed_at: null, reason: "AUDIT_BLOCKED", is_current_attempt: true, retry_count: 0, execution_allowed: false },
  ],
  dataset_summary: {
    candidates_inspected: 120,
    rows_produced: 100,
    trainable_rows: 90,
    non_trainable_rows: 5,
    unresolved_rows: 3,
    excluded_rows: 2,
    semantic_dataset_hash: "a".repeat(64),
    dataset_contract_version: "dataset_v1",
    feature_contract_version: "features_v1",
    label_contract_version: "labels_v1",
  },
  artifacts: [{
    artifact_id: "artifact-1",
    artifact_type: "DATASET_MANIFEST",
    logical_identity: "dataset-foundation-v1",
    validation_state: "VALID",
    publication_state: "PUBLISHED",
    content_sha256: "b".repeat(64),
    semantic_dataset_hash: "a".repeat(64),
    producer_stage: "DATASET_BUILD",
    producer_attempt: 2,
    created_at: "2026-09-27T01:04:00Z",
    published_at: "2026-09-27T01:05:00Z",
  }],
  timeline_preview: [event("PIPELINE_CREATED", "Pipeline created", "2026-09-27T01:00:00Z")],
  source: { source_type: "PERSISTED_RESEARCH", source_logical_id: "research-source-1", symbol_policy: "XAUUSD", causal_cutoff_policy: "CLOSED_ONLY", continuity_policy: "STRICT", contract_versions: { source: "v1" } },
  recent_runs: [
    { run_id: "run-current-1234567890", pipeline_key: "dataset_foundation_v1", lifecycle_state: "RUNNING", created_at: "2026-09-27T01:00:00Z", completed_at: null },
    { run_id: "run-history-1234567890", pipeline_key: "dataset_foundation_v1", lifecycle_state: "PASS", created_at: "2026-09-26T01:00:00Z", completed_at: "2026-09-26T02:00:00Z" },
  ],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}

beforeEach(() => {
  overviewState.data = fullResponse;
  overviewState.loading = false;
  overviewState.error = null;
  overviewState.refresh.mockReset();
  roadmapRun.mockReset();
  roadmapTimeline.mockReset();
  roadmapTimeline.mockResolvedValue({ contract_version: "research_roadmap_api_v1", run_id: "run-current-1234567890", events: fullResponse.timeline_preview, next_cursor: null } satisfies RoadmapTimeline);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
});

afterEach(() => cleanup());

describe("ResearchRoadmapPage", () => {
  it("renders the four authoritative stages, summary, artifact metadata, and current stage evidence", async () => {
    render(<ResearchRoadmapPage />);
    expect(screen.getByRole("heading", { name: "Research Roadmap" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Data Source/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Dataset Build/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Dataset Audit/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Artifact Verify/ })).toBeTruthy();
    expect(screen.getByText("50.0%")).toBeTruthy();
    expect(screen.getByText("Candidates inspected")).toBeTruthy();
    expect(screen.getByText("DATASET_MANIFEST")).toBeTruthy();
    await waitFor(() => expect(roadmapTimeline).toHaveBeenCalledWith("run-current-1234567890", 100, null, expect.any(AbortSignal)));
  });

  it("keeps zero-total progress without a false zero percent", () => {
    render(<ResearchRoadmapPage />);
    fireEvent.click(screen.getByRole("button", { name: /Artifact Verify/ }));
    expect(screen.getByText("Total is zero; no percentage is displayed.")).toBeTruthy();
    expect(screen.queryByText("0.0%")).toBeNull();
  });

  it("supports frontend-local stage selection without a mutation request", () => {
    render(<ResearchRoadmapPage />);
    fireEvent.click(screen.getByRole("button", { name: /Data Source/ }));
    expect(screen.getAllByText("Data Source").length).toBeGreaterThan(0);
    expect(roadmapRun).not.toHaveBeenCalled();
  });

  it("loads a historical run with GET-only detail and preserves timeline order", async () => {
    const historical: RoadmapResponse = { ...fullResponse, current_run: { ...fullResponse.current_run!, run_id: "run-history-1234567890", lifecycle_state: "PASS", current_stage_key: "ARTIFACT_VERIFY" }, recent_runs: [fullResponse.recent_runs[1]] };
    roadmapRun.mockResolvedValue(historical);
    roadmapTimeline.mockResolvedValue({ contract_version: "research_roadmap_api_v1", run_id: "run-history-1234567890", events: [event("PIPELINE_CREATED", "first", "2026-09-26T01:00:00Z"), event("PIPELINE_PASSED", "last", "2026-09-26T02:00:00Z")], next_cursor: null } satisfies RoadmapTimeline);
    render(<ResearchRoadmapPage />);
    fireEvent.click(screen.getByRole("button", { name: /run-history-123456/ }));
    await waitFor(() => expect(roadmapRun).toHaveBeenCalledWith("run-history-1234567890", expect.any(AbortSignal)));
    expect(await screen.findByText("first")).toBeTruthy();
    expect(screen.getByText("last")).toBeTruthy();
  });

  it("loads bounded timeline pages and appends API order", async () => {
    roadmapTimeline.mockResolvedValueOnce({ contract_version: "research_roadmap_api_v1", run_id: "run-current-1234567890", events: [event("PIPELINE_CREATED", "first", "2026-09-27T01:00:00Z")], next_cursor: "cursor-1" } satisfies RoadmapTimeline);
    roadmapTimeline.mockResolvedValueOnce({ contract_version: "research_roadmap_api_v1", run_id: "run-current-1234567890", events: [event("PIPELINE_PASSED", "second", "2026-09-27T02:00:00Z")], next_cursor: null } satisfies RoadmapTimeline);
    render(<ResearchRoadmapPage />);
    const loadMore = await screen.findByRole("button", { name: "Load more" });
    fireEvent.click(loadMore);
    expect(await screen.findByText("second")).toBeTruthy();
    expect(roadmapTimeline).toHaveBeenLastCalledWith("run-current-1234567890", 100, "cursor-1", expect.any(AbortSignal));
  });

  it("keeps retry history visible while the authoritative current attempt drives state", () => {
    overviewState.data = {
      ...fullResponse,
      stages: [
        ...fullResponse.stages.filter((stage) => stage.stage_key !== "DATASET_BUILD"),
        { ...fullResponse.stages[1], attempt: 1, lifecycle_state: "FAILED", is_current_attempt: false, retry_count: 1, reason: "BUILD_FAILED" },
        { ...fullResponse.stages[1], attempt: 2, lifecycle_state: "PASS", is_current_attempt: true, retry_count: 1, reason: null },
      ],
    };
    render(<ResearchRoadmapPage />);
    expect(screen.getByText("Attempt 1")).toBeTruthy();
    expect(screen.getByText("Attempt 2 · current")).toBeTruthy();
    expect(screen.getByText("BUILD_FAILED")).toBeTruthy();
    expect(screen.getAllByText("PASS").length).toBeGreaterThan(0);
  });

  it("renders truthful determinate, unknown-total, and invalid-count progress", () => {
    render(<ResearchRoadmapPage />);
    expect(screen.getByText("50.0%")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Dataset Audit/ }));
    expect(screen.getByText(/Progress is indeterminate/)).toBeTruthy();

    overviewState.data = {
      ...fullResponse,
      stages: fullResponse.stages.map((stage) => stage.stage_key === "DATASET_BUILD" ? { ...stage, processed: -1 } : stage),
    };
    cleanup();
    render(<ResearchRoadmapPage />);
    expect(screen.getByText("Progress unavailable; the API returned an invalid count.")).toBeTruthy();
  });

  it("fails safely on an unknown future lifecycle value", () => {
    overviewState.data = {
      ...fullResponse,
      stages: fullResponse.stages.map((stage) => stage.stage_key === "DATASET_BUILD" ? { ...stage, lifecycle_state: "FUTURE_STATE" as RoadmapResponse["stages"][number]["lifecycle_state"] } : stage),
    };
    render(<ResearchRoadmapPage />);
    expect(screen.getAllByText("FUTURE_STATE").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /Dataset Build/ })).toBeTruthy();
  });

  it("does not render adversarial source fields outside the whitelisted logical model", () => {
    const adversarial = {
      ...fullResponse.source!,
      source_logical_id: "C:\\secrets\\.env",
      contract_versions: { source: "postgres://user:password@host/db", api_key: "sk-secret" },
      unexpected_path: "../../credentials.json",
    } as unknown as RoadmapResponse["source"];
    overviewState.data = { ...fullResponse, source: adversarial };
    render(<ResearchRoadmapPage />);
    expect(screen.getByText("Logical source")).toBeTruthy();
    expect(screen.getByText("Unavailable")).toBeTruthy();
    expect(screen.queryByText(".env")).toBeNull();
    expect(screen.queryByText(/postgres|password|sk-secret|credentials/)).toBeNull();
  });

  it("keeps the newest selected run when an older historical request resolves later", async () => {
    const runA = "run-history-a";
    const runB = "run-history-b";
    const responseA = { ...fullResponse, current_run: { ...fullResponse.current_run!, run_id: runA, pipeline_key: "pipeline-a" } };
    const responseB = { ...fullResponse, current_run: { ...fullResponse.current_run!, run_id: runB, pipeline_key: "pipeline-b" } };
    overviewState.data = { ...fullResponse, recent_runs: [
      { run_id: runA, pipeline_key: "pipeline-a", lifecycle_state: "RUNNING", created_at: "2026-09-27T02:00:00Z", completed_at: null },
      { run_id: runB, pipeline_key: "pipeline-b", lifecycle_state: "RUNNING", created_at: "2026-09-27T03:00:00Z", completed_at: null },
    ] };
    const pendingA = deferred<RoadmapResponse>();
    const pendingB = deferred<RoadmapResponse>();
    roadmapRun.mockImplementation((runId: string) => runId === runA ? pendingA.promise : pendingB.promise);
    render(<ResearchRoadmapPage />);
    fireEvent.click(screen.getByRole("button", { name: /run-history-a/ }));
    fireEvent.click(screen.getByRole("button", { name: /run-history-b/ }));
    pendingB.resolve(responseB);
    await waitFor(() => expect(screen.getByText("pipeline-b")).toBeTruthy());
    pendingA.resolve(responseA);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const banner = document.querySelector(".roadmap-run-banner");
    expect(banner?.textContent).toContain("pipeline-b");
    expect(banner?.textContent).not.toContain("pipeline-a");
  });

  it("resets timeline cursor on run change and ignores a stale pagination response", async () => {
    const pendingPage = deferred<RoadmapTimeline>();
    const historical = { ...fullResponse, current_run: { ...fullResponse.current_run!, run_id: "run-history-1234567890", pipeline_key: "historical-pipeline" } };
    roadmapRun.mockResolvedValue(historical);
    roadmapTimeline.mockImplementation((runId: string, _limit: number, cursor: string | null) => {
      if (runId === "run-current-1234567890" && cursor === "cursor-1") return pendingPage.promise;
      if (runId === "run-current-1234567890") return Promise.resolve({ contract_version: "research_roadmap_api_v1", run_id: runId, events: [event("PIPELINE_CREATED", "current-first", "2026-09-27T01:00:00Z")], next_cursor: "cursor-1" } satisfies RoadmapTimeline);
      return Promise.resolve({ contract_version: "research_roadmap_api_v1", run_id: runId, events: [event("PIPELINE_PASSED", "historical-only", "2026-09-26T01:00:00Z")], next_cursor: null } satisfies RoadmapTimeline);
    });
    render(<ResearchRoadmapPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    fireEvent.click(screen.getByRole("button", { name: /run-history-123456/ }));
    await waitFor(() => expect(screen.getByText("historical-only")).toBeTruthy());
    pendingPage.resolve({ contract_version: "research_roadmap_api_v1", run_id: "run-current-1234567890", events: [event("PIPELINE_PASSED", "stale-current-page", "2026-09-27T02:00:00Z")], next_cursor: null });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("stale-current-page")).toBeNull();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("renders empty and sanitized error states", () => {
    overviewState.data = { ...fullResponse, current_run: null, stages: [], artifacts: [], recent_runs: [] };
    render(<ResearchRoadmapPage />);
    expect(screen.getByText("No roadmap run available")).toBeTruthy();
    overviewState.data = fullResponse;
    overviewState.error = "sensitive path should not be displayed";
    const { unmount } = render(<ResearchRoadmapPage />);
    expect(screen.getAllByText("The roadmap read service is temporarily unavailable.").length).toBeGreaterThan(0);
    expect(screen.queryByText("sensitive path should not be displayed")).toBeNull();
    unmount();
  });

  it("copies the full hash while displaying a shortened value", async () => {
    render(<ResearchRoadmapPage />);
    const copyButton = screen.getByRole("button", { name: "Copy full Semantic dataset hash" });
    fireEvent.click(copyButton);
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("a".repeat(64)));
    expect(screen.getByTitle("a".repeat(64))).toBeTruthy();
  });

  it("does not crash when Clipboard API is unavailable", () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    render(<ResearchRoadmapPage />);
    expect(() => fireEvent.click(screen.getByRole("button", { name: "Copy full Semantic dataset hash" }))).not.toThrow();
  });
});
