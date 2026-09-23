/* @vitest-environment jsdom */

import { describe, expect, it } from "vitest";

import { dashboardRoutes } from "./routes";

describe("dashboard deployment routes", () => {
  it("keeps the direct forward-validation route mapped to Forward Validation", () => {
    const children = dashboardRoutes[0].children ?? [];
    expect(children.some((route) => route.path === "/forward-validation")).toBe(true);
  });
});
