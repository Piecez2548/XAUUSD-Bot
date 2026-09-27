import {
  Activity,
  Check,
  CheckCircle2,
  ChevronRight,
  Clipboard,
  Clock3,
  Copy,
  Database,
  FileCheck2,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Panel } from "../components/Panel";
import { StatusPill } from "../components/StatusPill";
import { useApi } from "../hooks/useApi";
import { number } from "../lib/format";
import {
  roadmapRun,
  roadmapTimeline,
  sanitizeRoadmapError,
} from "../lib/researchRoadmapApi";
import {
  ROADMAP_STAGE_KEYS,
  type RoadmapArtifact,
  type RoadmapEvent,
  type RoadmapLifecycleState,
  type RoadmapResponse,
  type RoadmapRun,
  type RoadmapStage,
  type RoadmapStageKey,
} from "../types/researchRoadmap";

const STAGE_LABELS: Record<RoadmapStageKey, string> = {
  DATA_SOURCE: "Data Source",
  DATASET_BUILD: "Dataset Build",
  DATASET_AUDIT: "Dataset Audit",
  ARTIFACT_VERIFY: "Artifact Verify",
};

const STAGE_ICONS: Record<RoadmapStageKey, typeof Database> = {
  DATA_SOURCE: Database,
  DATASET_BUILD: Activity,
  DATASET_AUDIT: ShieldAlert,
  ARTIFACT_VERIFY: FileCheck2,
};

const EMPTY_STAGES: RoadmapStage[] = [];
const EMPTY_EVENTS: RoadmapEvent[] = [];

const lifecycleLabel = (state: RoadmapLifecycleState | null): string => {
  if (state === "WAITING" || state === "READY") return "PENDING";
  return state ?? "UNAVAILABLE";
};

function RoadmapStatus({ state }: { state: RoadmapLifecycleState | null }) {
  const tone = state === "PASS" ? "good" : state === "RUNNING" ? "warn" : state === "FAILED" || state === "BLOCKED" ? "bad" : "muted";
  return <span className={`status-pill ${tone}`} title={`Lifecycle: ${lifecycleLabel(state)}`}><span className="status-dot" aria-hidden="true" /><strong>{lifecycleLabel(state)}</strong></span>;
}

const stageFor = (stages: RoadmapStage[], key: RoadmapStageKey): RoadmapStage | null =>
  stages.find((stage) => stage.stage_key === key && stage.is_current_attempt)
  ?? null;

const attemptsFor = (stages: RoadmapStage[], key: RoadmapStageKey): RoadmapStage[] =>
  stages.filter((stage) => stage.stage_key === key).sort((left, right) => left.attempt - right.attempt);

const selectedStage = (run: RoadmapRun | null, stages: RoadmapStage[]): RoadmapStageKey => {
  if (run?.current_stage_key && ROADMAP_STAGE_KEYS.includes(run.current_stage_key as RoadmapStageKey)) {
    return run.current_stage_key as RoadmapStageKey;
  }
  const active = stages.find((stage) => stage.is_current_attempt && stage.lifecycle_state === "RUNNING");
  return active && ROADMAP_STAGE_KEYS.includes(active.stage_key as RoadmapStageKey)
    ? active.stage_key as RoadmapStageKey
    : ROADMAP_STAGE_KEYS[0];
};

function useSelectedRun(runId: string | null, revision: number) {
  const [state, setState] = useState<{ data: RoadmapResponse | null; loading: boolean; error: string | null }>({ data: null, loading: false, error: null });
  useEffect(() => {
    if (!runId) {
      setState({ data: null, loading: false, error: null });
      return;
    }
    const controller = new AbortController();
    setState({ data: null, loading: true, error: null });
    void roadmapRun(runId, controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setState({ data, loading: false, error: null });
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) setState({ data: null, loading: false, error: sanitizeRoadmapError(reason) });
      },
    );
    return () => controller.abort();
  }, [runId, revision]);
  return state;
}

