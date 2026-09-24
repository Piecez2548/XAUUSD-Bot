/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthGate } from "./AuthGate";
import { getAuthSession, loginRequest, LoginRequestError, type AuthSession } from "../lib/api";

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
    expect(await screen.findByRole("heading", { name: "Welcome back" })).toBeTruthy();
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
    vi.mocked(loginRequest).mockRejectedValue(
      new LoginRequestError("invalid_credentials"),
    );
    render(<AuthGate><div>Private dashboard</div></AuthGate>);
    fireEvent.change(await screen.findByLabelText("Login"), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "incorrect" } });
    fireEvent.click(screen.getByRole("button", { name: "SIGN IN" }));
    expect(await screen.findByRole("alert").then((el) => el.textContent)).toBe("Invalid login or password");
    expect(screen.queryByText("account-specific backend detail")).toBeNull();
  });

  it("distinguishes unavailable service and malformed login responses safely", async () => {
    vi.mocked(getAuthSession).mockResolvedValue({ authenticated: false });
    vi.mocked(loginRequest).mockRejectedValueOnce(new LoginRequestError("unavailable"));
    render(<AuthGate><div>Private dashboard</div></AuthGate>);
    fireEvent.change(await screen.findByLabelText("Login"), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "passphrase" } });
    fireEvent.click(screen.getByRole("button", { name: "SIGN IN" }));
    expect(await screen.findByRole("alert").then((el) => el.textContent)).toContain(
      "temporarily unavailable",
    );
    expect(screen.queryByText(/account|owner|identity/i)).toBeNull();

    cleanup();
    vi.mocked(getAuthSession).mockResolvedValue({ authenticated: false });
    vi.mocked(loginRequest).mockRejectedValueOnce(new LoginRequestError("malformed_response"));
    render(<AuthGate><div>Private dashboard</div></AuthGate>);
    fireEvent.change(await screen.findByLabelText("Login"), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "passphrase" } });
    fireEvent.click(screen.getByRole("button", { name: "SIGN IN" }));
    expect(await screen.findByRole("alert").then((el) => el.textContent)).toContain(
      "unexpected response",
    );
  });

  it("toggles password visibility accessibly without persisting credentials", async () => {
    vi.mocked(getAuthSession).mockResolvedValue({ authenticated: false });
    render(<AuthGate><div>Private dashboard</div></AuthGate>);
    const password = await screen.findByLabelText("Password") as HTMLInputElement;
    fireEvent.change(password, { target: { value: "short-lived passphrase" } });
    expect(password.type).toBe("password");
    fireEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(password.type).toBe("text");
    expect(screen.getByRole("button", { name: "Hide password" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Hide password" }));
    expect(password.type).toBe("password");
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("prevents duplicate form submissions while sign-in is pending", async () => {
    vi.mocked(getAuthSession)
      .mockResolvedValueOnce({ authenticated: false })
      .mockResolvedValueOnce(session);
    let resolveLogin!: () => void;
    vi.mocked(loginRequest).mockReturnValueOnce(new Promise<void>((resolve) => { resolveLogin = resolve; }));
    render(<AuthGate><div>Private dashboard</div></AuthGate>);
    fireEvent.change(await screen.findByLabelText("Login"), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "passphrase" } });
    const form = screen.getByLabelText("Login").closest("form");
    expect(form).not.toBeNull();
    fireEvent.submit(form!);
    expect(screen.getByRole("button", { name: "SIGNING IN…" }).hasAttribute("disabled")).toBe(true);
    fireEvent.submit(form!);
    expect(loginRequest).toHaveBeenCalledTimes(1);
    resolveLogin();
    expect(await screen.findByText("Private dashboard")).toBeTruthy();
  });

  it("returns to Login after the backend signals an expired/revoked session", async () => {
    vi.mocked(getAuthSession).mockResolvedValue(session);
    render(<AuthGate><div>Private dashboard</div></AuthGate>);
    expect(await screen.findByText("Private dashboard")).toBeTruthy();
    window.dispatchEvent(new Event("xauusd:auth-required"));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Welcome back" })).toBeTruthy());
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
