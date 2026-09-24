import {
  useCallback,
  useEffect,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";

import {
  getAuthSession,
  isAuthSession,
  loginRequest,
  PRIVATE_DASHBOARD,
  type AuthSession,
} from "../lib/api";
import { AuthSessionContext } from "./authSessionContext";

export function AuthGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(null);
  const [loading, setLoading] = useState(PRIVATE_DASHBOARD);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!PRIVATE_DASHBOARD) return;
    setLoading(true);
    setError(null);
    try {
      const result = await getAuthSession();
      if (!isAuthSession(result)) throw new Error("Invalid session response");
      setSession(result.authenticated === true ? result : { authenticated: false });
    } catch (reason) {
      setSession(null);
      setError(reason instanceof Error ? reason.message : "Authentication service unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!PRIVATE_DASHBOARD) return;
    const expire = () => {
      setSession({ authenticated: false });
      setError(null);
    };
    window.addEventListener("xauusd:auth-required", expire);
    void refresh();
    return () => window.removeEventListener("xauusd:auth-required", expire);
  }, [refresh]);

  if (!PRIVATE_DASHBOARD) return <>{children}</>;
  if (loading) return <main className="auth-shell"><section className="auth-card" aria-live="polite">Checking secure session…</section></main>;
  if (error) return <main className="auth-shell"><section className="auth-card"><h1>Private dashboard unavailable</h1><p role="alert">{error}</p><button className="text-button" onClick={() => void refresh()}>RETRY</button></section></main>;
  if (!session?.authenticated) return <LoginPage onLogin={async (login, password) => {
    await loginRequest(login, password);
    await refresh();
  }} />;
  return (
    <AuthSessionContext.Provider value={session}>
      {children}
    </AuthSessionContext.Provider>
  );
}

function LoginPage({ onLogin }: { onLogin: (login: string, password: string) => Promise<void> }) {
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onLogin(login, password);
      setPassword("");
    } catch {
      setError("Invalid login or password");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-card">
        <span className="eyebrow">PRIVATE TAILNET ACCESS</span>
        <h1>Sign in</h1>
        <p>Use an approved XAUUSD dashboard account.</p>
        <form onSubmit={(event) => void submit(event)}>
          <label htmlFor="auth-login">Login</label>
          <input id="auth-login" autoComplete="username" value={login} onChange={(event) => setLogin(event.target.value)} required maxLength={254} />
          <label htmlFor="auth-password">Password</label>
          <input id="auth-password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required maxLength={1024} />
          {error && <p role="alert">{error}</p>}
          <button className="text-button control-primary" type="submit" disabled={busy}>{busy ? "SIGNING IN…" : "SIGN IN"}</button>
        </form>
        <p className="auth-note">Account enrollment is administered locally. There is no public registration.</p>
      </section>
    </main>
  );
}