function useRoadmapTimeline(
  runId: string | null,
  preview: RoadmapEvent[],
  revision: number,
) {
  const [events, setEvents] = useState<RoadmapEvent[]>(preview);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestGeneration = useRef(0);
  const paginationController = useRef<AbortController | null>(null);

  useEffect(() => {
    const generation = requestGeneration.current + 1;
    requestGeneration.current = generation;
    paginationController.current?.abort();
    paginationController.current = null;
    if (!runId) {
      setEvents([]);
      setNextCursor(null);
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setEvents(preview);
    setNextCursor(null);
    setLoading(true);
    setError(null);
    void roadmapTimeline(runId, 100, null, controller.signal).then(
      (data) => {
        if (generation !== requestGeneration.current || controller.signal.aborted) return;
        setEvents(data.events);
        setNextCursor(data.next_cursor);
        setLoading(false);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(sanitizeRoadmapError(reason));
          setLoading(false);
        }
      },
    );
    return () => {
      controller.abort();
      paginationController.current?.abort();
    };
  }, [runId, preview, revision]);

  async function loadMore() {
    if (!runId || !nextCursor || loadingMore) return;
    const generation = requestGeneration.current;
    const controller = new AbortController();
    paginationController.current?.abort();
    paginationController.current = controller;
    setLoadingMore(true);
    try {
      const data = await roadmapTimeline(runId, 100, nextCursor, controller.signal);
      if (generation !== requestGeneration.current || controller.signal.aborted) return;
      setEvents((current) => [...current, ...data.events]);
      setNextCursor(data.next_cursor);
      setError(null);
    } catch (reason: unknown) {
      if (generation === requestGeneration.current && !controller.signal.aborted) {
        setError(sanitizeRoadmapError(reason));
      }
    } finally {
      if (generation === requestGeneration.current) setLoadingMore(false);
      if (paginationController.current === controller) paginationController.current = null;
    }
  }

  return { events, nextCursor, loading, loadingMore, error, loadMore };
}

export function ResearchRoadmapPage() {
  const overview = useApi<RoadmapResponse>("/api/research/roadmap?limit=20");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedStageKey, setSelectedStageKey] = useState<RoadmapStageKey>("DATA_SOURCE");
  const [refreshRevision, setRefreshRevision] = useState(0);
  const selectedRun = useSelectedRun(selectedRunId, refreshRevision);
  const activeResponse = selectedRunId ? selectedRun.data : overview.data;
  const activeRun = activeResponse?.current_run ?? null;
  const activeStages = activeResponse?.stages ?? EMPTY_STAGES;
  const preview = activeResponse?.timeline_preview ?? EMPTY_EVENTS;
  const timeline = useRoadmapTimeline(activeRun?.run_id ?? null, preview, refreshRevision);
  const currentStage = stageFor(activeStages, selectedStageKey);
  const currentAttempts = attemptsFor(activeStages, selectedStageKey);
  const defaultStageKey = selectedStage(activeRun, activeStages);
  const history = overview.data?.recent_runs ?? activeResponse?.recent_runs ?? [];

  useEffect(() => {
    setSelectedStageKey(defaultStageKey);
  }, [defaultStageKey]);

  const busy = overview.loading || selectedRun.loading;
  const error = selectedRunId ? selectedRun.error : overview.error ? sanitizeRoadmapError(overview.error) : null;
  const refresh = () => {
    overview.refresh();
    setRefreshRevision((value) => value + 1);
  };

  const empty = !busy && !error && !activeResponse?.current_run;
  return (
    <div className="page roadmap-page">
      <div className="page-heading roadmap-heading">
        <div>
          <div className="title-row"><h1>Research Roadmap</h1><span className="page-context">READ ONLY · V1C EVIDENCE</span></div>
          <p>Dataset research stages, bounded lineage, and historical run evidence.</p>
        </div>
        <div className="roadmap-heading-actions">
          {selectedRunId && <button className="text-button" type="button" onClick={() => setSelectedRunId(null)}>Current run</button>}
          <button className="text-button" type="button" onClick={refresh} disabled={busy} aria-label="Refresh research roadmap"><RefreshCw size={14} aria-hidden="true" />Refresh</button>
          <StatusPill label="Execution" state="DISABLED" />
        </div>
      </div>

      {busy && <div className="roadmap-state" role="status"><LoaderCircle size={20} aria-hidden="true" /><strong>Loading roadmap evidence</strong><span>Reading the authenticated GET-only roadmap API.</span></div>}
      {error && <div className="roadmap-state roadmap-error" role="alert"><XCircle size={20} aria-hidden="true" /><strong>Roadmap unavailable</strong><span>{error}</span><button className="text-button" type="button" onClick={refresh}>Retry read</button></div>}
      {empty && <div className="roadmap-state"><Clipboard size={20} aria-hidden="true" /><strong>No roadmap run available</strong><span>No persisted research pipeline run is available yet. The dashboard does not create one.</span></div>}

      {!busy && !error && activeResponse?.current_run && <>
        <RunOverview run={activeRun} selectedRunId={selectedRunId} />
        <StageRoadmap stages={activeStages} selectedStageKey={selectedStageKey} onSelect={setSelectedStageKey} />
        <div className="roadmap-two-column">
        <CurrentStagePanel run={activeRun} stage={currentStage} attempts={currentAttempts} />
          <DataSourcePanel source={activeResponse.source} />
        </div>
        <DatasetSummaryPanel summary={activeResponse.dataset_summary} />
        <ArtifactPanel artifacts={activeResponse.artifacts} />
        <TimelinePanel timeline={timeline} />
      </>}

      {!error && history.length > 0 && <RunHistory runs={history} selectedRunId={selectedRunId} onSelect={setSelectedRunId} />}
      {selectedRunId && selectedRun.error && <span className="sr-only">The selected historical run could not be loaded.</span>}
    </div>
  );
}

