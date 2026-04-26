/**
 * Unit tests for the toast reducer + `toast()` API.
 *
 * `useToast` is the only toast surface in the operator dashboard:
 * realtime fill notifications, alert escalations, "copied to
 * clipboard" feedback, and form-error recovery all flow through it.
 * The reducer is a near-verbatim port of the shadcn template, but the
 * project intentionally bumps `TOAST_LIMIT` to 5 and shrinks
 * `TOAST_REMOVE_DELAY` to 6s — so the reducer's stacking + auto-dismiss
 * contract has to keep working as fixtures evolve.
 *
 * The hook itself uses module-level state, so each test resets that
 * state via `dismiss()` + `act(() => vi.runAllTimers())` and uses fake
 * timers to drive the auto-remove queue deterministically.
 */
import { act, renderHook } from "@testing-library/react";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { reducer, toast, useToast } from "./use-toast";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  // Drain any pending auto-remove timeouts so module-level state from
  // one test doesn't bleed into the next.
  act(() => {
    vi.runAllTimers();
  });
  vi.useRealTimers();
});

describe("reducer (pure)", () => {
  it("ADD_TOAST prepends and caps at the toast limit (5)", () => {
    let state = { toasts: [] as ReturnType<typeof reducer>["toasts"] };
    for (let i = 0; i < 7; i += 1) {
      state = reducer(state, {
        type: "ADD_TOAST",
        toast: { id: String(i), open: true },
      });
    }
    // Only the 5 most-recent toasts survive, with the latest at the
    // front (newest-first stacking).
    expect(state.toasts.map((t) => t.id)).toEqual([
      "6",
      "5",
      "4",
      "3",
      "2",
    ]);
  });

  it("UPDATE_TOAST patches in-place by id", () => {
    let state = { toasts: [] as ReturnType<typeof reducer>["toasts"] };
    state = reducer(state, {
      type: "ADD_TOAST",
      toast: { id: "a", open: true, title: "first" },
    });
    state = reducer(state, {
      type: "UPDATE_TOAST",
      toast: { id: "a", title: "second" },
    });
    expect(state.toasts[0]?.title).toBe("second");
    // Other props remain untouched.
    expect(state.toasts[0]?.open).toBe(true);
  });

  it("DISMISS_TOAST flips a single toast's open flag without removing it", () => {
    let state = { toasts: [] as ReturnType<typeof reducer>["toasts"] };
    state = reducer(state, {
      type: "ADD_TOAST",
      toast: { id: "a", open: true },
    });
    state = reducer(state, {
      type: "ADD_TOAST",
      toast: { id: "b", open: true },
    });
    state = reducer(state, { type: "DISMISS_TOAST", toastId: "a" });
    const a = state.toasts.find((t) => t.id === "a");
    const b = state.toasts.find((t) => t.id === "b");
    expect(a?.open).toBe(false);
    expect(b?.open).toBe(true);
  });

  it("DISMISS_TOAST without an id flips every toast to closed", () => {
    let state = { toasts: [] as ReturnType<typeof reducer>["toasts"] };
    state = reducer(state, {
      type: "ADD_TOAST",
      toast: { id: "a", open: true },
    });
    state = reducer(state, {
      type: "ADD_TOAST",
      toast: { id: "b", open: true },
    });
    state = reducer(state, { type: "DISMISS_TOAST" });
    expect(state.toasts.every((t) => t.open === false)).toBe(true);
  });

  it("REMOVE_TOAST drops a single toast by id", () => {
    let state = { toasts: [] as ReturnType<typeof reducer>["toasts"] };
    state = reducer(state, {
      type: "ADD_TOAST",
      toast: { id: "a", open: true },
    });
    state = reducer(state, {
      type: "ADD_TOAST",
      toast: { id: "b", open: true },
    });
    state = reducer(state, { type: "REMOVE_TOAST", toastId: "a" });
    expect(state.toasts.map((t) => t.id)).toEqual(["b"]);
  });

  it("REMOVE_TOAST without an id clears every toast", () => {
    let state = { toasts: [] as ReturnType<typeof reducer>["toasts"] };
    state = reducer(state, {
      type: "ADD_TOAST",
      toast: { id: "a", open: true },
    });
    state = reducer(state, {
      type: "ADD_TOAST",
      toast: { id: "b", open: true },
    });
    state = reducer(state, { type: "REMOVE_TOAST" });
    expect(state.toasts).toEqual([]);
  });
});

describe("toast() + useToast() integration", () => {
  it("makes a toast visible to subscribed components", () => {
    const { result } = renderHook(() => useToast());
    act(() => {
      toast({ title: "hello" });
    });
    expect(result.current.toasts.length).toBeGreaterThan(0);
    expect(result.current.toasts[0]?.title).toBe("hello");
    expect(result.current.toasts[0]?.open).toBe(true);
  });

  it("returns an `update` callback that mutates the live toast", () => {
    const { result } = renderHook(() => useToast());
    let handle!: ReturnType<typeof toast>;
    act(() => {
      handle = toast({ title: "first" });
    });
    act(() => {
      handle.update({ id: handle.id, title: "second", open: true });
    });
    const live = result.current.toasts.find((t) => t.id === handle.id);
    expect(live?.title).toBe("second");
  });

  it("dismiss() flips open=false immediately and REMOVE fires after the delay", () => {
    const { result } = renderHook(() => useToast());
    let handle!: ReturnType<typeof toast>;
    act(() => {
      handle = toast({ title: "to-dismiss" });
    });
    act(() => {
      handle.dismiss();
    });
    const dismissed = result.current.toasts.find((t) => t.id === handle.id);
    expect(dismissed?.open).toBe(false);

    // After the 6s remove delay the toast vanishes from state entirely.
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    expect(
      result.current.toasts.find((t) => t.id === handle.id),
    ).toBeUndefined();
  });

  it("calling onOpenChange(false) from the underlying primitive triggers dismiss()", () => {
    const { result } = renderHook(() => useToast());
    let handle!: ReturnType<typeof toast>;
    act(() => {
      handle = toast({ title: "auto-close" });
    });
    const live = result.current.toasts.find((t) => t.id === handle.id);
    expect(live?.open).toBe(true);

    // Simulate the radix primitive emitting `onOpenChange(false)` when
    // the user clicks the close affordance — the wrapper should route
    // that through `dismiss()` which flips open=false.
    act(() => {
      live?.onOpenChange?.(false);
    });
    expect(
      result.current.toasts.find((t) => t.id === handle.id)?.open,
    ).toBe(false);
  });

  it("caps the visible toast stack at 5 even under burst load", () => {
    const { result } = renderHook(() => useToast());
    act(() => {
      for (let i = 0; i < 8; i += 1) {
        toast({ title: `burst-${i}` });
      }
    });
    expect(result.current.toasts.length).toBe(5);
    // Newest entries survive — `burst-7` should be at the front.
    expect(result.current.toasts[0]?.title).toBe("burst-7");
  });

  it("dismiss() with no id clears every visible toast", () => {
    const { result } = renderHook(() => useToast());
    act(() => {
      toast({ title: "a" });
      toast({ title: "b" });
      toast({ title: "c" });
    });
    // Module-level state can already hold toasts from earlier tests in
    // the file — assert the floor (>=3) rather than the exact count.
    expect(result.current.toasts.length).toBeGreaterThanOrEqual(3);
    act(() => {
      result.current.dismiss();
    });
    expect(result.current.toasts.every((t) => t.open === false)).toBe(true);
  });
});
