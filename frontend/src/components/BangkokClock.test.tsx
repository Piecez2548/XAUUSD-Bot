/* @vitest-environment jsdom */

import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BangkokClock } from "./BangkokClock";

describe("BangkokClock isolation", () => {
  afterEach(() => vi.useRealTimers());

  it("ticks locally and cleans its interval without rerendering its parent", () => {
    vi.useFakeTimers();
    let parentRenders = 0;
    function Parent() {
      parentRenders += 1;
      return <BangkokClock />;
    }
    const { unmount } = render(<Parent />);
    expect(parentRenders).toBe(1);
    expect(vi.getTimerCount()).toBe(1);
    act(() => vi.advanceTimersByTime(3_000));
    expect(parentRenders).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