function RunOverview({ run, selectedRunId }: { run: RoadmapRun | null; selectedRunId: string | null }) {
  if (!run) return null;
  return <div className="roadmap-run-banner"><div><span className="panel-context">{selectedRunId ? "HISTORICAL RUN" : "CURRENT RUN"}</span><strong>{run.pipeline_key}</strong><code title={run.run_id}>{shortHash(run.run_id, 18)}</code></div><div className="roadmap-run-meta"><RoadmapStatus state={run.lifecycle_state} /><span>Created {localDateTime(run.created_at)}</span><span>{run.execution_allowed ? "Execution available" : "Execution disabled"}</span></div></div>;
}

function StageRoadmap({ stages, selectedStageKey, onSelect }: { stages: RoadmapStage[]; selectedStageKey: RoadmapStageKey; onSelect: (key: RoadmapStageKey) => void }) {
  return <Panel title="Roadmap" kicker="AUTHORITATIVE STAGE ORDER"><div className="roadmap-stage-flow" aria-label="Research pipeline stages">{ROADMAP_STAGE_KEYS.map((key, index) => { const stage = stageFor(stages, key); const Icon = STAGE_ICONS[key]; const state = stage?.lifecycle_state ?? null; return <div className="roadmap-stage-wrap" key={key}><button type="button" className={`roadmap-stage ${selectedStageKey === key ? "selected" : ""}`} onClick={() => onSelect(key)} aria-pressed={selectedStageKey === key}><span className="roadmap-stage-index">0{index + 1}</span><span className="roadmap-stage-icon"><Icon size={17} aria-hidden="true" /></span><span className="roadmap-stage-copy"><strong>{STAGE_LABELS[key]}</strong><small>{lifecycleLabel(state)}</small></span><RoadmapStatus state={state} /></button>{index < ROADMAP_STAGE_KEYS.length - 1 && <ChevronRight className="roadmap-connector" size={17} aria-hidden="true" />}</div>; })}</div><p className="prose-note"><CheckCircle2 size={14} aria-hidden="true" />Connectors show the declared sequence only; downstream success is not inferred from an earlier stage.</p></Panel>;
}

