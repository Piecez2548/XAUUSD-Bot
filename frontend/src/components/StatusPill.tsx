import type { ServiceState } from "../types";

const healthy = new Set<ServiceState>(["ONLINE", "CONNECTED"]);
const warning = new Set<ServiceState>(["DEGRADED", "UNKNOWN", "PLANNED"]);

export function StatusPill({ label, state }: { label?: string; state: ServiceState }) {
  const tone = healthy.has(state)
    ? "good"
    : warning.has(state)
      ? "warn"
      : state === "DISABLED"
        ? "muted"
        : "bad";
  return (
    <span className={`status-pill ${tone}`} title={`${label ?? "Status"}: ${state}`}>
      <span className="status-dot" aria-hidden="true" />
      {label && <span className="status-label">{label}</span>}
      <strong>{state}</strong>
    </span>
  );
}
