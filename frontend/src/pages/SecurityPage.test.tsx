/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthSessionContext } from "../auth/authSessionContext";
import type { AuthSession } from "../lib/api";
import {
  disableAdmin,
  inviteAdmin,
  loadAccountSessions,
  loadCurrentSession,
  loadSecurityAccounts,
  MalformedSecurityResponse,
  revokeAccountSession,
  revokeAllAccountSessions,
  type SecurityAccount,
} from "../lib/securityApi";
import { SecurityPage } from "./SecurityPage";

vi.mock("../lib/securityApi", () => ({
  disableAdmin: vi.fn(),
  inviteAdmin: vi.fn(),
  loadAccountSessions: vi.fn(),
  loadCurrentSession: vi.fn(),
  loadSecurityAccounts: vi.fn(),
  MalformedSecurityResponse: class MalformedSecurityResponse extends Error {},
  revokeAccountSession: vi.fn(),
  revokeAllAccountSessions: vi.fn(),
}));

const ownerSession = {
  authenticated: true,
  user: { id: "owner-1", login: "owner@example.test", role: "OWNER", state: "ACTIVE" },
} satisfies AuthSession;

const admin: Awaited<ReturnType<typeof loadSecurityAccounts>>[number] = {
  id: "admin-1",
  login: "admin@example.test",
  role: "ADMIN",
  state: "ACTIVE",
  bound_tailscale_identity: "admin@tailnet.test",
  created_at: "2026-09-20T10:00:00Z",
  updated_at: "2026-09-21T10:00:00Z",
};

const owner = {
  ...admin,
  id: "owner-1",
  login: "owner@example.test",
  role: "OWNER" as const,
  bound_tailscale_identity: "owner@tailnet.test",
};

const activeSession: Awaited<ReturnType<typeof loadAccountSessions>>[number] = {
  id: "session-abcdef12",
  created_at: "2026-09-23T10:00:00Z",
  last_seen_at: "2026-09-24T10:00:00Z",
  idle_expires_at: "2026-09-24T11:00:00Z",
  absolute_expires_at: "2026-09-25T10:00:00Z",
  revoked: false,
};

function renderPage(session: AuthSession | null = ownerSession) {
  return render(
    <AuthSessionContext.Provider value={session}>
      <SecurityPage />
    </AuthSessionContext.Provider>,
  );
}

