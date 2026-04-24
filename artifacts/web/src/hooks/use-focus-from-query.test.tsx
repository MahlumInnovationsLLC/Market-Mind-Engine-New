import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

import { useFocusFromQuery } from "./use-focus-from-query";

// jsdom doesn't ship with CSS.escape; the hook leans on it to
// build a safe attribute selector. Polyfill the minimal subset we
// need (escape `"` and `\\`).
beforeAll(() => {
  const g = globalThis as unknown as { CSS?: { escape?: (s: string) => string } };
  if (!g.CSS) g.CSS = {};
  if (typeof g.CSS!.escape !== "function") {
    g.CSS!.escape = (s: string) => s.replace(/(["\\])/g, "\\$1");
  }
});

function Probe() {
  useFocusFromQuery();
  return null;
}

function renderAt(path: string) {
  const hook = memoryLocation({ path, record: true });
  const utils = render(
    <Router hook={hook.hook}>
      <Probe />
    </Router>,
  );
  return { ...utils, history: hook.history };
}

describe("useFocusFromQuery", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("scrolls and flashes the matching row, then strips focus", () => {
    const row = document.createElement("div");
    row.dataset.focusId = "cycle-42";
    row.scrollIntoView = vi.fn();
    document.body.appendChild(row);

    const { history } = renderAt("/trading?focus=cycle-42&keep=1");

    act(() => {
      vi.advanceTimersByTime(75);
    });

    expect(row.scrollIntoView).toHaveBeenCalledTimes(1);
    expect(row.dataset.focusFlash).toBe("true");
    // Focus param removed but other params survive.
    expect(history[history.length - 1]).toBe("/trading?keep=1");

    act(() => {
      vi.advanceTimersByTime(2600);
    });
    expect(row.dataset.focusFlash).toBeUndefined();
  });

  it("polls until the row mounts (lists render asynchronously)", () => {
    const { history } = renderAt("/trading?focus=trade-7");

    // First few attempts find nothing.
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(history[history.length - 1]).toBe("/trading?focus=trade-7");

    // Now the row mounts (e.g. data finished loading).
    const row = document.createElement("tr");
    row.dataset.focusId = "trade-7";
    row.scrollIntoView = vi.fn();
    document.body.appendChild(row);

    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(row.scrollIntoView).toHaveBeenCalledTimes(1);
    expect(row.dataset.focusFlash).toBe("true");
    expect(history[history.length - 1]).toBe("/trading");
  });

  it("does nothing when no focus param is present", () => {
    const row = document.createElement("div");
    row.dataset.focusId = "anything";
    row.scrollIntoView = vi.fn();
    document.body.appendChild(row);

    renderAt("/trading");

    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(row.scrollIntoView).not.toHaveBeenCalled();
    expect(row.dataset.focusFlash).toBeUndefined();
  });

  it("safely escapes special characters in the focus identifier", () => {
    const row = document.createElement("div");
    row.dataset.focusId = "user-clerk_123|abc";
    row.scrollIntoView = vi.fn();
    document.body.appendChild(row);

    renderAt(`/system?focus=${encodeURIComponent("user-clerk_123|abc")}`);

    act(() => {
      vi.advanceTimersByTime(75);
    });
    expect(row.scrollIntoView).toHaveBeenCalledTimes(1);
  });
});
