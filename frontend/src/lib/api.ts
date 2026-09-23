/**
 * The public Vercel build is static-only. A separate private build may be
 * served by FastAPI behind tailnet-only Tailscale Serve using same-origin
 * relative API paths; neither mode embeds a credential in the bundle.
 */
export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").trim();
export const PRIVATE_DASHBOARD =
  (import.meta.env.VITE_PRIVATE_DASHBOARD ?? "").trim().toLowerCase() === "true";
export const productionApiConfigured =
  !import.meta.env.PROD || API_BASE.length > 0 || PRIVATE_DASHBOARD;

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  if (import.meta.env.PROD && !API_BASE && !PRIVATE_DASHBOARD) {
    throw new Error("BACKEND_NOT_CONFIGURED");
  }
  // A future Access-protected HTTPS gateway authenticates the browser with
  // its own session cookie.  The cookie is not a VITE secret and is only
  // sent to the explicitly configured API origin by the browser.
  const response = await fetch(apiUrl(path), {
    credentials: API_BASE ? "include" : "same-origin",
    signal,
  });
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

export function websocketUrl(path = "/ws/live"): string {
  const configured = (import.meta.env.VITE_WS_URL as string | undefined)?.trim();
  if (configured) return configured;
  // Phase 2.6 deliberately ships HTTPS polling first.  Do not infer a WSS
  // endpoint from VITE_API_BASE_URL until a separately authenticated WSS edge
  // has been provisioned and explicitly configured.
  if (import.meta.env.PROD) return "";
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const base = API_BASE ? new URL(API_BASE, window.location.origin) : null;
  return base ? `${base.protocol === "https:" ? "wss:" : "ws:"}//${base.host}${path}` : `${protocol}//${window.location.host}${path}`;
}
