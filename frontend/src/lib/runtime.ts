import type { HealthResponse, ServiceState } from "../types";

export const OPERATOR_TIMEZONE = "Asia/Bangkok";

export type BackendState = "CONNECTED" | "STALE" | "OFFLINE" | "UNKNOWN";

export function backendState(
  health: HealthResponse | null,
  error: string | null,
  now = Date.now(),
  staleAfterMs = 30_000,
): BackendState {
  if (error) return "OFFLINE";
  if (!health?.checked_at) return "UNKNOWN";
  const checked = Date.parse(health.checked_at);
  if (Number.isNaN(checked)) return "UNKNOWN";
  return now - checked > staleAfterMs ? "STALE" : "CONNECTED";
}

export function workerState(services: Record<string, ServiceState> | undefined, key: string): ServiceState {
  return services?.[key] ?? "UNKNOWN";
}

export function sampleCheckpoint(signals: number): string {
  if (signals >= 200) return "200+";
  if (signals >= 100) return "100 / 200+";
  if (signals >= 50) return "50 / 100 / 200+";
  if (signals >= 30) return "30 / 50 / 100 / 200+";
  return "0 / 30 / 50 / 100 / 200+";
}

export function thaiDateTime(value: string | null | undefined): string {
  if (!value) return "ยังไม่มีข้อมูล";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "ยังไม่มีข้อมูล";
  return new Intl.DateTimeFormat("th-TH", {
    timeZone: OPERATOR_TIMEZONE,
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function bangkokClock(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: OPERATOR_TIMEZONE,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function stateLabel(state: string | null | undefined): string {
  const value = String(state ?? "UNKNOWN").toUpperCase();
  const labels: Record<string, string> = {
    CONNECTED: "🟢 ปกติ",
    ONLINE: "🟢 ออนไลน์",
    RUNNING: "🟢 กำลังทำงาน",
    DEGRADED: "🟡 ทำงานได้ แต่มีบางส่วนผิดปกติ",
    STALE: "🟡 ข้อมูลเก่า",
    OFFLINE: "🔴 ออฟไลน์",
    UNKNOWN: "⚪ ยังไม่ทราบสถานะ",
    ERROR: "🔴 เกิดข้อผิดพลาด",
    DISABLED: "⚫ ปิดใช้งาน",
    STOPPED: "⚫ หยุดทำงาน",
  };
  return `${labels[value] ?? labels.UNKNOWN} (${value})`;
}
