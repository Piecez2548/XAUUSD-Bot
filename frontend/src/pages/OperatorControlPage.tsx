import { useState } from "react";

import { Panel } from "../components/Panel";
import { StatusPill } from "../components/StatusPill";
import { DataState } from "../components/DataState";
import { useApi } from "../hooks/useApi";
import { postJson } from "../lib/api";
import { thaiDateTime } from "../lib/runtime";
import type { ControlResponse, ControlStatus, ServiceState } from "../types";

type ControlCommand = "start" | "stop" | "restart" | "demo-on" | "demo-off";

type SafeControlStatus = {
  checked_at?: string;
  control?: ServiceState;
  supervisor?: ServiceState;
  api?: ServiceState;
  live?: ServiceState;
  mt5?: ServiceState;
  database?: ServiceState;
  telegram?: ServiceState;
  forward_shadow?: ServiceState;
  execution?: Partial<ControlStatus["execution"]>;
  strategy?: Partial<ControlStatus["strategy"]>;
};

const systemComponents: Array<[keyof SafeControlStatus, string]> = [
  ["control", "Control"],
  ["supervisor", "Supervisor"],
  ["api", "API"],
  ["live", "Live Engine"],
  ["mt5", "MT5"],
  ["database", "Database"],
  ["telegram", "Telegram"],
  ["forward_shadow", "Forward Shadow"],
];

