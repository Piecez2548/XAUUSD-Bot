/* @vitest-environment jsdom */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EmptyState } from "./EmptyState";
import { StatusPill } from "./StatusPill";

describe("observatory truth states", () => {
  it("renders explicit planned and disabled states", () => {
    const { rerender } = render(<StatusPill label="AI" state="PLANNED" />);
    expect(screen.getByText("PLANNED").textContent).toBe("PLANNED");
    rerender(<StatusPill label="Execution" state="DISABLED" />);
    expect(screen.getByText("DISABLED").textContent).toBe("DISABLED");
  });

  it("renders an honest empty-state explanation", () => {
    render(<EmptyState title="No trade history" detail="No records exist." />);
    expect(screen.getByRole("status").textContent).toContain("No trade history");
    expect(screen.getByRole("status").textContent).toContain("No records exist.");
  });
});
