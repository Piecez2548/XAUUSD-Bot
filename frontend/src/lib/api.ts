/**
 * Vercel hosts only this read-only UI. The trading runtime remains on the
 * Windows host, so production must be explicitly pointed at a reachable API.
 * An unset production URL is intentionally an offline state, never localhost.
 */
export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").trim();
export const productionApiConfigured = !import.meta.env.PROD || API_BASE.length > 0;

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  if (import.meta.env.PROD && !API_BASE) {
    throw new Error("BACKEND_NOT_CONFIGURED");
  }
  const response = await fetch(`${API_BASE}${path}`, { signal });
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

export function websocketUrl(path = "/ws/live"): string {
  const configured = import.meta.env.VITE_WS_URL as string | undefined;
  if (configured) return configured;
  if (import.meta.env.PROD && !API_BASE) return "";
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const base = API_BASE ? new URL(API_BASE, window.location.origin) : null;
  return base ? `${base.protocol === "https:" ? "wss:" : "ws:"}//${base.host}${path}` : `${protocol}//${window.location.host}${path}`;
}