const serviceStates = new Set<ServiceState>([
  "ONLINE", "CONNECTED", "DEGRADED", "DISCONNECTED", "RUNNING", "STARTING",
  "RECONNECTING", "STOPPED", "DISABLED", "PLANNED", "ERROR", "UNKNOWN",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asServiceState(value: unknown): ServiceState | undefined {
  return typeof value === "string" && serviceStates.has(value as ServiceState)
    ? value as ServiceState
    : undefined;
}

function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function normalizeControlResponse(value: unknown): SafeControlStatus | null {
  if (!isRecord(value) || !isRecord(value.status)) return null;
  const raw = value.status;
  const execution = isRecord(raw.execution) ? raw.execution : undefined;
  const strategy = isRecord(raw.strategy) ? raw.strategy : undefined;
  return {
    checked_at: typeof raw.checked_at === "string" ? raw.checked_at : undefined,
    control: asServiceState(raw.control),
    supervisor: asServiceState(raw.supervisor),
    api: asServiceState(raw.api),
    live: asServiceState(raw.live),
    mt5: asServiceState(raw.mt5),
    database: asServiceState(raw.database),
    telegram: asServiceState(raw.telegram),
    forward_shadow: asServiceState(raw.forward_shadow),
    execution: execution ? {
      demo_execution_enabled: asBoolean(execution.demo_execution_enabled),
      demo_kill_switch_armed: asBoolean(execution.demo_kill_switch_armed),
      real_money_execution: execution.real_money_execution === "DISABLED" ? "DISABLED" : undefined,
    } : undefined,
    strategy: strategy ? {
      pair_zone_state: typeof strategy.pair_zone_state === "string" ? strategy.pair_zone_state : undefined,
      current_direction: typeof strategy.current_direction === "string" ? strategy.current_direction : undefined,
      latest_canonical_signal_id: typeof strategy.latest_canonical_signal_id === "string" ? strategy.latest_canonical_signal_id : null,
      latest_canonical_direction: typeof strategy.latest_canonical_direction === "string" ? strategy.latest_canonical_direction : null,
      latest_canonical_signal_at: typeof strategy.latest_canonical_signal_at === "string" ? strategy.latest_canonical_signal_at : null,
      latest_forward_session_id: typeof strategy.latest_forward_session_id === "string" ? strategy.latest_forward_session_id : null,
    } : undefined,
  };
}

function operationId(): string {
  if (!globalThis.crypto?.randomUUID) throw new Error("Secure operation IDs unavailable");
  return globalThis.crypto.randomUUID();
}

export function OperatorControlPage() {
  const control = useApi<unknown>("/api/control/status", 5_000);
  const [pending, setPending] = useState<ControlCommand | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function run(command: ControlCommand, confirmation?: string) {
    const lifecycleCommand = command === "start" || command === "stop" || command === "restart";
    const demoCommand = command === "demo-on" || command === "demo-off";
    if ((lifecycleCommand && !lifecycleReady) || (demoCommand && !demoStateReady)) {
      setActionError("Verified control state is unavailable; operation was not sent.");
      return;
    }
    if (confirmation && !window.confirm(confirmation)) return;
    setPending(command);
    setActionError(null);
    setMessage(null);
    try {
      const response = await postJson<ControlResponse>(`/api/control/${command}`, {
        operation_id: operationId(),
      });
      if (!response || response.ok !== true || typeof response.message !== "string") {
        throw new Error("Control operation response was unavailable or malformed");
      }
      setMessage(response.message);
      control.refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Control operation failed");
    } finally {
      setPending(null);
    }
  }

  const status = normalizeControlResponse(control.data);
  const responseShapeError = control.data !== null && status === null
    ? "Control status response was unavailable or malformed"
    : null;
  const execution = status?.execution;
  const demoStateReady = Boolean(
    execution
    && typeof execution.demo_execution_enabled === "boolean"
    && typeof execution.demo_kill_switch_armed === "boolean"
    && execution.real_money_execution === "DISABLED",
  );
  const lifecycleReady = Boolean(
    status
    && status.control === "CONNECTED"
    && status.api !== undefined
    && status.live !== undefined
    && status.supervisor !== undefined,
  );
  const displayError = control.error ?? responseShapeError;
  return (
    <div className="page operator-control-page">
      <div className="page-heading">
        <div><span className="eyebrow">OPERATOR CONTROL</span><h1>Web Control Plane</h1><p>Private, authenticated lifecycle control through the persistent Control process.</p></div>
        {status?.checked_at && <time className="mono" dateTime={status.checked_at}>Checked {thaiDateTime(status.checked_at)}</time>}
      </div>

      <Panel title="System" kicker="VERIFIED CONTROL STATE">
        {control.loading && !status ? <DataState loading error={null} emptyTitle="Loading control state" emptyDetail="Waiting for the authenticated Control process." /> : status ? <div className="control-status-grid">
          {systemComponents.map(([key, label]) => {
            const state = status[key] as ServiceState | undefined;
            return <div className="control-status-row" key={String(key)}><span>{label}</span><StatusPill label={state ?? "UNAVAILABLE"} state={state ?? "UNKNOWN"} /></div>;
          })}
        </div> : <DataState loading={false} error={displayError} emptyTitle="Control state unavailable" emptyDetail="No state is inferred while the persistent Control process is unavailable or malformed." />}
      </Panel>

      <div className="control-grid">
        <Panel title="Lifecycle" kicker="SERIALIZED EXISTING SUPERVISOR OPERATIONS">
          <div className="control-actions">
            <button className="text-button control-primary" disabled={pending !== null || !lifecycleReady} onClick={() => void run("start")}>START SYSTEM</button>
            <button className="text-button control-danger" disabled={pending !== null || !lifecycleReady} onClick={() => void run("stop", "Stop Live monitoring? MT5 and broker positions will not be modified.")}>STOP SYSTEM</button>
            <button className="text-button control-danger" disabled={pending !== null || !lifecycleReady} onClick={() => void run("restart", "Restart Live monitoring through the accepted Supervisor lifecycle?")}>RESTART SYSTEM</button>
          </div>
        </Panel>
        <Panel title="Demo execution" kicker="SAFETY-SENSITIVE PERSISTENT GATE">
          <div className="control-actions demo-actions">
            <button className="text-button demo-arm" disabled={pending !== null || !demoStateReady || execution?.demo_execution_enabled !== true} onClick={() => void run("demo-on", "Arm Demo execution? Every existing Demo safety gate still remains required.")}>ARM DEMO</button>
            <button className="text-button demo-disarm" disabled={pending !== null || !demoStateReady} onClick={() => void run("demo-off")}>DISARM DEMO</button>
          </div>
          <div className="execution-summary">
            <span>DEMO_EXECUTION_ENABLED</span><strong>{execution?.demo_execution_enabled === undefined ? "UNAVAILABLE" : execution.demo_execution_enabled ? "true" : "false"}</strong>
            <span>DEMO KILL SWITCH</span><strong>{execution?.demo_kill_switch_armed === undefined ? "UNAVAILABLE" : execution.demo_kill_switch_armed ? "ARMED" : "DISARMED"}</strong>
            <span>REAL MONEY EXECUTION</span><strong>{execution?.real_money_execution ?? "UNAVAILABLE"}</strong>
          </div>
        </Panel>
      </div>

      <Panel title="Strategy and forward state" kicker="READ-ONLY OBSERVATION">
        <div className="control-strategy-grid">
          <div><span>Pair Zone</span><strong>{status?.strategy?.pair_zone_state ?? "UNAVAILABLE"}</strong></div>
          <div><span>Direction</span><strong>{status?.strategy?.current_direction ?? "UNAVAILABLE"}</strong></div>
          <div><span>Latest canonical signal</span><strong className="mono">{status?.strategy?.latest_canonical_signal_id ?? "—"}</strong></div>
          <div><span>Latest signal direction</span><strong>{status?.strategy?.latest_canonical_direction ?? "—"}</strong></div>
          <div><span>Forward session</span><strong className="mono">{status?.strategy?.latest_forward_session_id ?? "—"}</strong></div>
        </div>
      </Panel>

      {message && <div className="connection-banner" role="status">{message}</div>}
      {(actionError || control.error) && <div className="connection-banner" role="alert">{actionError ?? control.error}</div>}
      <div className="safety-note"><strong>REAL MONEY EXECUTION: DISABLED</strong><span>No Web action enables live-money execution, sends arbitrary commands, or bypasses the existing Demo safety gates.</span></div>
    </div>
  );
}
