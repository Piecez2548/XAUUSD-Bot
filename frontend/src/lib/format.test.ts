import { describe, expect, it } from "vitest";

import { number, percent, unavailable, utcTime } from "./format";

describe("honest formatting", () => {
  it("renders absent values as unavailable", () => {
    expect(number(null)).toBe(unavailable);
    expect(percent(undefined)).toBe(unavailable);
  });

  it("formats finite values and UTC timestamps", () => {
    expect(number(12.345, 2)).toBe("12.35");
    expect(percent(2)).toBe("2.00%");
    expect(utcTime("2026-09-21T12:34:56Z")).toContain("12:34:56");
  });
});
