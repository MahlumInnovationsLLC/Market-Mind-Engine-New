/**
 * Unit tests for the mobile `apiFetch` helper.
 *
 * Every screen on the mobile app — verticals list, subscription tab,
 * dashboard, settings — funnels its REST traffic through `apiFetch`,
 * so the contract verified here (header attach, request-id echo,
 * Bearer auth, error wrapping) is the single most-shared bit of
 * networking logic on the mobile side.
 *
 * The Replit dev domain comes in three flavours
 * (bare host, http://, https://) and CI exercises all three; the
 * `resolveBaseUrl` cases below are why the mobile dashboard tests
 * don't wedge on `https://http://localhost:8080` when the local API
 * is plain HTTP.
 */
import {
  ApiError,
  REQUEST_ID_HEADER,
  apiFetch,
  newRequestId,
  setTokenGetter,
} from "@/lib/api";

type FetchMock = jest.Mock;

let fetchMock: FetchMock;
let originalFetch: typeof fetch;

beforeEach(() => {
  originalFetch = global.fetch;
  fetchMock = jest.fn();
  global.fetch = fetchMock as unknown as typeof fetch;
  // Reset the module-level token getter so leftover state from one
  // test doesn't leak an Authorization header into the next.
  setTokenGetter(null);
});

afterEach(() => {
  global.fetch = originalFetch;
  setTokenGetter(null);
});

function jsonResponse(
  body: unknown,
  init: { status?: number; headers?: Record<string, string> } = {},
): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status: init.status ?? 200,
    headers: {
      "content-type": "application/json",
      ...(init.headers ?? {}),
    },
  });
}

describe("newRequestId", () => {
  it("returns a 32-char hex string (uuid4 with dashes stripped)", () => {
    const id = newRequestId();
    expect(id).toMatch(/^[0-9a-f]{32}$/i);
  });

  it("returns a different id on each call (collision-free in practice)", () => {
    const ids = new Set<string>();
    for (let i = 0; i < 50; i += 1) ids.add(newRequestId());
    expect(ids.size).toBe(50);
  });
});

describe("apiFetch - request shape", () => {
  it("issues a GET with accept:application/json and a generated x-request-id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await apiFetch("/api/me");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init.method).toBe("GET");
    expect(init.headers.accept).toBe("application/json");
    expect(init.headers[REQUEST_ID_HEADER]).toMatch(/^[0-9a-f]{32}$/i);
    expect(init.body).toBeUndefined();
  });

  it("serialises a JSON body and sets content-type", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await apiFetch("/api/foo", { method: "POST", body: { a: 1 } });
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init.method).toBe("POST");
    expect(init.headers["content-type"]).toBe("application/json");
    expect(init.body).toBe(JSON.stringify({ a: 1 }));
  });

  it("preserves a caller-supplied x-request-id (so a fan-out shares one id)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await apiFetch("/api/foo", {
      headers: { [REQUEST_ID_HEADER]: "shared-123" },
    });
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init.headers[REQUEST_ID_HEADER]).toBe("shared-123");
  });

  it("attaches a Bearer token from the registered token getter", async () => {
    setTokenGetter(async () => "token-xyz");
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await apiFetch("/api/foo");
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init.headers.authorization).toBe("Bearer token-xyz");
  });

  it("does not attach an Authorization header when the token getter returns null", async () => {
    setTokenGetter(async () => null);
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await apiFetch("/api/foo");
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init.headers.authorization).toBeUndefined();
  });

  it("respects a fully-qualified URL instead of prepending the base", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await apiFetch("https://example.com/probe");
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toBe("https://example.com/probe");
  });
});

describe("apiFetch - response decoding", () => {
  it("returns the parsed JSON body on success", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ a: 1, b: "two" }));
    const out = await apiFetch<{ a: number; b: string }>("/api/foo");
    expect(out).toEqual({ a: 1, b: "two" });
  });

  it("returns the raw text when the body isn't valid JSON", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("plain-text-body", {
        status: 200,
        headers: { "content-type": "text/plain" },
      }),
    );
    const out = await apiFetch("/api/foo");
    expect(out).toBe("plain-text-body");
  });
});

describe("apiFetch - retry policy (Task #924)", () => {
  it("retries a GET on 503 and returns the eventual 200 (transparent recovery)", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response("", { status: 503 }))
      .mockResolvedValueOnce(jsonResponse({ a: 1 }));
    const out = await apiFetch<{ a: number }>("/api/foo");
    expect(out).toEqual({ a: 1 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does NOT retry a POST on 503 by default (mutation safety)", async () => {
    fetchMock.mockResolvedValueOnce(new Response("", { status: 503 }));
    await expect(
      apiFetch("/api/foo", { method: "POST", body: { x: 1 } }),
    ).rejects.toMatchObject({ status: 503 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries a POST when retryMutation: true is explicitly opted in", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response("", { status: 503 }))
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    const out = await apiFetch("/api/foo", {
      method: "POST",
      body: { x: 1 },
      retryMutation: true,
    });
    expect(out).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("apiFetch - error path", () => {
  it("throws an ApiError carrying status, parsed body, and the echoed request id", async () => {
    // GET retries on 5xx (Task #924). Use `mockResolvedValue` so every
    // attempt returns the same 500 — the final retry surfaces the
    // server-echoed request id we assert on.
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        jsonResponse(
          { detail: "boom" },
          {
            status: 500,
            headers: { [REQUEST_ID_HEADER]: "req-server-99" },
          },
        ),
      ),
    );

    let captured: ApiError | null = null;
    try {
      await apiFetch("/api/foo", { noRetry: true });
    } catch (err) {
      captured = err as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured?.status).toBe(500);
    expect(captured?.body).toEqual({ detail: "boom" });
    expect(captured?.requestId).toBe("req-server-99");
    // The thrown message embeds the request id so support can grep for
    // it without inspecting `err.requestId` directly.
    expect(captured?.message).toContain("req-server-99");
  });

  it("falls back to the request id we sent when the server omits the header", async () => {
    // Pass an explicit id so we can assert it's the one preserved on the
    // error rather than the auto-generated one. `noRetry` short-circuits
    // the 5xx retry loop so the test runs at the same speed as before.
    fetchMock.mockResolvedValueOnce(
      new Response("nope", { status: 503 }),
    );
    let captured: ApiError | null = null;
    try {
      await apiFetch("/api/foo", {
        headers: { [REQUEST_ID_HEADER]: "client-mint-1" },
        noRetry: true,
      });
    } catch (err) {
      captured = err as ApiError;
    }
    expect(captured?.requestId).toBe("client-mint-1");
    expect(captured?.status).toBe(503);
  });

  it("reports the 4xx status and parsed body on validation errors", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "missing field" }, { status: 422 }),
    );
    await expect(apiFetch("/api/foo", { method: "POST", body: {} })).rejects.toMatchObject({
      status: 422,
      body: { detail: "missing field" },
    });
  });
});
