/**
 * Unit tests for the `useIsMobile` viewport hook.
 *
 * `useIsMobile` is the gate that decides which navigation surface
 * (sidebar vs. bottom tab bar) renders for every page in the
 * dashboard, plus a number of conditional layout tweaks. It listens
 * to `matchMedia(max-width: 767px)` and returns a boolean that
 * components can treat as "render the mobile UI".
 *
 * The hook touches three browser APIs — `window.matchMedia`,
 * `MediaQueryList.addEventListener`, and `window.innerWidth` — so we
 * stub all three in jsdom (which doesn't ship `matchMedia`) and
 * verify the subscribe / read / unsubscribe contract end-to-end.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useIsMobile } from "./use-mobile";

type Listener = (event: { matches: boolean }) => void;

interface MockMediaQueryList {
  matches: boolean;
  media: string;
  addEventListener: ReturnType<typeof vi.fn>;
  removeEventListener: ReturnType<typeof vi.fn>;
  // legacy methods on real MQL — included so the type satisfies the
  // hook's `mql.addEventListener("change", …)` call.
  addListener: ReturnType<typeof vi.fn>;
  removeListener: ReturnType<typeof vi.fn>;
  dispatchEvent: ReturnType<typeof vi.fn>;
  onchange: null;
}

let listeners: Listener[];
let mql: MockMediaQueryList;
let originalMatchMedia: typeof window.matchMedia | undefined;
let originalInnerWidth: number;

function setViewport(width: number) {
  // jsdom doesn't update `innerWidth` automatically, and the hook reads
  // it both on mount and on every `change` event — so the test must
  // mutate the property and then fire the listener manually.
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: width,
  });
}

beforeEach(() => {
  originalMatchMedia = window.matchMedia;
  originalInnerWidth = window.innerWidth;
  listeners = [];
  mql = {
    matches: false,
    media: "",
    addEventListener: vi.fn((_event: string, cb: Listener) => {
      listeners.push(cb);
    }),
    removeEventListener: vi.fn((_event: string, cb: Listener) => {
      listeners = listeners.filter((l) => l !== cb);
    }),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
    onchange: null,
  };
  window.matchMedia = vi.fn().mockReturnValue(mql) as unknown as typeof window.matchMedia;
});

afterEach(() => {
  if (originalMatchMedia) window.matchMedia = originalMatchMedia;
  setViewport(originalInnerWidth);
});

describe("useIsMobile", () => {
  it("returns true when the initial viewport is below the 768px breakpoint", () => {
    setViewport(500);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(true);
  });

  it("returns false when the initial viewport is at or above 768px", () => {
    setViewport(1024);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
  });

  it("treats exactly 768px as desktop (the breakpoint is exclusive)", () => {
    setViewport(768);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
  });

  it("subscribes to matchMedia with the correct query and cleans up on unmount", () => {
    setViewport(500);
    const { unmount } = renderHook(() => useIsMobile());
    expect(window.matchMedia).toHaveBeenCalledWith("(max-width: 767px)");
    expect(mql.addEventListener).toHaveBeenCalledWith(
      "change",
      expect.any(Function),
    );
    unmount();
    expect(mql.removeEventListener).toHaveBeenCalledWith(
      "change",
      expect.any(Function),
    );
  });

  it("flips from desktop to mobile when the viewport shrinks past the breakpoint", () => {
    setViewport(1200);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);

    act(() => {
      setViewport(400);
      for (const cb of listeners) cb({ matches: true });
    });
    expect(result.current).toBe(true);
  });

  it("flips from mobile to desktop when the viewport grows past the breakpoint", () => {
    setViewport(400);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(true);

    act(() => {
      setViewport(1280);
      for (const cb of listeners) cb({ matches: false });
    });
    expect(result.current).toBe(false);
  });
});
