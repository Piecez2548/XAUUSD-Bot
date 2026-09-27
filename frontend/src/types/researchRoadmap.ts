export const ROADMAP_CONTRACT_VERSION = "research_roadmap_api_v1" as const;

export const ROADMAP_STAGE_KEYS = [
  "DATA_SOURCE",
  "DATASET_BUILD",
  "DATASET_AUDIT",
  "ARTIFACT_VERIFY",
] as const;

export type RoadmapStageKey = (typeof ROADMAP_STAGE_KEYS)[number];
export type RoadmapLifecycleState =
  | "WAITING"
  | "READY"
  | "RUNNING"
  | "PASS"
  | "BLOCKED"
  | "FAILED"
  | "SKIPPED"
  | "CANCELLED"
  | "UNKNOWN";
export type RoadmapProgressMode = "DETERMINATE" | "INDETERMINATE";

export interface RoadmapRun {
  run_id: string;
  pipeline_key: string;
  pipeline_contract_version: string;
  lifecycle_state: RoadmapLifecycleState;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  current_stage_key: string | null;
  execution_allowed: false;
}

export interface RoadmapStage {
  stage_key: string;
  attempt: number;
  lifecycle_state: RoadmapLifecycleState;
  processed: number;
  total: number | null;
  unit: string | null;
  progress_mode: RoadmapProgressMode;
  display_percentage: number | null;
  started_at: string | null;
  completed_at: string | null;
  reason: string | null;
  is_current_attempt: boolean;
  retry_count: number;
  execution_allowed: false;
}

export interface RoadmapDatasetSummary {
  candidates_inspected: number;
  rows_produced: number;
  trainable_rows: number;
  non_trainable_rows: number;
  unresolved_rows: number;
  excluded_rows: number;
  semantic_dataset_hash: string;
  dataset_contract_version: string;
  feature_contract_version: string;
  label_contract_version: string;
}

export interface RoadmapSource {
  source_type: string;
  source_logical_id: string | null;
  symbol_policy: string | null;
  causal_cutoff_policy: string | null;
  continuity_policy: string | null;
  contract_versions: Record<string, string>;
}

export interface RoadmapArtifact {
  artifact_id: string;
  artifact_type: string;
  logical_identity: string;
  validation_state: string;
  publication_state: string;
  content_sha256: string;
  semantic_dataset_hash: string | null;
  producer_stage: string;
  producer_attempt: number;
  created_at: string;
  published_at: string | null;
}

export interface RoadmapEvent {
  event_type: string;
  timestamp: string;
  stage_key: string | null;
  attempt: number | null;
  message: string;
  metadata: Record<string, unknown>;
}

export interface RoadmapTimeline {
  contract_version: typeof ROADMAP_CONTRACT_VERSION;
  run_id: string;
  events: RoadmapEvent[];
  next_cursor: string | null;
}

export interface RoadmapRunHistory {
  run_id: string;
  pipeline_key: string;
  lifecycle_state: RoadmapLifecycleState;
  created_at: string;
  completed_at: string | null;
}

export interface RoadmapResponse {
  contract_version: typeof ROADMAP_CONTRACT_VERSION;
  current_run: RoadmapRun | null;
  stages: RoadmapStage[];
  dataset_summary: RoadmapDatasetSummary | null;
  artifacts: RoadmapArtifact[];
  timeline_preview: RoadmapEvent[];
  source: RoadmapSource | null;
  recent_runs: RoadmapRunHistory[];
}
