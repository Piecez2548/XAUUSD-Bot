/**
 * The public Vercel build is static-only. A separate private build may be
 * served by FastAPI behind tailnet-only Tailscale Serve using same-origin
 * relative API paths; neither mode embeds a credential in the bundle.
 */
export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").trim();
export const PRIVATE_DASHBOARD =
  (import.meta.env.VITE_PRIVATE_DASHBOARD ?? "").trim().toLowerCase() === "true";
const CSRF_TOKEN_PATTERN = /^[A-Za-z0-9_-]{86}$/;
let csrfTokenInMemory: string | null = null;

export interface AuthSession {
  authenticated: boolean;
  user?: { id: string; login: string; role: "OWNER" | "ADMIN"; state: "ACTIVE" };
  session?: { idle_expires_at: string; absolute_expires_at: string };
}

export function isAuthSession(value: unknown): value is AuthSession {
  if (typeof value !== "object" || value === null || !("authenticated" in value)) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  if (candidate.authenticated === false) return true;
  if (candidate.authenticated !== true) return false;
  if (typeof candidate.user !== "object" || candidate.user === null) return false;
  if (typeof candidate.session !== "object" || candidate.session === null) return false;
  const user = candidate.user as Record<string, unknown>;
  const session = candidate.session as Record<string, unknown>;
  return (
    typeof user.id === "string" && user.id.length > 0 &&
    typeof user.login === "string" && user.login.length > 0 &&
    (user.role === "OWNER" || user.role === "ADMIN") &&
    user.state === "ACTIVE" &&
    typeof session.idle_expires_at === "string" &&
    !Number.isNaN(Date.parse(session.idle_expires_at)) &&
    typeof session.absolute_expires_at === "string" &&
    !Number.isNaN(Date.parse(session.absolute_expires_at))
  );
}

function notifyUnauthorized(response: Response): void {
  if (response.status === 401 && PRIVATE_DASHBOARD) {
    csrfTokenInMemory = null;
    window.dispatchEvent(new Event("xauusd:auth-required"));
  }
}

async function acquireCsrfToken(): Promise<string> {
  if (!PRIVATE_DASHBOARD) throw new Error("CSRF token is available only in the private dashboard");
  if (csrfTokenInMemory) return csrfTokenInMemory;
  const response = await fetch("/api/auth/csrf", { credentials: "same-origin" });
  notifyUnauthorized(response);
  if (!response.ok) throw new Error("Unable to obtain mutation security token");
  const payload: unknown = await response.json();
  if (
    typeof payload !== "object" || payload === null ||
    !("csrf_token" in payload) || typeof payload.csrf_token !== "string" ||
    !CSRF_TOKEN_PATTERN.test(payload.csrf_token)
  ) {
    throw new Error("Invalid mutation security token response");
  }
  csrfTokenInMemory = payload.csrf_token;
  return csrfTokenInMemory;
}

async function rejectCsrfIfNeeded(response: Response): Promise<void> {
  if (response.status !== 403) return;
  const payload: unknown = await response.clone().json().catch(() => null);
  if (typeof payload === "object" && payload !== null &&
      "code" in payload && payload.code === "CSRF_REJECTED") {
    csrfTokenInMemory = null;
    window.dispatchEvent(new Event("xauusd:csrf-rejected"));
    throw new Error("CSRF_REJECTED");
  }
}

export async function getAuthSession(): Promise<AuthSession> {
  csrfTokenInMemory = null;
  const response = await fetch("/api/auth/session", { credentials: "same-origin" });
  if (!response.ok) {
    notifyUnauthorized(response);
    throw new Error(`Session check failed (${response.status})`);
  }
  const payload: unknown = await response.json();
  if (!isAuthSession(payload)) throw new Error("Invalid session response");
  if (payload.authenticated && PRIVATE_DASHBOARD) await acquireCsrfToken();
  return payload;
}

export async function loginRequest(login: string, password: string): Promise<void> {
  csrfTokenInMemory = null;
  const response = await fetch("/api/auth/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ login, password }),
  });
  if (!response.ok) throw new Error("Invalid login or password");
  if (PRIVATE_DASHBOARD) await acquireCsrfToken();
}

export async function logoutRequest(): Promise<void> {
  const csrfToken = PRIVATE_DASHBOARD ? await acquireCsrfToken() : null;
  const response = await fetch("/api/auth/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
    },
    body: "{}",
  });
  notifyUnauthorized(response);
  await rejectCsrfIfNeeded(response);
  if (!response.ok) {
    throw new Error("Sign out failed; server session state is unknown");
  }
  csrfTokenInMemory = null;
  window.dispatchEvent(new Event("xauusd:auth-required"));
}

export interface ApiRuntimeConfig {
  apiBase: string;
  privateDashboard: boolean;
  production: boolean;
}

export function isApiConfigured(config: ApiRuntimeConfig): boolean {
  return !config.production || config.apiBase.length > 0 || config.privateDashboard;
}

export const productionApiConfigured = isApiConfigured({
  apiBase: API_BASE,
  privateDashboard: PRIVATE_DASHBOARD,
  production: import.meta.env.PROD,
});

export function apiUrl(path: string, apiBase = API_BASE): string {
  return `${apiBase}${path}`;
}

export async function requestJson<T>(
  path: string,
  config: ApiRuntimeConfig,
  signal?: AbortSignal,
  fetcher: typeof fetch = fetch,
): Promise<T> {
  if (!isApiConfigured(config)) {
    throw new Error("BACKEND_NOT_CONFIGURED");
  }
  // A future Access-protected HTTPS gateway authenticates the browser with
  // its own session cookie.  The cookie is not a VITE secret and is only
  // sent to the explicitly configured API origin by the browser.
  const response = await fetcher(apiUrl(path, config.apiBase), {
    credentials: config.apiBase ? "include" : "same-origin",
    signal,
  });
  notifyUnauthorized(response);
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return requestJson(
    path,
    {
      apiBase: API_BASE,
      privateDashboard: PRIVATE_DASHBOARD,
      production: import.meta.env.PROD,
    },
    signal,
  );
}

export async function postJson<T>(
  path: string,
  body: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<T> {
  const config = {
    apiBase: API_BASE,
    privateDashboard: PRIVATE_DASHBOARD,
    production: import.meta.env.PROD,
  };
  if (!isApiConfigured(config)) {
    throw new Error("BACKEND_NOT_CONFIGURED");
  }
  const response = await fetcherForApi(path, config, body, signal);
  notifyUnauthorized(response);
  await rejectCsrfIfNeeded(response);
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

async function fetcherForApi(
  path: string,
  config: ApiRuntimeConfig,
  body: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<Response> {
  const csrfToken = PRIVATE_DASHBOARD ? await acquireCsrfToken() : null;
  return fetch(apiUrl(path, config.apiBase), {
    method: "POST",
    credentials: config.apiBase ? "include" : "same-origin",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
    },
    body: JSON.stringify(body),
    signal,
  });
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