function CurrentStagePanel({ run, stage, attempts }: { run: RoadmapRun | null; stage: RoadmapStage | null; attempts: RoadmapStage[] }) {
  const percent = progressPercent(stage);
  return <Panel title="Current stage" kicker="FRONTEND-LOCAL SELECTION"><div className="current-stage-summary"><strong>{stage ? STAGE_LABELS[stage.stage_key as RoadmapStageKey] ?? stage.stage_key : run?.current_stage_key ?? "No stage selected"}</strong><RoadmapStatus state={stage?.lifecycle_state ?? null} /></div>{stage ? <><div className="metrics compact"><div className="metric-row"><span>Lifecycle</span><strong>{lifecycleLabel(stage.lifecycle_state)}</strong></div><div className="metric-row"><span>Attempt / retries</span><strong>{stage.attempt} / {stage.retry_count}</strong></div><div className="metric-row"><span>Evidence</span><strong>{formatCount(stage.processed)}{stage.total == null ? "" : ` / ${formatCount(stage.total)}`} {safeLogicalValue(stage.unit) ?? "Unavailable"}</strong></div><div className="metric-row"><span>Started / completed</span><strong>{localDateTime(stage.started_at)} → {localDateTime(stage.completed_at)}</strong></div><div className="metric-row"><span>Reason</span><strong>{stage.reason ?? "No reason recorded"}</strong></div></div><ProgressBar stage={stage} percent={percent} />{attempts.length > 1 && <AttemptHistory attempts={attempts} />}</> : <div className="roadmap-subtle">No persisted attempt is available for this stage.</div>}</Panel>;
}

function AttemptHistory({ attempts }: { attempts: RoadmapStage[] }) {
  return <div className="roadmap-attempt-history"><span className="roadmap-attempt-history-label">Attempt history</span>{attempts.map((attempt) => <div className={`roadmap-attempt ${attempt.is_current_attempt ? "current" : ""}`} key={`${attempt.stage_key}-${attempt.attempt}`}><span>Attempt {attempt.attempt}{attempt.is_current_attempt ? " · current" : ""}</span><RoadmapStatus state={attempt.lifecycle_state} /><small>{attempt.reason ?? "No reason recorded"}</small></div>)}</div>;
}

function ProgressBar({ stage, percent }: { stage: RoadmapStage; percent: number | null }) {
  const processed = safeCount(stage.processed);
  const total = safeCount(stage.total);
  if (processed == null || (stage.total != null && total == null)) return <div className="progress-note"><span>Progress unavailable; the API returned an invalid count.</span></div>;
  if (total === 0) return <div className="progress-note"><span>Total is zero; no percentage is displayed.</span></div>;
  if (stage.progress_mode !== "DETERMINATE" && stage.progress_mode !== "INDETERMINATE") return <div className="progress-note"><span>Progress unavailable; the API returned an invalid mode.</span></div>;
  if (stage.progress_mode === "INDETERMINATE" || stage.total == null || percent == null) return <div className="progress-note"><span>Progress is indeterminate{safeLogicalValue(stage.unit) ? ` · ${safeLogicalValue(stage.unit)}` : ""}</span><div className="progress-track indeterminate"><span /></div></div>;
  return <div className="progress-note"><div className="progress-label"><span>{number(processed, 0)} / {number(total, 0)} {safeLogicalValue(stage.unit) ?? "Unavailable"}</span><strong>{percent.toFixed(1)}%</strong></div><div className="progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-label={`${STAGE_LABELS[stage.stage_key as RoadmapStageKey] ?? stage.stage_key} progress`}><span style={{ width: `${percent}%` }} /></div></div>;
}

