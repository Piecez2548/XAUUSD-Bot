import { deleteJson, getAuthSession, getJson, postJson, type AuthSession } from "./api";

export type AccountState = "PENDING" | "ACTIVE" | "DISABLED";
export interface SecurityAccount {
  id: string;
  login: string;
  role: "OWNER" | "ADMIN";
  state: AccountState;
  bound_tailscale_identity: string;
  created_at: string;
  updated_at: string;
}

export interface SecuritySession {
  id: string;
  created_at: string;
  last_seen_at: string;
  idle_expires_at: string;
  absolute_expires_at: string;
  revoked: boolean;
}

export interface AdminInvitation {
  account: SecurityAccount;
  enrollment_secret: string;
  expires_at: string;
}

export class MalformedSecurityResponse extends Error {
  constructor() {
    super("Security API returned an invalid response");
    this.name = "MalformedSecurityResponse";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === expected.length && expected.every((key) => key in value);
}

function isTimestamp(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && Number.isFinite(Date.parse(value));
}

export function isSecurityAccount(value: unknown): value is SecurityAccount {
  if (!isRecord(value) || !hasExactKeys(value, [
    "id", "login", "role", "state", "bound_tailscale_identity", "created_at", "updated_at",
  ])) return false;
  return (
    typeof value.id === "string" && value.id.length > 0 &&
    typeof value.login === "string" && value.login.length > 0 &&
    (value.role === "OWNER" || value.role === "ADMIN") &&
    (value.state === "PENDING" || value.state === "ACTIVE" || value.state === "DISABLED") &&
    typeof value.bound_tailscale_identity === "string" && value.bound_tailscale_identity.length > 0 &&
    isTimestamp(value.created_at) && isTimestamp(value.updated_at)
  );
}

export function isSecuritySession(value: unknown): value is SecuritySession {
  return (
    isRecord(value) &&
    hasExactKeys(value, [
      "id", "created_at", "last_seen_at", "idle_expires_at", "absolute_expires_at", "revoked",
    ]) &&
    typeof value.id === "string" && value.id.length > 0 &&
    isTimestamp(value.created_at) &&
    isTimestamp(value.last_seen_at) &&
    isTimestamp(value.idle_expires_at) &&
    isTimestamp(value.absolute_expires_at) &&
    typeof value.revoked === "boolean"
  );
}

function validAccountList(value: unknown): SecurityAccount[] {
  if (!isRecord(value) || !hasExactKeys(value, ["accounts"]) ||
      !Array.isArray(value.accounts) || !value.accounts.every(isSecurityAccount)) {
    throw new MalformedSecurityResponse();
  }
  return value.accounts;
}

function validSessionList(value: unknown): SecuritySession[] {
  if (!isRecord(value) || !hasExactKeys(value, ["sessions"]) ||
      !Array.isArray(value.sessions) || !value.sessions.every(isSecuritySession)) {
    throw new MalformedSecurityResponse();
  }
  return value.sessions;
}

export async function loadCurrentSession(): Promise<AuthSession> {
  return getAuthSession();
}

export async function loadSecurityAccounts(): Promise<SecurityAccount[]> {
  return validAccountList(await getJson<unknown>("/api/auth/admin/accounts"));
}

export async function inviteAdmin(
  login: string,
  boundTailscaleIdentity: string,
): Promise<AdminInvitation> {
  const value: unknown = await postJson<unknown>("/api/auth/admin/accounts", {
    login,
    role: "ADMIN",
    bound_tailscale_identity: boundTailscaleIdentity,
  });
  if (
    !isRecord(value) || !hasExactKeys(value, ["account", "enrollment_secret", "expires_at", "secret_disclosure"]) ||
    !isSecurityAccount(value.account) || value.account.role !== "ADMIN" ||
    value.account.state !== "PENDING" ||
    typeof value.enrollment_secret !== "string" ||
    !/^[A-Za-z0-9_-]{43}$/.test(value.enrollment_secret) ||
    !isTimestamp(value.expires_at) || value.secret_disclosure !== "one_time"
  ) {
    throw new MalformedSecurityResponse();
  }
  return {
    account: value.account,
    enrollment_secret: value.enrollment_secret,
    expires_at: value.expires_at,
  };
}

export async function disableAdmin(accountId: string): Promise<void> {
  const value: unknown = await postJson<unknown>(
    `/api/auth/admin/accounts/${encodeURIComponent(accountId)}/disable`,
    {},
  );
  if (!isRecord(value) || !hasExactKeys(value, ["disabled"]) || value.disabled !== true) {
    throw new MalformedSecurityResponse();
  }
}

export async function loadAccountSessions(accountId: string): Promise<SecuritySession[]> {
  return validSessionList(
    await getJson<unknown>(`/api/auth/admin/accounts/${encodeURIComponent(accountId)}/sessions`),
  );
}

export async function revokeAccountSession(accountId: string, sessionId: string): Promise<boolean> {
  const value: unknown = await deleteJson<unknown>(
    `/api/auth/admin/accounts/${encodeURIComponent(accountId)}/sessions/${encodeURIComponent(sessionId)}`,
  );
  if (!isRecord(value) || !hasExactKeys(value, ["revoked"]) || typeof value.revoked !== "boolean") {
    throw new MalformedSecurityResponse();
  }
  return value.revoked;
}

export async function revokeAllAccountSessions(accountId: string): Promise<number> {
  const value: unknown = await deleteJson<unknown>(
    `/api/auth/admin/accounts/${encodeURIComponent(accountId)}/sessions`,
  );
  if (!isRecord(value) || !hasExactKeys(value, ["revoked_count"]) || !Number.isInteger(value.revoked_count) ||
      (value.revoked_count as number) < 0) {
    throw new MalformedSecurityResponse();
  }
  return value.revoked_count as number;
}
