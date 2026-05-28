import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  apiFetch,
  formatErrorMessage,
  getErrorRequestId,
  resolveApiUrl,
} from "./api";

type FetchMock = ReturnType<typeof vi.fn>;

let originalFetch: typeof fetch;
let fetchMock: FetchMock;

beforeEach(() => {
  originalFetch = globalThis.fetch;
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("apiFetch error handling", () => {
  it("captures the response x-request-id on the thrown ApiError", async () => {
    // `noRetry` keeps this test focused on the error-shape contract
    // without exercising the retry-fetch wrapper introduced in #924.
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "boom" }), {
        status: 500,
        statusText: "Internal Server Error",
        headers: {
          "content-type": "application/json",
          "x-request-id": "req-abc-123",
        },
      }),
    );

    let captured: ApiError | null = null;
    try {
      await apiFetch("/api/widgets", { noRetry: true });
    } catch (err) {
      captured = err as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured?.status).toBe(500);
    expect(captured?.requestId).toBe("req-abc-123");
  });

  it("leaves requestId null when the server omits the header", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("nope", { status: 503, statusText: "Unavailable" }),
    );

    let captured: ApiError | null = null;
    try {
      await apiFetch("/api/widgets", { noRetry: true });
    } catch (err) {
      captured = err as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured?.requestId).toBeNull();
  });
});

describe("apiFetch retry policy (Task #924)", () => {
  it("retries a GET on 503 and returns the eventual 200", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response("", { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    const out = await apiFetch<{ ok: boolean }>("/api/widgets");
    expect(out).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does NOT retry a POST on 503 by default", async () => {
    fetchMock.mockResolvedValueOnce(new Response("", { status: 503 }));
    await expect(
      apiFetch("/api/widgets", { method: "POST", body: { a: 1 } }),
    ).rejects.toMatchObject({ status: 503 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries a POST when retryMutation: true is opted in", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response("", { status: 503 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200 }));
    await apiFetch("/api/widgets", {
      method: "POST",
      body: { a: 1 },
      retryMutation: true,
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("formatErrorMessage", () => {
  it("appends the reference id from a local ApiError", () => {
    const err = new ApiError("HTTP 500 :: /api/widgets", 500, null, "abc123");
    expect(formatErrorMessage(err)).toBe(
      "HTTP 500 :: /api/widgets (Reference: abc123)",
    );
  });

  it("honours the baseOverride and still appends the reference id", () => {
    const err = new ApiError("HTTP 500 :: /api/widgets", 500, null, "abc123");
    expect(formatErrorMessage(err, "Save failed (HTTP 500)")).toBe(
      "Save failed (HTTP 500) (Reference: abc123)",
    );
  });

  it("falls back to the message when no reference id is available", () => {
    expect(formatErrorMessage(new Error("network down"))).toBe("network down");
  });

  it("reads the reference id off a generated-client style error with headers", () => {
    const err = {
      message: "HTTP 502 Bad Gateway",
      headers: new Headers({ "x-request-id": "header-id-9" }),
    };
    expect(getErrorRequestId(err)).toBe("header-id-9");
    expect(formatErrorMessage(err, "Upstream failed")).toBe(
      "Upstream failed (Reference: header-id-9)",
    );
  });

  it("returns null reference id for plain errors", () => {
    expect(getErrorRequestId(new Error("boom"))).toBeNull();
    expect(getErrorRequestId(null)).toBeNull();
    expect(getErrorRequestId("string error")).toBeNull();
  });
});

describe("resolveApiUrl + apiFetch host knob (VITE_API_URL)", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("returns the path unchanged when VITE_API_URL is empty (Replit default)", () => {
    vi.stubEnv("VITE_API_URL", "");
    expect(resolveApiUrl("/api/widgets")).toBe("/api/widgets");
  });

  it("prepends VITE_API_URL when set, stripping trailing slash", () => {
    vi.stubEnv("VITE_API_URL", "https://api.example.com/");
    expect(resolveApiUrl("/api/widgets")).toBe(
      "https://api.example.com/api/widgets",
    );
  });

  it("treats absolute URLs as-is regardless of VITE_API_URL", () => {
    vi.stubEnv("VITE_API_URL", "https://api.example.com");
    expect(resolveApiUrl("https://elsewhere.test/extern")).toBe(
      "https://elsewhere.test/extern",
    );
  });

  it("apiFetch hits the relative path when VITE_API_URL is empty", async () => {
    vi.stubEnv("VITE_API_URL", "");
    fetchMock.mockResolvedValueOnce(new Response("{}", { status: 200 }));
    await apiFetch("/api/widgets");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/widgets");
  });

  it("apiFetch hits the absolute URL when VITE_API_URL is set", async () => {
    vi.stubEnv("VITE_API_URL", "https://api.example.com");
    fetchMock.mockResolvedValueOnce(new Response("{}", { status: 200 }));
    await apiFetch("/api/widgets");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "https://api.example.com/api/widgets",
    );
  });

  it("falls back to same-origin on a malformed VITE_API_URL", () => {
    // Critical security property: a typo'd env var must NEVER silently
    // route an authenticated request (apiFetch attaches the bearer
    // token) to wherever the URL parser lands. Degrade to today's
    // same-origin behavior instead.
    vi.stubEnv("VITE_API_URL", "not a url");
    expect(resolveApiUrl("/api/widgets")).toBe("/api/widgets");
  });

  it("rejects unsupported protocols (e.g. javascript:, file:)", () => {
    vi.stubEnv("VITE_API_URL", "javascript:alert(1)");
    expect(resolveApiUrl("/api/widgets")).toBe("/api/widgets");
  });

  it("trims whitespace before validating VITE_API_URL", () => {
    vi.stubEnv("VITE_API_URL", "  https://api.example.com  ");
    expect(resolveApiUrl("/api/widgets")).toBe(
      "https://api.example.com/api/widgets",
    );
  });
});
