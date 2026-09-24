import { afterEach, describe, expect, it, vi } from "vitest";

import { getJson, postJson } from "./api";
import {
  inviteAdmin,
  loadAccountSessions,
  loadSecurityAccounts,
  MalformedSecurityResponse,
} from "./securityApi";

vi.mock("./api", () => ({
  deleteJson: vi.fn(),
  getAuthSession: vi.fn(),
  getJson: vi.fn(),
  postJson: vi.fn(),
}));

const validAccount = {
  id: "admin-1",
  login: "admin@example.test",
  role: "ADMIN",
  state: "PENDING",
  bound_tailscale_identity: "admin@tailnet.test",
  created_at: "2026-09-24T10:00:00Z",
  updated_at: "2026-09-24T10:00:00Z",
};

describe("security API response validation", () => {
  afterEach(() => vi.resetAllMocks());

  it("rejects malformed account lists instead of interpreting them as empty", async () => {
    vi.mocked(getJson).mockResolvedValue({ accounts: [{ ...validAccount, state: "UNKNOWN" }] });
    await expect(loadSecurityAccounts()).rejects.toBeInstanceOf(MalformedSecurityResponse);
  });

  it("rejects account/session envelopes that contain secret or unreviewed fields", async () => {
    vi.mocked(getJson).mockResolvedValueOnce({
      accounts: [{ ...validAccount, password_hash: "must-not-enter-frontend-state" }],
    });
    await expect(loadSecurityAccounts()).rejects.toBeInstanceOf(MalformedSecurityResponse);

    vi.mocked(getJson).mockResolvedValueOnce({
      sessions: [{
        id: "session-1",
        created_at: "2026-09-24T10:00:00Z",
        last_seen_at: "2026-09-24T10:00:00Z",
        idle_expires_at: "2026-09-24T11:00:00Z",
        absolute_expires_at: "2026-09-25T10:00:00Z",
        revoked: false,
        session_token: "must-not-enter-frontend-state",
      }],
    });
    await expect(loadAccountSessions("admin-1")).rejects.toBeInstanceOf(MalformedSecurityResponse);
  });

  it("rejects malformed session records instead of fabricating session state", async () => {
    vi.mocked(getJson).mockResolvedValue({ sessions: [{ id: "s1", revoked: "false" }] });
    await expect(loadAccountSessions("admin-1")).rejects.toBeInstanceOf(MalformedSecurityResponse);
  });

  it("rejects malformed invitation envelopes and enforces the fixed ADMIN role", async () => {
    vi.mocked(postJson).mockResolvedValue({
      account: { ...validAccount, role: "OWNER" },
      enrollment_secret: "A".repeat(43),
      expires_at: "2099-01-01T00:00:00Z",
      secret_disclosure: "one_time",
    });
    await expect(inviteAdmin("owner@example.test", "owner@tailnet.test"))
      .rejects.toBeInstanceOf(MalformedSecurityResponse);
    expect(postJson).toHaveBeenCalledWith("/api/auth/admin/accounts", {
      login: "owner@example.test",
      role: "ADMIN",
      bound_tailscale_identity: "owner@tailnet.test",
    });
  });
});
