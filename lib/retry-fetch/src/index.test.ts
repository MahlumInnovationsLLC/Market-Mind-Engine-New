/**
 * Tests for the shared retry-with-backoff wrapper.
 *
 * Both web (`vitest`) and mobile (`jest`) consume this module; the tests
 * live here under vitest because the wrapper has no DOM/React surface —
 * the policy is what's worth pinning.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  computeBackoff,
  fetchWithRetry,
  isIdempotent,
  setRetryObserver,
  type RetryObserverEvent,
} from "./index";

type FetchMock = ReturnType<typeof vi.fn>;

let fetchMock: FetchMock;
let originalFetch: typeof fetch;

function jsonOk(body: unknown = { ok: true }, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const NO_SLEEP = (_ms: number) => Promise.resolve();
const FIXED_RNG = () => 0.5; // jitterFactor = 1 (no jitter)

beforeEach(() => {
  originalFetch = globalThis.fetch;
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  setRetryObserver(null);
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  setRetryObserver(null);
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Idempotency rule
// ---------------------------------------------------------------------------

describe("isIdempotent / verb-based default", () => {
  it("treats GET and HEAD as idempotent", () => {
    expect(isIdempotent("GET")).toBe(true);
    expect(isIdempotent("get")).toBe(true);
    expect(isIdempotent("HEAD")).toBe(true);
  });

  it("treats POST/PUT/PATCH/DELETE as non-idempotent", () => {
    for (const m of ["POST", "PUT", "PATCH", "DELETE"]) {
      expect(isIdempotent(m)).toBe(false);
    }
  });
});

describe("retry policy by verb", () => {
  it("retries a GET on 503 and returns the eventual 200", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response("first", { status: 503 }))
      .mockResolvedValueOnce(new Response("second", { status: 503 }))
      .mockResolvedValueOnce(jsonOk());
    const res = await fetchWithRetry(
      "/api/widgets",
      { method: "GET" },
      { sleep: NO_SLEEP, rng: FIXED_RNG },
    );
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does NOT retry a POST on 503 by default", async () => {
    fetchMock.mockResolvedValueOnce(new Response("nope", { status: 503 }));
    const res = await fetchWithRetry(
      "/api/widgets",
      { method: "POST" },
      { sleep: NO_SLEEP, rng: FIXED_RNG },
    );
    expect(res.status).toBe(503);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("respects an explicit retryMutation opt-in (retry: true) on a POST", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response("503", { status: 503 }))
      .mockResolvedValueOnce(jsonOk());
    const res = await fetchWithRetry(
      "/api/widgets",
      { method: "POST" },
      { retry: true, sleep: NO_SLEEP, rng: FIXED_RNG },
    );
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("honours retry: false even for a GET (caller override)", async () => {
    fetchMock.mockResolvedValueOnce(new Response("503", { status: 503 }));
    const res = await fetchWithRetry(
      "/api/widgets",
      { method: "GET" },
      { retry: false, sleep: NO_SLEEP, rng: FIXED_RNG },
    );
    expect(res.status).toBe(503);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// Which statuses retry / which don't
// ---------------------------------------------------------------------------

describe("status-class rules", () => {
  it("does NOT retry on 400 (caller mistake — needs to surface)", async () => {
    fetchMock.mockResolvedValueOnce(new Response("bad", { status: 400 }));
    const res = await fetchWithRetry("/api/widgets", { method: "GET" }, { sleep: NO_SLEEP });
    expect(res.status).toBe(400);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does NOT retry on 401, 403, 404, 422", async () => {
    for (const status of [401, 403, 404, 422]) {
      fetchMock.mockReset();
      fetchMock.mockResolvedValueOnce(new Response("", { status }));
      const res = await fetchWithRetry(
        "/api/widgets",
        { method: "GET" },
        { sleep: NO_SLEEP },
      );
      expect(res.status).toBe(status);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    }
  });

  it("retries on 408 Request Timeout (transient 4xx)", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response("", { status: 408 }))
      .mockResolvedValueOnce(jsonOk());
    const res = await fetchWithRetry(
      "/api/widgets",
      { method: "GET" },
      { sleep: NO_SLEEP, rng: FIXED_RNG },
    );
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("retries on 429 Too Many Requests (transient 4xx)", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response("", { status: 429 }))
      .mockResolvedValueOnce(jsonOk());
    const res = await fetchWithRetry(
      "/api/widgets",
      { method: "GET" },
      { sleep: NO_SLEEP, rng: FIXED_RNG },
    );
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("retries on every 5xx (500, 502, 503, 504)", async () => {
    for (const status of [500, 502, 503, 504]) {
      fetchMock.mockReset();
      fetchMock
        .mockResolvedValueOnce(new Response("", { status }))
        .mockResolvedValueOnce(jsonOk());
      const res = await fetchWithRetry(
        "/api/widgets",
        { method: "GET" },
        { sleep: NO_SLEEP, rng: FIXED_RNG },
      );
      expect(res.status).toBe(200);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    }
  });

  it("returns the LAST response after maxAttempts on persistent 5xx (no throw)", async () => {
    fetchMock.mockResolvedValue(new Response("nope", { status: 502 }));
    const res = await fetchWithRetry(
      "/api/widgets",
      { method: "GET" },
      { sleep: NO_SLEEP, rng: FIXED_RNG, maxAttempts: 3 },
    );
    expect(res.status).toBe(502);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

// ---------------------------------------------------------------------------
// Network errors
// ---------------------------------------------------------------------------

describe("network error handling", () => {
  it("retries on a thrown TypeError (DNS / TCP failure)", async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError("network down"))
      .mockResolvedValueOnce(jsonOk());
    const res = await fetchWithRetry(
      "/api/widgets",
      { method: "GET" },
      { sleep: NO_SLEEP, rng: FIXED_RNG },
    );
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("re-throws the last error after maxAttempts network failures", async () => {
    fetchMock.mockRejectedValue(new TypeError("network down"));
    await expect(
      fetchWithRetry(
        "/api/widgets",
        { method: "GET" },
        { sleep: NO_SLEEP, rng: FIXED_RNG, maxAttempts: 2 },
      ),
    ).rejects.toThrow("network down");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does NOT retry when the CALLER aborted the request", async () => {
    const ac = new AbortController();
    fetchMock.mockImplementationOnce(async (_input, init: RequestInit) => {
      ac.abort(); // simulate caller cancel mid-flight
      const err = new Error("aborted") as Error & { name: string };
      err.name = "AbortError";
      throw err;
    });
    await expect(
      fetchWithRetry(
        "/api/widgets",
        { method: "GET", signal: ac.signal },
        { sleep: NO_SLEEP, rng: FIXED_RNG },
      ),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// Backoff math
// ---------------------------------------------------------------------------

describe("computeBackoff", () => {
  it("returns base on the first retry with no jitter", () => {
    expect(computeBackoff(1, 200, 5000, FIXED_RNG)).toBe(200);
  });

  it("doubles on each subsequent attempt up to maxDelayMs", () => {
    expect(computeBackoff(2, 200, 5000, FIXED_RNG)).toBe(400);
    expect(computeBackoff(3, 200, 5000, FIXED_RNG)).toBe(800);
    expect(computeBackoff(10, 200, 5000, FIXED_RNG)).toBe(5000);
  });

  it("applies ±25% symmetric jitter (rng=0 → -25%, rng=1 → +25%)", () => {
    // attempt=1, base=200
    expect(computeBackoff(1, 200, 5000, () => 0)).toBe(Math.round(200 * 0.75));
    expect(computeBackoff(1, 200, 5000, () => 1)).toBe(Math.round(200 * 1.25));
  });

  it("never returns a negative delay", () => {
    expect(computeBackoff(1, 0, 5000, () => 0)).toBeGreaterThanOrEqual(0);
  });
});

describe("totalBudgetMs", () => {
  it("returns the last response instead of sleeping past the budget", async () => {
    const sleeps: number[] = [];
    fetchMock.mockResolvedValue(new Response("", { status: 503 }));
    const res = await fetchWithRetry(
      "/api/widgets",
      { method: "GET" },
      {
        rng: () => 1, // max-jitter, +25% — pushes us over a small budget fast
        sleep: async (ms) => {
          sleeps.push(ms);
        },
        baseDelayMs: 1000,
        maxAttempts: 5,
        totalBudgetMs: 1, // ~zero budget — first delay must trip the cap
      },
    );
    expect(res.status).toBe(503);
    expect(sleeps.length).toBe(0); // budget tripped before the first sleep
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// Telemetry
// ---------------------------------------------------------------------------

describe("setRetryObserver", () => {
  it("emits a retry event for each retried 5xx and a final success_after_retry", async () => {
    const events: RetryObserverEvent[] = [];
    setRetryObserver((e) => events.push(e));
    fetchMock
      .mockResolvedValueOnce(new Response("", { status: 503 }))
      .mockResolvedValueOnce(new Response("", { status: 502 }))
      .mockResolvedValueOnce(jsonOk());
    await fetchWithRetry(
      "/api/widgets",
      { method: "GET" },
      { sleep: NO_SLEEP, rng: FIXED_RNG },
    );
    expect(events).toEqual([
      {
        kind: "retry",
        attempt: 1,
        reason: { type: "status", status: 503 },
        url: "/api/widgets",
        method: "GET",
      },
      {
        kind: "retry",
        attempt: 2,
        reason: { type: "status", status: 502 },
        url: "/api/widgets",
        method: "GET",
      },
      {
        kind: "success_after_retry",
        attempts: 3,
        url: "/api/widgets",
        method: "GET",
      },
    ]);
  });

  it("emits final_failure on persistent network error", async () => {
    const events: RetryObserverEvent[] = [];
    setRetryObserver((e) => events.push(e));
    fetchMock.mockRejectedValue(new TypeError("nope"));
    await expect(
      fetchWithRetry(
        "/api/widgets",
        { method: "GET" },
        { sleep: NO_SLEEP, rng: FIXED_RNG, maxAttempts: 2 },
      ),
    ).rejects.toThrow();
    const finals = events.filter((e) => e.kind === "final_failure");
    expect(finals).toHaveLength(1);
    expect(finals[0]).toMatchObject({
      kind: "final_failure",
      attempts: 2,
      reason: { type: "network" },
    });
  });

  it("swallows errors thrown by the observer", async () => {
    setRetryObserver(() => {
      throw new Error("observer blew up");
    });
    fetchMock
      .mockResolvedValueOnce(new Response("", { status: 503 }))
      .mockResolvedValueOnce(jsonOk());
    const res = await fetchWithRetry(
      "/api/widgets",
      { method: "GET" },
      { sleep: NO_SLEEP, rng: FIXED_RNG },
    );
    expect(res.status).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// AbortController + attempt timeout
// ---------------------------------------------------------------------------

describe("caller abort during backoff", () => {
  it("aborts a status-retry sleep when the caller cancels mid-backoff", async () => {
    const ac = new AbortController();
    fetchMock.mockResolvedValue(new Response("", { status: 503 }));
    const sleep = (_ms: number) =>
      // Long sleep so the caller's abort fires before it would resolve.
      new Promise<void>((resolve) => setTimeout(resolve, 1000));
    const promise = fetchWithRetry(
      "/api/widgets",
      { method: "GET", signal: ac.signal },
      { sleep, rng: FIXED_RNG, maxAttempts: 3, baseDelayMs: 50 },
    );
    // Fire the abort once the first attempt has resolved and the
    // wrapper has entered its back-off.
    setTimeout(() => ac.abort(), 10);
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("aborts a network-retry sleep when the caller cancels mid-backoff", async () => {
    const ac = new AbortController();
    fetchMock.mockRejectedValue(new TypeError("net down"));
    const sleep = (_ms: number) =>
      new Promise<void>((resolve) => setTimeout(resolve, 1000));
    const promise = fetchWithRetry(
      "/api/widgets",
      { method: "GET", signal: ac.signal },
      { sleep, rng: FIXED_RNG, maxAttempts: 3, baseDelayMs: 50 },
    );
    setTimeout(() => ac.abort(), 10);
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("attemptTimeoutMs", () => {
  it("aborts a stuck attempt and retries with a fresh one", async () => {
    let callCount = 0;
    fetchMock.mockImplementation(async (_input, init: RequestInit) => {
      callCount += 1;
      if (callCount === 1) {
        // First call: hang until the per-attempt timeout fires.
        return await new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            const err = new Error("aborted") as Error & { name: string };
            err.name = "AbortError";
            reject(err);
          });
        });
      }
      return jsonOk();
    });
    const res = await fetchWithRetry(
      "/api/widgets",
      { method: "GET" },
      { sleep: NO_SLEEP, rng: FIXED_RNG, attemptTimeoutMs: 10, maxAttempts: 2 },
    );
    expect(res.status).toBe(200);
    expect(callCount).toBe(2);
  });
});
