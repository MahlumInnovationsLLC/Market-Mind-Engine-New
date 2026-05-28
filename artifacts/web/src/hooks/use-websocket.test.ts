import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

const getTokenMock = vi.fn<() => Promise<string | null>>();

vi.mock("@clerk/react", () => ({
  useAuth: () => ({
    isSignedIn: true,
    getToken: getTokenMock,
  }),
}));

import { useWebSocket } from "./use-websocket";

type Listener = ((ev: unknown) => void) | null;

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  static instances: MockWebSocket[] = [];

  CONNECTING = 0;
  OPEN = 1;
  CLOSING = 2;
  CLOSED = 3;

  url: string;
  readyState: number = MockWebSocket.CONNECTING;
  closeCalls: { code?: number; reason?: string }[] = [];

  onopen: Listener = null;
  onmessage: Listener = null;
  onerror: Listener = null;
  onclose: Listener = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  // Helpers used by tests to drive the socket lifecycle.
  fireOpen() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }

  fireMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }

  fireClose(code = 1006) {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code, wasClean: false, reason: "" } as CloseEvent);
  }

  close(code?: number, reason?: string) {
    this.closeCalls.push({ code, reason });
    this.readyState = MockWebSocket.CLOSED;
  }
}

// Build a JWT-like string with a given exp (unix seconds).  Only the
// payload is parsed by the hook, so the header/signature can be opaque.
function makeJwt(expSeconds: number): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const payload = btoa(JSON.stringify({ exp: expSeconds }))
    .replace(/=+$/, "")
    .replace(/\+/g, "-")
    .replace(/\//g, "_");
  return `${header}.${payload}.sig`;
}

const originalWebSocket = globalThis.WebSocket;

// Fixed wall clock so JWT-exp arithmetic is deterministic.  Aligned to
// a whole second so `exp - now` is exactly the value we encoded.
const FIXED_NOW_MS = new Date("2026-01-01T00:00:00.000Z").getTime();

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_NOW_MS);
  MockWebSocket.instances = [];
  getTokenMock.mockReset();
  (globalThis as unknown as { WebSocket: typeof WebSocket }).WebSocket =
    MockWebSocket as unknown as typeof WebSocket;
});

afterEach(() => {
  vi.useRealTimers();
  (globalThis as unknown as { WebSocket: typeof WebSocket | undefined }).WebSocket =
    originalWebSocket;
});

