/* @vitest-environment jsdom */

import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppErrorBoundary } from "./AppErrorBoundary";

function BrokenView(): ReactElement {
  throw new Error("secret stack must not be rendered");
}

describe("AppErrorBoundary", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows a safe Thai recovery state without exposing the error", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<AppErrorBoundary><BrokenView /></AppErrorBoundary>);
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("แดชบอร์ดแสดงผลไม่ได้ชั่วคราว")).toBeTruthy();
    expect(screen.getByText("REAL-MONEY EXECUTION: DISABLED")).toBeTruthy();
    expect(screen.queryByText("secret stack must not be rendered")).toBeNull();
  });
});
