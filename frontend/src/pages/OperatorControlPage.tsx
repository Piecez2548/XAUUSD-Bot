import { useState } from "react";

import { Panel } from "../components/Panel";
import { StatusPill } from "../components/StatusPill";
import { DataState } from "../components/DataState";
import { useApi } from "../hooks/useApi";
import { postJson } from "../lib/api";
import { thaiDateTime } from "../lib/runtime";
import type { ControlResponse, ControlStatus, ServiceState } from "../types";

type ControlCommand = "start" | "stop" | "restart" | "demo-on" | "demo-off";

const systemComponents: Array<[keyof ControlStatus, string]> = [
  ["control", "Control"],
  ["supervisor", "Supervisor"],
  ["api", "API"],
  ["live", "Live Engine"],
  ["mt5", "MT5"],
  ["database", "Database"],
  ["telegram", "Telegram"],
  ["forward_shadow", "Forward Shadow"],
];

function operationId(): string {
  if (!globalThis.crypto?.randomUUID) throw new Error("Secure operation IDs unavailable");
  return globalThis.crypto.randomUUID();
}

export function OperatorControlPage() {
  const control = useApi<ControlStatus>("/api/control/status", 5_000);
  const [pending, setPending] = useState<ControlCommand | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function run(command: ControlCommand, confirmation?: string) {
    if (confirmation && !window.confirm(confirmation)) return;
    setPending(command);
    setActionError(null);
    setMessage(null);
    try {
      const response = await postJson<ControlResponse>(`/api/control/${command}`, {
        operation_id: operationId(),
      });
      setMessage(response.message);
      control.refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Control operation failed");
    } finally {
      setPending(null);
    }
  }

  const status = control.data;
  return (
    <div className="page operator-control-page">
      <div className="page-heading">
        <div><span className="eyebrow">OPERATOR CONTROL</span><h1>Web Control Plane</h1><p>Private, authenticated lifecycle control through the persistent Control process.</p></div>
        {status?.checked_at && <time className="mono" dateTime={status.checked_at}>Checked {thaiDateTime(status.checked_at)}</time>}
      </div>

      <Panel title="System" kicker="VERIFIED CONTROL STATE">
        {control.loading && !status ? <DataState loading error={null} emptyTitle="Loading control state" emptyDetail="Waiting for the authenticated Control process." /> : status ? <div className="control-status-grid">
          {systemComponents.map(([key, label]) => <div className="control-status-row" key={String(key)}><span>{label}</span><StatusPill label={String(status[key])} state={status[key] as ServiceState} /></div>)}
        </div> : <DataState loading={false} error={control.error} emptyTitle="Control state unavailable" emptyDetail="No state is inferred while the persistent Control process is unavailable." />}
      </Panel>

      <div className="control-grid">
        <Panel title="Lifecycle" kicker="SERIALIZED EXISTING SUPERVISOR OPERATIONS">
          <div className="control-actions">
            <button className="text-button control-primary" disabled={pending !== null} onClick={() => void run("start")}>START SYSTEM</button>
            <button className="text-button control-danger" disabled={pending !== null} onClick={() => void run("stop", "Stop API and Live monitoring? MT5 and broker positions will not be modified.")}>STOP SYSTEM</button>
            <button className="text-button control-danger" disabled={pending !== null} onClick={() => void run("restart", "Restart API and Live monitoring through the accepted Supervisor lifecycle?")}>RESTART SYSTEM</button>
          </div>
        </Panel>
        <Panel title="Demo execution" kicker="SAFETY-SENSITIVE PERSISTENT GATE">
          <div className="control-actions demo-actions">
            <button className="text-button demo-arm" disabled={pending !== null || status?.execution.demo_execution_enabled !== true} onClick={() => void run("demo-on", "Arm Demo execution? Every existing Demo safety gate still remains required.")}>ARM DEMO</button>
            <button className="text-button demo-disarm" disabled={pending !== null} onClick={() => void run("demo-off")}>DISARM DEMO</button>
          </div>
          <div className="execution-summary">
            <span>DEMO_EXECUTION_ENABLED</span><strong>{status?.execution.demo_execution_enabled ? "true" : "false"}</strong>
            <span>DEMO KILL SWITCH</span><strong>{status?.execution.demo_kill_switch_armed ? "ARMED" : "DISARMED"}</strong>
            <span>REAL MONEY EXECUTION</span><strong>DISABLED</strong>
          </div>
        </Panel>
      </div>

      <Panel title="Strategy and forward state" kicker="READ-ONLY OBSERVATION">
        <div className="control-strategy-grid">
          <div><span>Pair Zone</span><strong>{status?.strategy.pair_zone_state ?? "UNKNOWN"}</strong></div>
          <div><span>Direction</span><strong>{status?.strategy.current_direction ?? "UNKNOWN"}</strong></div>
          <div><span>Latest canonical signal</span><strong className="mono">{status?.strategy.latest_canonical_signal_id ?? "—"}</strong></div>
          <div><span>Latest signal direction</span><strong>{status?.strategy.latest_canonical_direction ?? "—"}</strong></div>
          <div><span>Forward session</span><strong className="mono">{status?.strategy.latest_forward_session_id ?? "—"}</strong></div>
        </div>
      </Panel>

      {message && <div className="connection-banner" role="status">{message}</div>}
      {(actionError || control.error) && <div className="connection-banner" role="alert">{actionError ?? control.error}</div>}
      <div className="safety-note"><strong>REAL MONEY EXECUTION: DISABLED</strong><span>No Web action enables live-money execution, sends arbitrary commands, or bypasses the existing Demo safety gates.</span></div>
    </div>
  );
}
