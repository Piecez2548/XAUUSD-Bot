/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthGate } from "./AuthGate";
import { getAuthSession, loginRequest, type AuthSession } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  PRIVATE_DASHBOARD: true,
  getAuthSession: vi.fn(),
  loginRequest: vi.fn(),
  logoutRequest: vi.fn(),
}));

const session = {
  authenticated: true,
  user: { id: "user-1", login: "operator@example.test", role: "ADMIN", state: "ACTIVE" },
  session: {
    idle_expires_at: "2026-09-24T12:00:00Z",
    absolute_expires_at: "2026-09-25T00:00:00Z",
  },
} satisfies AuthSession;

describe("private dashboard authentication gate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => cleanup());

  it("shows Login instead of private routes without an app session", async () => {
    vi.mocked(getAuthSession).mockResolvedValue({ authenticated: false });
    render(<AuthGate><div>Private dashboard</div></AuthGate>);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
    expect(screen.queryByText("Private dashboard")).toBeNull();
    expect(screen.queryByText(/register/i)).toBeNull();
  });

  it("enters the app after login and stores no auth token in browser storage", async () => {
    vi.mocked(getAuthSession)
      .mockResolvedValueOnce({ authenticated: false })
      .mockResolvedValueOnce(session);
    vi.mocked(loginRequest).mockResolvedValue(undefined);
    render(<AuthGate><div>Private dashboard</div></AuthGate>);
    fireEvent.change(await screen.findByLabelText("Login"), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret passphrase" } });
    fireEvent.click(screen.getByRole("button", { name: "SIGN IN" }));
    expect(await screen.findByText("Private dashboard")).toBeTruthy();
    expect(loginRequest).toHaveBeenCalledWith("operator", "secret passphrase");
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("shows a generic invalid-login message", async () => {
    vi.mocked(getAuthSession).mockResolvedValue({ authenticated: false });
    vi.mocked(loginRequest).mockRejectedValue(new Error("account-specific backend detail"));
    render(<AuthGate><div>Private dashboard</div></AuthGate>);
    fireEvent.change(await screen.findByLabelText("Login"), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "incorrect" } });
    fireEvent.click(screen.getByRole("button", { name: "SIGN IN" }));
    expect(await screen.findByRole("alert").then((el) => el.textContent)).toBe("Invalid login or password");
    expect(screen.queryByText("account-specific backend detail")).toBeNull();
  });

  it("returns to Login after the backend signals an expired/revoked session", async () => {
    vi.mocked(getAuthSession).mockResolvedValue(session);
    render(<AuthGate><div>Private dashboard</div></AuthGate>);
    expect(await screen.findByText("Private dashboard")).toBeTruthy();
    window.dispatchEvent(new Event("xauusd:auth-required"));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Sign in" })).toBeTruthy());
    expect(screen.queryByText("Private dashboard")).toBeNull();
  });

  it("fails closed on malformed successful session responses", async () => {
    vi.mocked(getAuthSession).mockResolvedValue({
      authenticated: "yes",
    } as unknown as AuthSession);
    render(<AuthGate><div>Private dashboard</div></AuthGate>);
    expect(await screen.findByRole("heading", { name: "Private dashboard unavailable" })).toBeTruthy();
    expect(screen.queryByText("Private dashboard")).toBeNull();
  });
});
