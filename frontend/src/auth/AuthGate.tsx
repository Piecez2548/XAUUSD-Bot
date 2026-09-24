import {
  useRef,
  useCallback,
  useEffect,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { Eye, EyeOff, ShieldCheck } from "lucide-react";

import {
  getAuthSession,
  isAuthSession,
  loginRequest,
  LoginRequestError,
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
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      await onLogin(login, password);
      setPassword("");
    } catch (reason) {
      if (reason instanceof LoginRequestError) {
        const messages = {
          invalid_credentials: "Invalid login or password",
          unavailable: "Sign-in is temporarily unavailable. Check your connection and try again.",
          malformed_response: "The authentication service returned an unexpected response. Please try again.",
        };
        setError(messages[reason.kind]);
      } else {
        setError("Sign-in is temporarily unavailable. Check your connection and try again.");
      }
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-card" aria-labelledby="auth-title">
        <header className="auth-brand">
          <span className="auth-brand-mark" aria-hidden="true"><ShieldCheck size={19} /></span>
          <span className="auth-brand-copy">
            <strong>XAUUSD AI TRADER</strong>
            <span>Private Trading Observatory</span>
          </span>
          <span className="auth-private-badge">PRIVATE</span>
        </header>
        <div className="auth-heading">
          <span className="auth-eyebrow">AUTHORIZED OPERATOR ACCESS</span>
          <h1 id="auth-title">Welcome back</h1>
          <p>Sign in to your private trading observatory.</p>
        </div>
        <form className="auth-form" onSubmit={(event) => void submit(event)} aria-busy={busy}>
          <div className="auth-field">
            <label htmlFor="auth-login">Login</label>
            <input
              id="auth-login"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              value={login}
              onChange={(event) => { setLogin(event.target.value); setError(null); }}
              required
              maxLength={254}
              disabled={busy}
              aria-describedby={error ? "auth-error" : undefined}
            />
          </div>
          <div className="auth-field">
            <label htmlFor="auth-password">Password</label>
            <div className="auth-password-field">
              <input
                id="auth-password"
                type={showPassword ? "text" : "password"}
                autoComplete="current-password"
                value={password}
                onChange={(event) => { setPassword(event.target.value); setError(null); }}
                required
                maxLength={1024}
                disabled={busy}
                aria-describedby={error ? "auth-error" : undefined}
              />
              <button
                className="auth-password-toggle"
                type="button"
                aria-label={showPassword ? "Hide password" : "Show password"}
                aria-pressed={showPassword}
                onClick={() => setShowPassword((visible) => !visible)}
                disabled={busy}
              >
                {showPassword ? <EyeOff size={16} aria-hidden="true" /> : <Eye size={16} aria-hidden="true" />}
              </button>
            </div>
          </div>
          {error && <p className="auth-error" id="auth-error" role="alert" aria-live="polite">{error}</p>}
          <button className="auth-submit" type="submit" disabled={busy}>
            {busy && <span className="auth-spinner" aria-hidden="true" />}
            {busy ? "SIGNING IN…" : "SIGN IN"}
          </button>
        </form>
        <footer className="auth-footer" aria-label="Access security">
          <span>Private</span><i aria-hidden="true" />
          <span>Tailnet protected</span><i aria-hidden="true" />
          <span>Authorized access only</span>
        </footer>
      </section>
    </main>
  );
}
