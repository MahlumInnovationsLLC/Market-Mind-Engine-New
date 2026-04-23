import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  apiFetch,
  formatErrorMessage,
  getErrorRequestId,
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
      await apiFetch("/api/widgets");
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
      await apiFetch("/api/widgets");
    } catch (err) {
      captured = err as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured?.requestId).toBeNull();
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