function DataSourcePanel({ source }: { source: RoadmapResponse["source"] }) {
  return <Panel title="Data source" kicker="BOUNDED SOURCE PROVENANCE">{source ? <div className="roadmap-detail-grid"><Detail label="Source type" value={safeSourceValue(source.source_type)} /><Detail label="Logical source" value={safeSourceValue(source.source_logical_id)} /><Detail label="Symbol policy" value={safeSourceValue(source.symbol_policy)} /><Detail label="Causal cutoff" value={safeSourceValue(source.causal_cutoff_policy)} /><Detail label="Continuity policy" value={safeSourceValue(source.continuity_policy)} /><div className="roadmap-detail-wide"><span>Contract versions</span><div className="tag-list">{Object.entries(source.contract_versions).filter(([key, value]) => safeSourceValue(key) && safeSourceValue(value)).map(([key, value]) => <span key={key}>{safeSourceValue(key)}: {safeSourceValue(value)}</span>)}</div></div></div> : <div className="roadmap-subtle">No source provenance was returned for this run.</div>}</Panel>;
}

function DatasetSummaryPanel({ summary }: { summary: RoadmapResponse["dataset_summary"] }) {
  return <Panel title="Dataset summary" kicker="LINEAGE AND QUALITY EVIDENCE">{summary ? <><div className="roadmap-stat-grid">{[["Candidates inspected", summary.candidates_inspected], ["Rows produced", summary.rows_produced], ["Trainable rows", summary.trainable_rows], ["Non-trainable rows", summary.non_trainable_rows], ["Unresolved rows", summary.unresolved_rows], ["Excluded rows", summary.excluded_rows]].map(([label, value]) => <div className="roadmap-stat" key={String(label)}><span>{String(label)}</span><strong>{number(value as number, 0)}</strong></div>)}</div><div className="roadmap-contract-grid"><HashValue label="Semantic dataset hash" value={summary.semantic_dataset_hash} /><Detail label="Dataset contract" value={summary.dataset_contract_version} /><Detail label="Feature contract" value={summary.feature_contract_version} /><Detail label="Label contract" value={summary.label_contract_version} /></div></> : <div className="roadmap-subtle">No dataset summary was returned for this run.</div>}</Panel>;
}

function ArtifactPanel({ artifacts }: { artifacts: RoadmapArtifact[] }) {
  return <Panel title="Artifacts" kicker="METADATA ONLY"><div className="roadmap-artifacts">{artifacts.length ? artifacts.map((artifact) => <article className="roadmap-artifact" key={artifact.artifact_id}><div className="roadmap-artifact-heading"><div><strong>{safeLogicalValue(artifact.artifact_type) ?? "Unavailable"}</strong><span>{safeLogicalValue(artifact.logical_identity) ?? "Unavailable"}</span></div><div className="roadmap-artifact-states"><ArtifactState label="Validation" value={artifact.validation_state} /><ArtifactState label="Publication" value={artifact.publication_state} /></div></div><div className="roadmap-artifact-meta"><Detail label="Producer" value={`${safeLogicalValue(artifact.producer_stage) ?? "Unavailable"} · attempt ${formatCount(artifact.producer_attempt)}`} /><HashValue label="Content SHA-256" value={artifact.content_sha256} /></div></article>) : <div className="roadmap-subtle">No artifact metadata was returned for this run.</div>}</div></Panel>;
}

function ArtifactState({ label, value }: { label: string; value: string | null | undefined }) {
  const state = value ?? "Unavailable";
  const tone = state === "VALIDATED" || state === "VALID" || state === "PUBLISHED" ? "good" : state === "UNVALIDATED" || state === "UNPUBLISHED" ? "warn" : "muted";
  return <span className={`roadmap-artifact-state ${tone}`} title={`${label}: ${state}`}><span>{label}</span><strong>{state}</strong></span>;
}