describe("OWNER Security administration UI", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
    vi.mocked(loadSecurityAccounts).mockResolvedValue([owner, admin]);
    vi.mocked(loadAccountSessions).mockResolvedValue([activeSession]);
    vi.mocked(loadCurrentSession).mockResolvedValue(ownerSession);
  });

  afterEach(() => cleanup());

  it("renders safe account/session metadata and never exposes secret material", async () => {
    renderPage();
    expect(await screen.findByText("admin@example.test")).toBeTruthy();
    expect(await screen.findByText("admin@tailnet.test")).toBeTruthy();
    expect(await screen.findByText("Session session-…")).toBeTruthy();
    expect(screen.queryByText(/session[_ -]?token|token[_ -]?hash|csrf[_ -]?token/i)).toBeNull();
    expect(screen.queryByText("owner@tailnet.test", { selector: "code" })).toBeNull();
  });

  it("does not request or present OWNER data to an ADMIN", () => {
    renderPage({
      authenticated: true,
      user: { id: "admin-1", login: admin.login, role: "ADMIN", state: "ACTIVE" },
    });
    expect(screen.getByRole("alert").textContent).toContain("OWNER authorization required");
    expect(loadSecurityAccounts).not.toHaveBeenCalled();
    expect(loadAccountSessions).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /create admin invitation/i })).toBeNull();
  });

  it("issues a fixed ADMIN invitation, shows its secret once, and discards it without persistence", async () => {
    const secret = "A".repeat(43);
    vi.mocked(inviteAdmin).mockResolvedValue({
      account: { ...admin, state: "PENDING" },
      enrollment_secret: secret,
      expires_at: "2099-01-01T00:00:00Z",
    });
    renderPage();
    fireEvent.click(screen.getAllByRole("button", { name: /create admin invitation/i })[0]);
    expect(screen.queryByLabelText(/role/i)).toBeNull();
    fireEvent.change(screen.getByLabelText("ADMIN login"), { target: { value: "new-admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Bound Tailscale identity"), { target: { value: "new@tailnet.test" } });
    fireEvent.click(screen.getByRole("button", { name: "ISSUE INVITATION" }));
    expect(await screen.findByText(secret)).toBeTruthy();
    expect(inviteAdmin).toHaveBeenCalledWith("new-admin@example.test", "new@tailnet.test");
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(window.location.href).not.toContain(secret);
    expect(window.indexedDB).toBeUndefined();
    fireEvent.click(screen.getByRole("button", { name: "CLOSE AND DISCARD SECRET" }));
    await waitFor(() => expect(screen.queryByText(secret)).toBeNull());
  });

  it("allows immediate dismissal while the post-issuance account refresh is pending and never resurrects the secret", async () => {
    const secret = "B".repeat(43);
    let resolveRefresh!: (accounts: SecurityAccount[]) => void;
    const pendingRefresh = new Promise<SecurityAccount[]>((resolve) => {
      resolveRefresh = resolve;
    });
    vi.mocked(loadSecurityAccounts)
      .mockResolvedValueOnce([owner, admin])
      .mockImplementationOnce(() => pendingRefresh);
    vi.mocked(inviteAdmin).mockResolvedValue({
      account: { ...admin, state: "PENDING" },
      enrollment_secret: secret,
      expires_at: "2099-01-01T00:00:00Z",
    });
    renderPage();
    expect(await screen.findByText("admin@example.test")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: /create admin invitation/i })[0]);
    fireEvent.change(screen.getByLabelText("ADMIN login"), { target: { value: "new-admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Bound Tailscale identity"), { target: { value: "new@tailnet.test" } });
    fireEvent.click(screen.getByRole("button", { name: "ISSUE INVITATION" }));

    expect(await screen.findByText(secret)).toBeTruthy();
    await waitFor(() => expect(loadSecurityAccounts).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "CLOSE AND DISCARD SECRET" }));
    expect(screen.queryByText(secret)).toBeNull();
    expect(screen.queryByRole("button", { name: "COPY SECRET" })).toBeNull();

    resolveRefresh([owner, admin]);
    await waitFor(() => expect(screen.getByText("admin@example.test")).toBeTruthy());
    expect(screen.queryByText(secret)).toBeNull();
  });

  it("keeps issuance successful and the secret dismissible when its later account refresh fails", async () => {
    const secret = "D".repeat(43);
    vi.mocked(loadSecurityAccounts)
      .mockResolvedValueOnce([owner, admin])
      .mockRejectedValueOnce(new Error("refresh unavailable"));
    vi.mocked(inviteAdmin).mockResolvedValue({
      account: { ...admin, state: "PENDING" },
      enrollment_secret: secret,
      expires_at: "2099-01-01T00:00:00Z",
    });
    renderPage();
    expect(await screen.findByText("admin@example.test")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: /create admin invitation/i })[0]);
    fireEvent.change(screen.getByLabelText("ADMIN login"), { target: { value: "new-admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Bound Tailscale identity"), { target: { value: "new@tailnet.test" } });
    fireEvent.click(screen.getByRole("button", { name: "ISSUE INVITATION" }));

    expect(await screen.findByText(secret)).toBeTruthy();
    expect(await screen.findByText("ADMIN invitation issued. The enrollment secret is shown once below.")).toBeTruthy();
    expect(await screen.findByText(/security request failed/i)).toBeTruthy();
    expect(inviteAdmin).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "CLOSE AND DISCARD SECRET" }));
    expect(screen.queryByText(secret)).toBeNull();
    expect(inviteAdmin).toHaveBeenCalledTimes(1);
  });

  it("blocks duplicate invitation submission while the issuance mutation itself is pending", async () => {
    const secret = "E".repeat(43);
    let resolveInvite!: (value: Awaited<ReturnType<typeof inviteAdmin>>) => void;
    const pendingInvite = new Promise<Awaited<ReturnType<typeof inviteAdmin>>>((resolve) => {
      resolveInvite = resolve;
    });
    vi.mocked(inviteAdmin).mockReturnValue(pendingInvite);
    renderPage();
    expect(await screen.findByText("admin@example.test")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: /create admin invitation/i })[0]);
    fireEvent.change(screen.getByLabelText("ADMIN login"), { target: { value: "new-admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Bound Tailscale identity"), { target: { value: "new@tailnet.test" } });
    const submit = screen.getByRole("button", { name: "ISSUE INVITATION" });
    fireEvent.click(submit);
    await waitFor(() => expect(submit).toHaveProperty("disabled", true));
    fireEvent.click(submit);
    expect(inviteAdmin).toHaveBeenCalledTimes(1);

    resolveInvite({
      account: { ...admin, state: "PENDING" },
      enrollment_secret: secret,
      expires_at: "2099-01-01T00:00:00Z",
    });
    expect(await screen.findByText(secret)).toBeTruthy();
    expect(inviteAdmin).toHaveBeenCalledTimes(1);
  });

  it("drops the one-time secret when the Security page unmounts", async () => {
    const secret = "F".repeat(43);
    vi.mocked(inviteAdmin).mockResolvedValue({
      account: { ...admin, state: "PENDING" },
      enrollment_secret: secret,
      expires_at: "2099-01-01T00:00:00Z",
    });
    const view = renderPage();
    expect(await screen.findByText("admin@example.test")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: /create admin invitation/i })[0]);
    fireEvent.change(screen.getByLabelText("ADMIN login"), { target: { value: "new-admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Bound Tailscale identity"), { target: { value: "new@tailnet.test" } });
    fireEvent.click(screen.getByRole("button", { name: "ISSUE INVITATION" }));
    expect(await screen.findByText(secret)).toBeTruthy();
    view.unmount();
    expect(screen.queryByText(secret)).toBeNull();
  });

  it("requires disable confirmation and only reports success after authoritative refresh", async () => {
    vi.mocked(disableAdmin).mockResolvedValue(undefined);
    vi.mocked(loadSecurityAccounts)
      .mockResolvedValueOnce([owner, admin])
      .mockRejectedValueOnce(new Error("offline"));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "DISABLE" }));
    expect(disableAdmin).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog").textContent).toContain("admin@example.test");
    fireEvent.click(screen.getByRole("button", { name: "DISABLE ACCOUNT" }));
    await waitFor(() => expect(disableAdmin).toHaveBeenCalledWith("admin-1"));
    expect((await screen.findAllByRole("alert")).length).toBeGreaterThan(0);
    expect(screen.queryByText(/confirmed DISABLED/i)).toBeNull();
  });

  it("confirms disable only when refreshed account data says DISABLED and refreshes target sessions", async () => {
    vi.mocked(disableAdmin).mockResolvedValue(undefined);
    vi.mocked(loadSecurityAccounts)
      .mockResolvedValueOnce([owner, admin])
      .mockResolvedValueOnce([owner, { ...admin, state: "DISABLED" }]);
    vi.mocked(loadAccountSessions).mockResolvedValue([]);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "DISABLE" }));
    fireEvent.click(screen.getByRole("button", { name: "DISABLE ACCOUNT" }));
    expect(await screen.findByRole("status")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain("admin@example.test is confirmed DISABLED");
    expect(loadAccountSessions).toHaveBeenCalledWith("admin-1");
  });

  it("does not offer disabling the OWNER", async () => {
    vi.mocked(loadSecurityAccounts).mockResolvedValue([owner]);
    renderPage();
    await screen.findByText("owner@tailnet.test");
    expect(screen.queryByRole("button", { name: "DISABLE" })).toBeNull();
  });

  it("requires explicit confirmation before revoking a session and refreshes the result", async () => {
    vi.mocked(revokeAccountSession).mockResolvedValue(true);
    vi.mocked(loadAccountSessions)
      .mockResolvedValueOnce([activeSession])
      .mockResolvedValueOnce([{ ...activeSession, revoked: true }]);
    renderPage();
    fireEvent.click((await screen.findAllByRole("button", { name: "REVOKE SESSION" }))[0]);
    expect(revokeAccountSession).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole("button", { name: "REVOKE SESSION" })[1]);
    await waitFor(() => expect(revokeAccountSession).toHaveBeenCalledWith("owner-1", activeSession.id));
    expect(await screen.findByText("REVOKED")).toBeTruthy();
  });

  it("requires typing the target login before Revoke All", async () => {
    vi.mocked(revokeAllAccountSessions).mockResolvedValue(1);
    vi.mocked(loadAccountSessions)
      .mockResolvedValueOnce([activeSession])
      .mockResolvedValueOnce([{ ...activeSession, revoked: true }]);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "REVOKE ALL" }));
    const confirm = screen.getByRole("button", { name: "REVOKE ALL SESSIONS" });
    expect(confirm).toHaveProperty("disabled", true);
    expect(revokeAllAccountSessions).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/type the account login/i), { target: { value: owner.login } });
    expect(confirm).toHaveProperty("disabled", false);
    fireEvent.click(confirm);
    await waitFor(() => expect(revokeAllAccountSessions).toHaveBeenCalledWith("owner-1"));
    expect(await screen.findByText(/server confirmed revocation of 1 session/i)).toBeTruthy();
  });

  it("returns to unauthenticated flow when revocation invalidates the current session", async () => {
    vi.mocked(revokeAccountSession).mockResolvedValue(true);
    vi.mocked(loadCurrentSession).mockResolvedValue({ authenticated: false });
    const authRequired = vi.fn();
    window.addEventListener("xauusd:auth-required", authRequired);
    renderPage();
    fireEvent.click((await screen.findAllByRole("button", { name: "REVOKE SESSION" }))[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "REVOKE SESSION" })[1]);
    await waitFor(() => expect(authRequired).toHaveBeenCalled());
    window.removeEventListener("xauusd:auth-required", authRequired);
    expect(loadSecurityAccounts).toHaveBeenCalledTimes(1);
  });

  it("fails closed on malformed account/session/invitation data", async () => {
    vi.mocked(loadSecurityAccounts).mockRejectedValueOnce(new MalformedSecurityResponse());
    renderPage();
    expect((await screen.findAllByText(/security data was incomplete or malformed/i)).length).toBeGreaterThan(0);
    expect(screen.getAllByText("UNKNOWN").length).toBeGreaterThan(0);

    cleanup();
    vi.resetAllMocks();
    vi.mocked(loadSecurityAccounts).mockResolvedValue([owner]);
    vi.mocked(loadAccountSessions).mockRejectedValue(new MalformedSecurityResponse());
    renderPage();
    expect(await screen.findByText(/security data was incomplete or malformed/i)).toBeTruthy();
    expect(screen.getAllByText("UNKNOWN").length).toBeGreaterThan(0);

    cleanup();
    vi.resetAllMocks();
    vi.mocked(loadSecurityAccounts).mockResolvedValue([owner]);
    vi.mocked(loadAccountSessions).mockResolvedValue([]);
    vi.mocked(inviteAdmin).mockRejectedValue(new MalformedSecurityResponse());
    renderPage();
    fireEvent.click(screen.getAllByRole("button", { name: /create admin invitation/i })[0]);
    fireEvent.change(screen.getByLabelText("ADMIN login"), { target: { value: "bad@example.test" } });
    fireEvent.change(screen.getByLabelText("Bound Tailscale identity"), { target: { value: "bad@tailnet.test" } });
    fireEvent.click(screen.getByRole("button", { name: "ISSUE INVITATION" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText(/one-time enrollment secret/i)).toBeNull();
    expect(screen.queryByText("undefined")).toBeNull();
  });
});
