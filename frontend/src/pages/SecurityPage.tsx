import {
  AlertTriangle,
  Ban,
  Check,
  Copy,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UserPlus,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { useCurrentAuthSession } from "../auth/authSessionContext";
import { ApiRequestError } from "../lib/api";
import {
  disableAdmin,
  inviteAdmin,
  loadAccountSessions,
  loadCurrentSession,
  loadSecurityAccounts,
  MalformedSecurityResponse,
  revokeAccountSession,
  revokeAllAccountSessions,
  type AdminInvitation,
  type SecurityAccount,
  type SecuritySession,
} from "../lib/securityApi";

type LoadStatus = "LOADING" | "AVAILABLE" | "EMPTY" | "UNAVAILABLE" | "ERROR";
type DialogState =
  | null
  | { kind: "invite" }
  | { kind: "disable"; account: SecurityAccount }
  | { kind: "revoke"; account: SecurityAccount; item: SecuritySession }
  | { kind: "revoke-all"; account: SecurityAccount };

function errorText(error: unknown): string {
  if (error instanceof MalformedSecurityResponse) {
    return "Security data was incomplete or malformed. No action was assumed.";
  }
  if (error instanceof ApiRequestError) {
    if (error.code === "FORBIDDEN" || error.status === 403) {
      return "OWNER authorization was denied. No change was made.";
    }
    if (error.code === "AUTH_REQUIRED" || error.status === 401) {
      return "Your application session is no longer available. Sign in again.";
    }
    if (error.code === "CSRF_REJECTED") {
      return "Security token was rejected. Refresh the page/session before trying again; this action was not retried.";
    }
    if (error.status === 409) return "That account cannot be created in its current state.";
    if (error.status === 422) return "Check the account login and Tailscale identity fields.";
    return "The security service could not complete the request.";
  }
  if (error instanceof TypeError || (error instanceof Error && /fetch|network/i.test(error.message))) {
    return "The security service is unavailable. The last verified data is preserved.";
  }
  return "The security request failed. No new state is being assumed.";
}

function timestamp(value: string): string {
  return new Date(value).toLocaleString();
}

function sessionLabel(item: SecuritySession): string {
  return `${item.id.slice(0, 8)}…`;
}

export function SecurityPage() {
  const authSession = useCurrentAuthSession();
  const isOwner = authSession?.authenticated === true && authSession.user?.role === "OWNER";
  const [accounts, setAccounts] = useState<SecurityAccount[] | null>(null);
  const accountsRef = useRef<SecurityAccount[] | null>(null);
  const [accountStatus, setAccountStatus] = useState<LoadStatus>("LOADING");
  const [accountError, setAccountError] = useState<string | null>(null);
  const [refreshingAccounts, setRefreshingAccounts] = useState(false);
  const [sessions, setSessions] = useState<SecuritySession[] | null>(null);
  const sessionsRef = useRef<SecuritySession[] | null>(null);
  const [sessionsForAccount, setSessionsForAccount] = useState<string | null>(null);
  const sessionsForAccountRef = useRef<string | null>(null);
  const [sessionStatus, setSessionStatus] = useState<LoadStatus>("LOADING");
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [refreshingSessions, setRefreshingSessions] = useState(false);
  const [selectedAccountId, setSelectedAccountId] = useState("");
  const [dialog, setDialog] = useState<DialogState>(null);
  const [invitation, setInvitation] = useState<AdminInvitation | null>(null);
  const [busy, setBusy] = useState(false);
  const [pageMessage, setPageMessage] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);

  const loadAccounts = useCallback(async (): Promise<SecurityAccount[] | null> => {
    setRefreshingAccounts(true);
    setAccountError(null);
    if (accountsRef.current === null) setAccountStatus("LOADING");
    try {
      const result = await loadSecurityAccounts();
      accountsRef.current = result;
      setAccounts(result);
      setAccountStatus(result.length ? "AVAILABLE" : "EMPTY");
      setSelectedAccountId((current) =>
        result.some((account) => account.id === current)
          ? current
          : result.find((account) => account.id === authSession?.user?.id)?.id ?? "",
      );
      return result;
    } catch (error) {
      const message = errorText(error);
      setAccountError(message);
      if (accountsRef.current === null) setAccountStatus(error instanceof TypeError ? "UNAVAILABLE" : "ERROR");
      return null;
    } finally {
      setRefreshingAccounts(false);
    }
  }, [authSession?.user?.id]);

  const loadSessions = useCallback(async (accountId: string): Promise<SecuritySession[] | null> => {
    if (!accountId) return null;
    setRefreshingSessions(true);
    setSessionError(null);
    if (sessionsForAccountRef.current !== accountId || sessionsRef.current === null) {
      setSessionStatus("LOADING");
    }
    try {
      const result = await loadAccountSessions(accountId);
      sessionsRef.current = result;
      sessionsForAccountRef.current = accountId;
      setSessions(result);
      setSessionsForAccount(accountId);
      setSessionStatus(result.length ? "AVAILABLE" : "EMPTY");
      return result;
    } catch (error) {
      const message = errorText(error);
      setSessionError(message);
      if (sessionsForAccountRef.current !== accountId || sessionsRef.current === null) {
        setSessionStatus(error instanceof TypeError ? "UNAVAILABLE" : "ERROR");
      }
      return null;
    } finally {
      setRefreshingSessions(false);
    }
  }, []);

  useEffect(() => {
    if (isOwner) void loadAccounts();
  }, [isOwner, loadAccounts]);

  useEffect(() => {
    if (isOwner && selectedAccountId) void loadSessions(selectedAccountId);
  }, [isOwner, selectedAccountId, loadSessions]);

  const selectedAccount = accounts?.find((account) => account.id === selectedAccountId) ?? null;
  const ownAccount = accounts?.find((account) => account.id === authSession?.user?.id) ?? null;
  const activeAccounts = accounts?.filter((account) => account.state === "ACTIVE").length;
  const pendingAccounts = accounts?.filter((account) => account.state === "PENDING").length;
  const disabledAccounts = accounts?.filter((account) => account.state === "DISABLED").length;
  const unrevokedSessions = useMemo(
    () => sessionsForAccount === selectedAccountId
      ? sessions?.filter((item) => !item.revoked).length ?? null
      : null,
    [selectedAccountId, sessions, sessionsForAccount],
  );

  function openDialog(next: DialogState) {
    setPageMessage(null);
    setPageError(null);
    setDialog(next);
  }

  function closeDialog() {
    if (busy) return;
    setDialog(null);
    setInvitation(null);
  }

  async function refreshSelectedAccount(accountId: string) {
    const [, freshSessions] = await Promise.all([loadAccounts(), loadSessions(accountId)]);
    return freshSessions;
  }

  async function createInvitation(login: string, identity: string) {
    setBusy(true);
    setPageError(null);
    setPageMessage(null);
    try {
      const issued = await inviteAdmin(login, identity);
      setInvitation(issued);
      setDialog(null);
      setPageMessage("ADMIN invitation issued. The enrollment secret is shown once below.");
      // Issuance is complete; the independent refresh must not block secret dismissal.
      setBusy(false);
      void loadAccounts();
    } catch (error) {
      setPageError(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function confirmDisable(account: SecurityAccount) {
    setBusy(true);
    setPageError(null);
    setPageMessage(null);
    try {
      await disableAdmin(account.id);
      setDialog(null);
      const freshAccounts = await loadAccounts();
      await loadSessions(account.id);
      if (freshAccounts?.find((item) => item.id === account.id)?.state === "DISABLED") {
        setPageMessage(`${account.login} is confirmed DISABLED in refreshed account data.`);
      } else {
        setPageError("Disable was acknowledged, but refreshed account state is unavailable or not DISABLED. Verify before proceeding.");
      }
    } catch (error) {
      setPageError(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function verifyCurrentSessionAfterRevocation(): Promise<boolean> {
    try {
      const current = await loadCurrentSession();
      if (!current.authenticated || current.user?.role !== "OWNER") {
        window.dispatchEvent(new Event("xauusd:auth-required"));
        return false;
      }
      return true;
    } catch (error) {
      setPageError(`Revocation was acknowledged, but current-session verification failed. ${errorText(error)}`);
      return false;
    }
  }

  async function confirmRevoke(account: SecurityAccount, item: SecuritySession) {
    setBusy(true);
    setPageError(null);
    setPageMessage(null);
    try {
      await revokeAccountSession(account.id, item.id);
      setDialog(null);
      if (!(await verifyCurrentSessionAfterRevocation())) return;
      const refreshed = await refreshSelectedAccount(account.id);
      if (!refreshed) {
        setPageError("Session revocation was acknowledged, but refreshed session data is unavailable. Existing data remains unchanged.");
      } else {
        setPageMessage("Session data refreshed from the server.");
      }
    } catch (error) {
      setPageError(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function confirmRevokeAll(account: SecurityAccount) {
    setBusy(true);
    setPageError(null);
    setPageMessage(null);
    try {
      const revokedCount = await revokeAllAccountSessions(account.id);
      setDialog(null);
      if (!(await verifyCurrentSessionAfterRevocation())) return;
      const refreshed = await refreshSelectedAccount(account.id);
      if (!refreshed) {
        setPageError("Revoke All was acknowledged, but refreshed session data is unavailable. Existing data remains unchanged.");
      } else {
        setPageMessage(`Server confirmed revocation of ${revokedCount} session(s); session data was refreshed.`);
      }
    } catch (error) {
      setPageError(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  if (!isOwner) {
    return (
      <div className="page security-page">
        <div className="page-heading"><div><h1>Security Administration</h1><p>OWNER access is required. No administration data was requested.</p></div></div>
        <section className="security-notice" role="alert"><ShieldCheck aria-hidden="true" /><div><strong>OWNER authorization required</strong><span>This area is not available to this application identity.</span></div></section>
      </div>
    );
  }

  return (
    <div className="page security-page">
      <div className="page-heading">
        <div><span className="eyebrow">OWNER SECURITY ADMINISTRATION</span><h1>Security</h1><p>Manage application accounts, invitations, and sessions. Backend authorization remains authoritative.</p></div>
        <div className="heading-state"><ShieldCheck size={15} aria-hidden="true" /><strong>OWNER</strong><span>verified session</span></div>
      </div>

      {pageError && <div className="security-message error" role="alert"><AlertTriangle size={16} />{pageError}</div>}
      {pageMessage && <div className="security-message success" role="status"><Check size={16} />{pageMessage}</div>}

      <section className="security-section" aria-labelledby="security-overview-heading">
        <div className="security-section-heading"><div><span className="eyebrow">SECURITY OVERVIEW</span><h2 id="security-overview-heading">Verified account posture</h2></div></div>
        <div className="security-overview-grid">
          <article className="security-overview-card"><span>Current account</span><strong>{authSession.user?.login ?? "UNAVAILABLE"}</strong><small>{authSession.user?.role ?? "UNKNOWN"} · {ownAccount?.bound_tailscale_identity ?? "Bound identity unavailable"}</small></article>
          <article className="security-overview-card"><span>ACTIVE accounts</span><strong>{activeAccounts ?? "UNKNOWN"}</strong><small>{accounts ? "Authoritative account list" : "Account data unavailable"}</small></article>
          <article className="security-overview-card"><span>PENDING accounts</span><strong>{pendingAccounts ?? "UNKNOWN"}</strong><small>{disabledAccounts === undefined ? "DISABLED count unavailable" : `${disabledAccounts} DISABLED`}</small></article>
          <article className="security-overview-card"><span>Unrevoked sessions</span><strong>{unrevokedSessions ?? "UNKNOWN"}</strong><small>{selectedAccount ? `Selected account: ${selectedAccount.login}` : "Select an account to load session data"}</small></article>
        </div>
        {accountStatus === "LOADING" && <InlineStatus>Loading authoritative account data…</InlineStatus>}
        {accountError && <InlineStatus alert>{accountError}</InlineStatus>}
      </section>

      <section className="security-section" aria-labelledby="security-accounts-heading">
        <div className="security-section-heading">
          <div><span className="eyebrow">ACCOUNTS</span><h2 id="security-accounts-heading">Application accounts</h2></div>
          <div className="security-actions">
            <button className="text-button" type="button" onClick={() => void loadAccounts()} disabled={refreshingAccounts}>
              <RefreshCw size={14} aria-hidden="true" />{refreshingAccounts ? "REFRESHING…" : "REFRESH"}
            </button>
            <button className="text-button control-primary" type="button" onClick={() => openDialog({ kind: "invite" })} disabled={busy}>
              <UserPlus size={14} aria-hidden="true" />CREATE ADMIN INVITATION
            </button>
          </div>
        </div>
        <div className="security-account-list" aria-live="polite">
          {accountStatus === "LOADING" && <ResourceState status="LOADING" label="Loading verified accounts…" />}
          {accountStatus === "UNAVAILABLE" && <ResourceState status="UNAVAILABLE" label={accountError ?? "Account service unavailable."} />}
          {accountStatus === "ERROR" && <ResourceState status="ERROR" label={accountError ?? "Account response unavailable."} />}
          {accountStatus === "EMPTY" && <ResourceState status="EMPTY" label="No account records were returned." />}
          {accounts?.map((account) => (
            <AccountCard
              key={account.id}
              account={account}
              selected={selectedAccountId === account.id}
              busy={busy}
              onSelect={() => setSelectedAccountId(account.id)}
              onDisable={() => openDialog({ kind: "disable", account })}
            />
          ))}
        </div>
        <p className="security-limitation">The API provides created/updated times, but not an activation timestamp. Enrollment consumption/expiry status is not exposed as a read-only API.</p>
      </section>

      <section className="security-section" aria-labelledby="security-sessions-heading">
        <div className="security-section-heading"><div><span className="eyebrow">SESSIONS</span><h2 id="security-sessions-heading">Session administration</h2></div></div>
        <div className="security-session-toolbar">
          <label htmlFor="security-account-select">Account</label>
          <select id="security-account-select" value={selectedAccountId} onChange={(event) => setSelectedAccountId(event.target.value)} disabled={!accounts?.length || busy}>
            <option value="">Select an account</option>
            {accounts?.map((account) => <option key={account.id} value={account.id}>{account.login} · {account.role}</option>)}
          </select>
          <button className="text-button" type="button" onClick={() => void loadSessions(selectedAccountId)} disabled={!selectedAccountId || refreshingSessions || busy}>
            <RefreshCw size={14} aria-hidden="true" />{refreshingSessions ? "REFRESHING…" : "REFRESH SESSIONS"}
          </button>
          {selectedAccount && unrevokedSessions !== null && unrevokedSessions > 0 && (
            <button className="text-button security-danger" type="button" onClick={() => openDialog({ kind: "revoke-all", account: selectedAccount })} disabled={busy}>
              <Trash2 size={14} aria-hidden="true" />REVOKE ALL
            </button>
          )}
        </div>
        {!selectedAccountId && <ResourceState status={accountStatus === "LOADING" ? "LOADING" : "EMPTY"} label="Choose an account to inspect sessions." />}
        {selectedAccountId && sessionsForAccount !== selectedAccountId && sessionStatus === "LOADING" && <ResourceState status="LOADING" label="Loading verified sessions…" />}
        {selectedAccountId && sessionsForAccount === selectedAccountId && sessionStatus === "LOADING" && !sessions && <ResourceState status="LOADING" label="Loading verified sessions…" />}
        {sessionStatus === "UNAVAILABLE" && <ResourceState status="UNAVAILABLE" label={sessionError ?? "Session service unavailable."} />}
        {sessionStatus === "ERROR" && <ResourceState status="ERROR" label={sessionError ?? "Session response unavailable."} />}
        {sessionError && sessionStatus === "AVAILABLE" && <InlineStatus alert>{sessionError}</InlineStatus>}
        {sessionsForAccount === selectedAccountId && sessionStatus === "EMPTY" && <ResourceState status="EMPTY" label="The server returned no session records for this account." />}
        {sessionsForAccount === selectedAccountId && sessionStatus === "AVAILABLE" && sessions?.map((item) => (
          <SessionCard
            key={item.id}
            item={item}
            account={selectedAccount!}
            busy={busy}
            onRevoke={() => openDialog({ kind: "revoke", account: selectedAccount!, item })}
          />
        ))}
        <p className="security-limitation">The API does not identify which record is the current browser session or provide device metadata. No session is labeled “current” based on ordering or timestamps. After revocation, this page revalidates its own session and returns to sign-in if it was revoked.</p>
      </section>

      <section className="security-section" aria-labelledby="security-enrollment-heading">
        <div className="security-section-heading"><div><span className="eyebrow">ENROLLMENT / INVITATIONS</span><h2 id="security-enrollment-heading">Temporary ADMIN enrollment</h2></div><KeyRound size={18} aria-hidden="true" /></div>
        <p className="security-copy">Only ADMIN invitations can be issued here. Each enrollment secret is shown once and expires at the server-provided time. The API does not expose a separate invitation-status endpoint.</p>
        <button className="text-button control-primary" type="button" onClick={() => openDialog({ kind: "invite" })} disabled={busy}><UserPlus size={14} aria-hidden="true" />CREATE ADMIN INVITATION</button>
      </section>

      <section className="security-section security-audit-note" aria-labelledby="security-audit-heading">
        <div><span className="eyebrow">SECURITY OVERVIEW</span><h2 id="security-audit-heading">Audit history</h2></div>
        <p>No safe OWNER-only authentication-audit read API exists yet. Security audit records remain backend-only; this page does not query general runtime events or expose internal audit details.</p>
      </section>

      {dialog?.kind === "invite" && <InviteDialog busy={busy} onClose={closeDialog} onSubmit={(login, identity) => void createInvitation(login, identity)} />}
      {dialog?.kind === "disable" && <ConfirmDialog title="Disable ADMIN account?" busy={busy} onClose={closeDialog} onConfirm={() => void confirmDisable(dialog.account)} confirmLabel="DISABLE ACCOUNT" danger>
        <p>This will disable <strong>{dialog.account.login}</strong> and revoke its sessions and pending enrollment. OWNER accounts cannot be disabled.</p>
      </ConfirmDialog>}
      {dialog?.kind === "revoke" && <ConfirmDialog title="Revoke this session?" busy={busy} onClose={closeDialog} onConfirm={() => void confirmRevoke(dialog.account, dialog.item)} confirmLabel="REVOKE SESSION" danger>
        <p>Revoke session <code>{sessionLabel(dialog.item)}</code> for <strong>{dialog.account.login}</strong>?</p>
        <p>If this is the session operating this page, the page will return to sign-in after server verification.</p>
      </ConfirmDialog>}
      {dialog?.kind === "revoke-all" && <RevokeAllDialog account={dialog.account} busy={busy} onClose={closeDialog} onConfirm={() => void confirmRevokeAll(dialog.account)} />}
      {invitation && <InvitationDialog invitation={invitation} onClose={closeDialog} />}
    </div>
  );
}

function ResourceState({ status, label }: { status: LoadStatus; label: string }) {
  return <div className={`security-resource-state ${status.toLowerCase()}`} role={status === "ERROR" || status === "UNAVAILABLE" ? "alert" : "status"}>
    {status === "LOADING" ? <LoaderCircle size={16} aria-hidden="true" /> : <AlertTriangle size={16} aria-hidden="true" />}
    <span><strong>{status}</strong> · {label}</span>
  </div>;
}

function InlineStatus({ children, alert = false }: { children: string; alert?: boolean }) {
  return <p className="security-inline-status" role={alert ? "alert" : "status"}>{children}</p>;
}

function AccountCard({
  account,
  selected,
  busy,
  onSelect,
  onDisable,
}: {
  account: SecurityAccount;
  selected: boolean;
  busy: boolean;
  onSelect: () => void;
  onDisable: () => void;
}) {
  const canDisable = account.role === "ADMIN" && account.state !== "DISABLED";
  return (
    <article className={`security-account-card ${selected ? "selected" : ""}`}>
      <div className="security-account-main">
        <div className="security-account-title"><strong>{account.login}</strong><span className={`security-state ${account.state.toLowerCase()}`}>{account.state}</span></div>
        <dl className="security-metadata">
          <div><dt>Role</dt><dd>{account.role}</dd></div>
          <div><dt>Bound Tailscale identity</dt><dd>{account.bound_tailscale_identity}</dd></div>
          <div><dt>Created</dt><dd><time dateTime={account.created_at}>{timestamp(account.created_at)}</time></dd></div>
          <div><dt>Updated</dt><dd><time dateTime={account.updated_at}>{timestamp(account.updated_at)}</time></dd></div>
        </dl>
      </div>
      <div className="security-account-actions">
        <button className="text-button" type="button" aria-pressed={selected} onClick={onSelect}>VIEW SESSIONS</button>
        {canDisable && <button className="text-button security-danger" type="button" onClick={onDisable} disabled={busy}><Ban size={14} aria-hidden="true" />DISABLE</button>}
      </div>
    </article>
  );
}

function SessionCard({
  item,
  account,
  busy,
  onRevoke,
}: {
  item: SecuritySession;
  account: SecurityAccount;
  busy: boolean;
  onRevoke: () => void;
}) {
  return (
    <article className="security-session-card">
      <div className="security-session-heading"><strong>Session {sessionLabel(item)}</strong><span className={`security-state ${item.revoked ? "disabled" : "active"}`}>{item.revoked ? "REVOKED" : "NOT REVOKED"}</span></div>
      <dl className="security-metadata">
        <div><dt>Account</dt><dd>{account.login}</dd></div>
        <div><dt>Created</dt><dd><time dateTime={item.created_at}>{timestamp(item.created_at)}</time></dd></div>
        <div><dt>Last seen</dt><dd><time dateTime={item.last_seen_at}>{timestamp(item.last_seen_at)}</time></dd></div>
        <div><dt>Idle expiry</dt><dd><time dateTime={item.idle_expires_at}>{timestamp(item.idle_expires_at)}</time></dd></div>
        <div><dt>Absolute expiry</dt><dd><time dateTime={item.absolute_expires_at}>{timestamp(item.absolute_expires_at)}</time></dd></div>
      </dl>
      {!item.revoked && <button className="text-button security-danger" type="button" onClick={onRevoke} disabled={busy}>REVOKE SESSION</button>}
    </article>
  );
}

function DialogFrame({
  title,
  busy,
  onClose,
  onKeyDown,
  children,
}: {
  title: string;
  busy: boolean;
  onClose: () => void;
  onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  children: ReactNode;
}) {
  return (
    <div className="security-dialog-backdrop">
      <div className="security-dialog" role="dialog" aria-modal="true" aria-labelledby="security-dialog-title" onKeyDown={onKeyDown}>
        <div className="security-dialog-heading"><h2 id="security-dialog-title">{title}</h2><button className="icon-button" type="button" aria-label="Close dialog" onClick={onClose} disabled={busy}><X size={16} /></button></div>
        <div className="security-dialog-body">{children}</div>
      </div>
    </div>
  );
}

function DialogActions({
  busy,
  onClose,
  confirmLabel,
  onConfirm,
  danger = false,
  disabled = false,
}: {
  busy: boolean;
  onClose: () => void;
  confirmLabel: string;
  onConfirm: () => void;
  danger?: boolean;
  disabled?: boolean;
}) {
  return <div className="security-dialog-actions">
    <button className="text-button" type="button" onClick={onClose} disabled={busy}>CANCEL</button>
    <button className={`text-button ${danger ? "security-danger" : "control-primary"}`} type="button" onClick={onConfirm} disabled={busy || disabled}>{busy ? "WORKING…" : confirmLabel}</button>
  </div>;
}

function InviteDialog({
  busy,
  onClose,
  onSubmit,
}: {
  busy: boolean;
  onClose: () => void;
  onSubmit: (login: string, identity: string) => void;
}) {
  const [login, setLogin] = useState("");
  const [identity, setIdentity] = useState("");
  const [error, setError] = useState<string | null>(null);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedLogin = login.trim();
    const normalizedIdentity = identity.trim();
    if (!normalizedLogin || !normalizedIdentity) {
      setError("Enter both the ADMIN login and its bound Tailscale identity.");
      return;
    }
    setError(null);
    onSubmit(normalizedLogin, normalizedIdentity);
  }
  return (
    <DialogFrame title="Create ADMIN invitation" busy={busy} onClose={onClose} onKeyDown={(event) => { if (event.key === "Escape" && !busy) onClose(); }}>
      <form className="security-form" onSubmit={submit}>
        <p>The role is fixed to ADMIN. OWNER creation and role selection are not available here.</p>
        <label htmlFor="invite-login">ADMIN login</label><input id="invite-login" type="email" autoComplete="off" maxLength={254} required value={login} onChange={(event) => setLogin(event.target.value)} />
        <label htmlFor="invite-identity">Bound Tailscale identity</label><input id="invite-identity" autoComplete="off" maxLength={254} required value={identity} onChange={(event) => setIdentity(event.target.value)} />
        {error && <p role="alert">{error}</p>}
        <div className="security-dialog-actions"><button className="text-button" type="button" onClick={onClose} disabled={busy}>CANCEL</button><button className="text-button control-primary" type="submit" disabled={busy}>{busy ? "ISSUING…" : "ISSUE INVITATION"}</button></div>
      </form>
    </DialogFrame>
  );
}

function ConfirmDialog({
  title,
  busy,
  onClose,
  onConfirm,
  confirmLabel,
  danger = false,
  children,
}: {
  title: string;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
  confirmLabel: string;
  danger?: boolean;
  children: ReactNode;
}) {
  return (
    <DialogFrame title={title} busy={busy} onClose={onClose} onKeyDown={(event) => { if (event.key === "Escape" && !busy) onClose(); }}>
      {children}
      <DialogActions busy={busy} onClose={onClose} onConfirm={onConfirm} confirmLabel={confirmLabel} danger={danger} />
    </DialogFrame>
  );
}

function RevokeAllDialog({
  account,
  busy,
  onClose,
  onConfirm,
}: {
  account: SecurityAccount;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const [confirmation, setConfirmation] = useState("");
  const matches = confirmation.trim().toLocaleLowerCase() === account.login.toLocaleLowerCase();
  return (
    <DialogFrame title="Revoke all sessions?" busy={busy} onClose={onClose} onKeyDown={(event) => { if (event.key === "Escape" && !busy) onClose(); }}>
      <p>This will invalidate <strong>all sessions</strong> for <strong>{account.login}</strong>, including the session operating this page if applicable. You may need to sign in again.</p>
      <label className="security-confirm-label" htmlFor="revoke-all-confirm">Type the account login to confirm: <strong>{account.login}</strong></label>
      <input id="revoke-all-confirm" autoComplete="off" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
      <DialogActions busy={busy} onClose={onClose} onConfirm={onConfirm} confirmLabel="REVOKE ALL SESSIONS" danger disabled={!matches} />
    </DialogFrame>
  );
}

function InvitationDialog({ invitation, onClose }: { invitation: AdminInvitation; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  const expired = Date.now() >= Date.parse(invitation.expires_at);
  async function copySecret() {
    setCopyError(null);
    try {
      await navigator.clipboard.writeText(invitation.enrollment_secret);
      setCopied(true);
    } catch {
      setCopyError("Clipboard access is unavailable. Select and copy the secret manually.");
    }
  }
  return (
    <DialogFrame title="One-time enrollment secret" busy={false} onClose={onClose} onKeyDown={(event) => { if (event.key === "Escape") onClose(); }}>
      <p className="security-secret-warning">THIS SECRET WILL NOT BE SHOWN AGAIN</p>
      <p>ADMIN invitation for <strong>{invitation.account.login}</strong>. This secret is temporary; share it only with the bound identity over an approved channel.</p>
      <div className="security-expiry"><span>EXPIRES</span><strong className={expired ? "expired" : ""}>{expired ? "EXPIRED" : timestamp(invitation.expires_at)}</strong></div>
      {expired ? <p role="alert">This enrollment secret has expired and must not be used.</p> : <code className="security-secret" aria-label="One-time enrollment secret">{invitation.enrollment_secret}</code>}
      {copyError && <p role="alert">{copyError}</p>}
      <div className="security-dialog-actions">
        {!expired && <button className="text-button" type="button" onClick={() => void copySecret()}><Copy size={14} aria-hidden="true" />{copied ? "COPIED BY OPERATOR" : "COPY SECRET"}</button>}
        <button className="text-button control-primary" type="button" onClick={onClose}>CLOSE AND DISCARD SECRET</button>
      </div>
    </DialogFrame>
  );
}