function TimelinePanel({ timeline }: { timeline: ReturnType<typeof useRoadmapTimeline> }) {
  return <Panel title="Event timeline" kicker="API ORDER · LOCAL DISPLAY TIME" action={timeline.nextCursor ? <button className="text-button" type="button" onClick={() => void timeline.loadMore()} disabled={timeline.loadingMore}>{timeline.loadingMore ? "Loading…" : "Load more"}</button> : undefined}>{timeline.error && <p className="inline-warning" role="alert"><ShieldAlert size={14} />{timeline.error}</p>}{timeline.loading && !timeline.events.length ? <div className="roadmap-subtle">Loading timeline events…</div> : timeline.events.length ? <ol className="roadmap-timeline">{timeline.events.map((event, index) => <li key={`${event.event_type}-${event.timestamp}-${event.attempt ?? "none"}-${index}`}><span className="roadmap-timeline-marker" aria-hidden="true"><Clock3 size={13} /></span><div><div className="roadmap-timeline-heading"><strong>{event.event_type.replaceAll("_", " ")}</strong><time dateTime={event.timestamp}>{localDateTime(event.timestamp)}</time></div><span>{event.message}</span>{(event.stage_key || event.attempt != null) && <small>{event.stage_key ? STAGE_LABELS[event.stage_key as RoadmapStageKey] ?? event.stage_key : "Pipeline"}{event.attempt == null ? "" : ` · attempt ${event.attempt}`}</small>}</div></li>)}</ol> : <div className="roadmap-subtle">No timeline events were returned for this run.</div>}</Panel>;
}

function RunHistory({ runs, selectedRunId, onSelect }: { runs: RoadmapResponse["recent_runs"]; selectedRunId: string | null; onSelect: (runId: string) => void }) {
  return <Panel title="Run history" kicker="BOUNDED HISTORICAL GET"><div className="roadmap-history">{runs.length ? runs.map((run) => <button type="button" className={`roadmap-history-row ${selectedRunId === run.run_id ? "selected" : ""}`} key={run.run_id} onClick={() => onSelect(run.run_id)}><span className="roadmap-history-id"><strong>{shortHash(run.run_id, 18)}</strong><small>{run.pipeline_key}</small></span><RoadmapStatus state={run.lifecycle_state} /><span>{lifecycleLabel(run.lifecycle_state)}</span><time dateTime={run.created_at}>{localDateTime(run.created_at)}</time><ChevronRight size={15} aria-hidden="true" /></button>) : <div className="roadmap-subtle">No historical runs were returned.</div>}</div></Panel>;
}

function Detail({ label, value }: { label: string; value: string | null | undefined }) {
  return <div><span>{label}</span><strong>{value ?? "Unavailable"}</strong></div>;
}

function HashValue({ label, value }: { label: string; value: string | null }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    if (!value || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }
  return <div className="hash-value"><span>{label}</span><div><code title={value ?? undefined}>{value ? shortHash(value, 16) : "Unavailable"}</code>{value && <button className="icon-button" type="button" onClick={() => void copy()} aria-label={`Copy full ${label}`} title={copied ? "Copied" : "Copy full hash"}>{copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}</button>}</div></div>;
}

function progressPercent(stage: RoadmapStage | null): number | null {
  if (!stage || safeCount(stage.processed) == null || stage.total == null || safeCount(stage.total) == null || stage.total <= 0) return null;
  if (stage.display_percentage != null && Number.isFinite(stage.display_percentage)) return Math.min(100, Math.max(0, stage.display_percentage));
  return Math.min(100, Math.max(0, (stage.processed / stage.total) * 100));
}

function safeCount(value: number | null | undefined): number | null {
  return value != null && Number.isFinite(value) && value >= 0 ? value : null;
}

function formatCount(value: number | null | undefined): string {
  const safe = safeCount(value);
  return safe == null ? "Unavailable" : number(safe, 0);
}

function safeLogicalValue(value: string | null | undefined): string | null {
  return value != null && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/.test(value) ? value : null;
}

function safeSourceValue(value: string | null | undefined): string | null {
  const safe = safeLogicalValue(value);
  return safe != null && !/(?:password|token|secret|api[_-]?key|sk[-_]|credential)/i.test(safe) ? safe : null;
}

function shortHash(value: string, length: number): string {
  return value.length <= length ? value : `${value.slice(0, length)}…`;
}

function localDateTime(value: string | null | undefined): string {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unavailable" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}