async function flushPromises() {
  // Resolve any pending microtasks (e.g., awaited getToken()).
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("useWebSocket — proactive token rotation", () => {
  it("schedules rotation based on the JWT exp claim", async () => {
    // Token expires in exactly 60s; rotation should fire at exp - 10s
    // safety = 50s.  System time is fixed so this is deterministic.
    const token = makeJwt(Math.floor((Date.now() + 60_000) / 1000));
    getTokenMock.mockResolvedValue(token);

    const { unmount } = renderHook(() => useWebSocket({ path: "/ws" }));
    await flushPromises();

    expect(MockWebSocket.instances).toHaveLength(1);
    const first = MockWebSocket.instances[0];
    act(() => {
      first.fireOpen();
    });

    // 1ms before the 50s mark: no rotation yet.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(49_999);
    });
    expect(MockWebSocket.instances).toHaveLength(1);

    // Crossing the 50s mark triggers rotation: a new socket is opened
    // alongside the old one.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(MockWebSocket.instances).toHaveLength(2);

    unmount();
  });

  it("falls back to the 50s rotation when no exp claim is present", async () => {
    getTokenMock.mockResolvedValue("not-a-jwt");

    const { unmount } = renderHook(() => useWebSocket({ path: "/ws" }));
    await flushPromises();
    const first = MockWebSocket.instances[0];
    act(() => {
      first.fireOpen();
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(49_999);
    });
    expect(MockWebSocket.instances).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(MockWebSocket.instances).toHaveLength(2);

    unmount();
  });

  it("performs an overlap-and-swap without flickering status", async () => {
    const expSeconds = Math.floor((Date.now() + 60_000) / 1000);
    getTokenMock.mockResolvedValue(makeJwt(expSeconds));

    const { result, unmount } = renderHook(() =>
      useWebSocket({ path: "/ws" }),
    );
    await flushPromises();

    const first = MockWebSocket.instances[0];
    act(() => {
      first.fireOpen();
    });
    expect(result.current.status).toBe("open");

    // Track every status the hook reports from the moment we trigger
    // the rotation, so we can assert it never flickered.
    const observed: string[] = [result.current.status];

    // Advance to the rotation point.  The hook should open a SECOND
    // socket without closing the first, and without changing status.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50_000);
    });
    observed.push(result.current.status);

    expect(MockWebSocket.instances).toHaveLength(2);
    const second = MockWebSocket.instances[1];

    // While rotation is in flight, status MUST stay "open" (no
    // "connecting"/"closed" flicker), and the old socket is still live.
    expect(result.current.status).toBe("open");
    expect(first.readyState).toBe(MockWebSocket.OPEN);
    expect(first.closeCalls).toHaveLength(0);

    // Old socket should keep delivering events during the overlap.
    act(() => {
      first.fireMessage({ type: "tick", n: 1 });
    });
    expect(result.current.lastEvent).toEqual({ type: "tick", n: 1 });

    // Now the new socket opens — but the hook MUST NOT swap until the
    // server's hello-ack arrives.  Old socket is still authoritative.
    act(() => {
      second.fireOpen();
    });
    observed.push(result.current.status);
    expect(first.closeCalls).toHaveLength(0);
    expect(first.readyState).toBe(MockWebSocket.OPEN);

    // Server hello-ack lands — now the swap completes and the old
    // socket is closed.  Status stays "open" throughout.
    act(() => {
      second.fireMessage({ type: "hello", ts: Date.now() });
    });
    observed.push(result.current.status);

    expect(result.current.status).toBe("open");
    expect(second.readyState).toBe(MockWebSocket.OPEN);
    expect(first.closeCalls.length).toBeGreaterThan(0);
    // Hello frames must NOT pollute the event buffer surfaced to
    // consumers — `lastEvent` still reflects the last real tick.
    expect(result.current.lastEvent).toEqual({ type: "tick", n: 1 });

    // After swap, the post-swap close of the OLD socket must not
    // bubble through as a disconnect (its handlers were detached).
    act(() => {
      first.onclose?.({ code: 1000, wasClean: true, reason: "" } as CloseEvent);
    });
    expect(result.current.status).toBe("open");

    // And the new socket is the one delivering events going forward.
    act(() => {
      second.fireMessage({ type: "tick", n: 2 });
    });
    expect(result.current.lastEvent).toEqual({ type: "tick", n: 2 });

    // No status in the observed sequence should ever be anything other
    // than "open" during/after the swap.
    expect(observed.every((s) => s === "open")).toBe(true);

    unmount();
  });

  it("retries rotation when the replacement socket fails to open", async () => {
    const expSeconds = Math.floor((Date.now() + 60_000) / 1000);
    getTokenMock.mockResolvedValue(makeJwt(expSeconds));

    const { result, unmount } = renderHook(() =>
      useWebSocket({ path: "/ws" }),
    );
    await flushPromises();

    const first = MockWebSocket.instances[0];
    act(() => {
      first.fireOpen();
    });
    expect(result.current.status).toBe("open");

    // Trigger the rotation.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50_000);
    });
    expect(MockWebSocket.instances).toHaveLength(2);
    const second = MockWebSocket.instances[1];

    // Replacement socket errors and closes BEFORE ever opening.  The
    // old socket should remain live and the user-visible status should
    // stay "open".
    act(() => {
      second.onerror?.(new Event("error"));
      second.fireClose(1006);
    });

    expect(result.current.status).toBe("open");
    expect(first.readyState).toBe(MockWebSocket.OPEN);
    expect(first.closeCalls).toHaveLength(0);

    // The hook should schedule another rotation attempt soon
    // (ROTATION_MIN_MS = 5s).  After ~5s a third socket appears.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_500);
    });
    expect(MockWebSocket.instances.length).toBeGreaterThanOrEqual(3);
    const third = MockWebSocket.instances[MockWebSocket.instances.length - 1];

    // This retry succeeds: it opens, the server hello-ack lands, the
    // swap completes, status holds.
    act(() => {
      third.fireOpen();
      third.fireMessage({ type: "hello", ts: Date.now() });
    });
    expect(result.current.status).toBe("open");
    expect(first.closeCalls.length).toBeGreaterThan(0);

    unmount();
  });

  it("retains the old socket when the replacement opens but never acks", async () => {
    const expSeconds = Math.floor((Date.now() + 60_000) / 1000);
    getTokenMock.mockResolvedValue(makeJwt(expSeconds));

    const { result, unmount } = renderHook(() =>
      useWebSocket({ path: "/ws" }),
    );
    await flushPromises();

    const first = MockWebSocket.instances[0];
    act(() => {
      first.fireOpen();
    });
    expect(result.current.status).toBe("open");

    // Trigger rotation.  Replacement opens but the server never sends
    // its `hello` frame (e.g. load-balancer killed the upstream).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50_000);
    });
    expect(MockWebSocket.instances).toHaveLength(2);
    const second = MockWebSocket.instances[1];

    act(() => {
      second.fireOpen();
    });

    // Old socket MUST remain untouched while we wait for the ack.
    expect(first.closeCalls).toHaveLength(0);
    expect(first.readyState).toBe(MockWebSocket.OPEN);
    expect(result.current.status).toBe("open");

    // Burn the 5s ACK timeout — the hook should close the stalled
    // replacement, mark a rotation failure, and schedule a retry.
    // Old socket stays live the entire time.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_001);
    });
    expect(second.closeCalls.length).toBeGreaterThan(0);
    expect(first.closeCalls).toHaveLength(0);
    expect(first.readyState).toBe(MockWebSocket.OPEN);
    expect(result.current.status).toBe("open");
    expect(result.current.rotationFailureCount).toBeGreaterThan(0);
    expect(result.current.lastRotationError ?? "").toMatch(
      /did not ack/i,
    );

    // A retry attempt should then fire on the ROTATION_MIN_MS (5s)
    // timer; a fresh replacement appears and this one acks.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_500);
    });
    expect(MockWebSocket.instances.length).toBeGreaterThanOrEqual(3);
    const third = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    act(() => {
      third.fireOpen();
      third.fireMessage({ type: "hello", ts: Date.now() });
    });
    expect(first.closeCalls.length).toBeGreaterThan(0);
    expect(result.current.status).toBe("open");
    // Successful rotation resets the failure counter.
    expect(result.current.rotationFailureCount).toBe(0);
    expect(result.current.lastRotationError).toBeNull();

    unmount();
  });

  it("closes the opened-but-unacked replacement on unmount", async () => {
    const expSeconds = Math.floor((Date.now() + 60_000) / 1000);
    getTokenMock.mockResolvedValue(makeJwt(expSeconds));

    const { result, unmount } = renderHook(() =>
      useWebSocket({ path: "/ws" }),
    );
    await flushPromises();

    const first = MockWebSocket.instances[0];
    act(() => {
      first.fireOpen();
    });
    expect(result.current.status).toBe("open");

    // Trigger rotation; the replacement opens but never acks.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50_000);
    });
    expect(MockWebSocket.instances).toHaveLength(2);
    const second = MockWebSocket.instances[1];
    act(() => {
      second.fireOpen();
    });
    // Pre-condition: replacement is alive, ack-timer pending.
    expect(second.closeCalls).toHaveLength(0);
    expect(second.readyState).toBe(MockWebSocket.OPEN);

    // Unmount mid-handshake — must close BOTH sockets, no leaks.
    unmount();
    expect(first.closeCalls.length).toBeGreaterThan(0);
    expect(second.closeCalls.length).toBeGreaterThan(0);

    // And the dangling ack-timer must not fire after unmount: bump
    // time well past the ROTATION_ACK_TIMEOUT_MS and confirm no extra
    // sockets get constructed by a retry path.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it("counts an ack-timeout rotation failure exactly once", async () => {
    const expSeconds = Math.floor((Date.now() + 60_000) / 1000);
    getTokenMock.mockResolvedValue(makeJwt(expSeconds));

    const { result, unmount } = renderHook(() =>
      useWebSocket({ path: "/ws" }),
    );
    await flushPromises();
    const first = MockWebSocket.instances[0];
    act(() => {
      first.fireOpen();
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(50_000);
    });
    const second = MockWebSocket.instances[1];
    act(() => {
      second.fireOpen();
    });

    // Burn the ack-timeout.  Implementation calls close() which
    // synchronously fires our MockWebSocket.onclose — so both the
    // timeout path AND the onclose path race to record a failure.
    // The single-shot `markFailed` guard must ensure only one lands.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_500);
    });
    expect(result.current.rotationFailureCount).toBe(1);
    expect(result.current.lastRotationError ?? "").toMatch(/did not ack/i);

    unmount();
  });

  it("filters server hello frames so they never reach the event buffer", async () => {
    getTokenMock.mockResolvedValue("not-a-jwt");

    const { result, unmount } = renderHook(() =>
      useWebSocket({ path: "/ws" }),
    );
    await flushPromises();

    const first = MockWebSocket.instances[0];
    act(() => {
      first.fireOpen();
    });

    // Server's initial hello on the freshly-connected socket — must be
    // swallowed so it doesn't show up in `events` / `lastEvent`.
    act(() => {
      first.fireMessage({ type: "hello", ts: Date.now() });
    });
    expect(result.current.events).toHaveLength(0);
    expect(result.current.lastEvent).toBeNull();

    // A real event right after still lands normally.
    act(() => {
      first.fireMessage({ type: "tick", n: 1 });
    });
    expect(result.current.events).toHaveLength(1);
    expect(result.current.lastEvent).toEqual({ type: "tick", n: 1 });

    unmount();
  });

  it("retries rotation when constructing the replacement socket throws", async () => {
    const expSeconds = Math.floor((Date.now() + 60_000) / 1000);
    getTokenMock.mockResolvedValue(makeJwt(expSeconds));

    const { result, unmount } = renderHook(() =>
      useWebSocket({ path: "/ws" }),
    );
    await flushPromises();

    const first = MockWebSocket.instances[0];
    act(() => {
      first.fireOpen();
    });

    // Make the next WebSocket constructor throw, simulating an
    // immediate failure to even create the replacement.
    const RealMock = MockWebSocket;
    let throwOnce = true;
    const ThrowingMock = function (this: MockWebSocket, url: string) {
      if (throwOnce) {
        throwOnce = false;
        throw new Error("ctor blew up");
      }
      return new RealMock(url);
    } as unknown as typeof WebSocket;
    (ThrowingMock as unknown as { OPEN: number }).OPEN = MockWebSocket.OPEN;
    (globalThis as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      ThrowingMock;

    // Trigger rotation; the constructor throws, but the old socket
    // stays live and status remains "open".
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50_000);
    });
    expect(result.current.status).toBe("open");
    expect(first.closeCalls).toHaveLength(0);

    // Restore the working mock so the retry can succeed.
    (globalThis as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      MockWebSocket as unknown as typeof WebSocket;

    // The retry timer (ROTATION_MIN_MS = 5s) should fire and open a
    // fresh replacement.
    const beforeCount = MockWebSocket.instances.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_500);
    });
    expect(MockWebSocket.instances.length).toBeGreaterThan(beforeCount);

    unmount();
  });
});
