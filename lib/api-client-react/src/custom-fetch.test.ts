import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  customFetch,
  setAuthTokenGetter,
  setUnauthorizedHandler,
} from "./custom-fetch";

type FetchMock = ReturnType<typeof vi.fn>;

function jsonResponse(
  status: number,
  body: unknown = null,
  init: ResponseInit = {},
): Response {
  const headers = new Headers(init.headers);
  if (body !== null && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    statusText: init.statusText ?? (status === 200 ? "OK" : "Unauthorized"),
    headers,
  });
}

let originalFetch: typeof fetch;
let fetchMock: FetchMock;

beforeEach(() => {
  originalFetch = globalThis.fetch;
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  setAuthTokenGetter(null);
  setUnauthorizedHandler(null);
  vi.restoreAllMocks();
});

describe("customFetch silent token refresh", () => {
  it("retries once on 401 and succeeds without invoking the unauthorized handler", async () => {
    const tokens = ["stale-token", "fresh-token"];
    const getter = vi.fn(async () => tokens.shift() ?? null);
    setAuthTokenGetter(getter);

    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);

    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { error: "expired" }))
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));

    const result = await customFetch<{ ok: boolean }>("https://api.test/resource");

    expect(result).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onUnauthorized).not.toHaveBeenCalled();

    expect(getter).toHaveBeenCalledTimes(2);
    expect(getter).toHaveBeenNthCalledWith(1, { forceRefresh: false });
    expect(getter).toHaveBeenNthCalledWith(2, { forceRefresh: true });

    const firstHeaders = new Headers(fetchMock.mock.calls[0][1].headers);
    const secondHeaders = new Headers(fetchMock.mock.calls[1][1].headers);
    expect(firstHeaders.get("authorization")).toBe("Bearer stale-token");
    expect(secondHeaders.get("authorization")).toBe("Bearer fresh-token");
  });

  it("invokes the unauthorized handler exactly once after two consecutive 401s", async () => {
    const getter = vi.fn(async () => "any-token");
    setAuthTokenGetter(getter);

    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);

    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { error: "expired" }))
      .mockResolvedValueOnce(jsonResponse(401, { error: "still expired" }));

    await expect(customFetch("https://api.test/resource")).rejects.toBeInstanceOf(
      ApiError,
    );

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(onUnauthorized).toHaveBeenCalledWith({
      status: 401,
      url: "https://api.test/resource",
      method: "GET",
    });
  });

  it("does not retry when no auth getter is registered", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);

    fetchMock.mockResolvedValueOnce(jsonResponse(401, { error: "nope" }));

    await expect(customFetch("https://api.test/resource")).rejects.toBeInstanceOf(
      ApiError,
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("does not retry when the caller supplied their own Authorization header", async () => {
    const getter = vi.fn(async () => "managed-token");
    setAuthTokenGetter(getter);

    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);

    fetchMock.mockResolvedValueOnce(jsonResponse(401, { error: "nope" }));

    await expect(
      customFetch("https://api.test/resource", {
        headers: { authorization: "Bearer caller-supplied" },
      }),
    ).rejects.toBeInstanceOf(ApiError);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(getter).not.toHaveBeenCalled();
    const sentHeaders = new Headers(fetchMock.mock.calls[0][1].headers);
    expect(sentHeaders.get("authorization")).toBe("Bearer caller-supplied");
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("never retries more than once even if the refreshed request also returns 401", async () => {
    const getter = vi.fn(async () => "token");
    setAuthTokenGetter(getter);

    fetchMock
      .mockResolvedValueOnce(jsonResponse(401))
      .mockResolvedValueOnce(jsonResponse(401))
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));

    await expect(customFetch("https://api.test/resource")).rejects.toBeInstanceOf(
      ApiError,
    );

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(getter).toHaveBeenCalledTimes(2);
  });
});
