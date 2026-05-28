/**
 * Lightweight authenticated fetch helper for endpoints that aren't yet
 * in the OpenAPI spec (currently: billing).
 *
 * The generated `@workspace/api-client-react` client also uses fetch under
 * the hood and shares a session token getter, but it requires a regen step
 * to add new endpoints.  For ad-hoc routes this helper keeps things
 * lightweight while still flowing through the same Clerk JWT.
 */

import { fetchWithRetry, isIdempotent } from "@workspace/retry-fetch";

/**
 * Generate a request id (uuid4 hex, no dashes) attached to every outbound
 * request via the `x-request-id` header.  The backend echoes it on the
 * response and binds it to its log context, so a single id stitches
 * frontend → API → swarm → gates → execution into one greppable trace.
 */
export function newRequestId(): string {
  const cryptoApi: Crypto | undefined =
    typeof globalThis !== "undefined" ? (globalThis as { crypto?: Crypto }).crypto : undefined;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") {
    return cryptoApi.randomUUID().replace(/-/g, "");
  }
  // Fallback — sufficient for correlation, not security.
  let out = "";
  for (let i = 0; i < 32; i += 1) {
    out += Math.floor(Math.random() * 16).toString(16);
  }
  return out;
}

export const REQUEST_ID_HEADER = "x-request-id";

export class ApiError extends Error {
  status: number;
  body: unknown;
  /**
   * Correlation id echoed by the server in the `x-request-id` response
   * header.  Surface this in error UIs so support can paste it into log
   * search and find the full backend trace immediately.
   */
  requestId: string | null;
  constructor(
    message: string,
    status: number,
    body: unknown,
    requestId: string | null = null,
  ) {
    super(message);
    this.status = status;
    this.body = body;
    this.requestId = requestId;
  }
}

/**
 * Pull the `x-request-id` correlation id off any error that carries one.
 * Works with the local `ApiError` above and the generated client's
 * `ApiError` (which exposes the response `headers`).
 */
export function getErrorRequestId(err: unknown): string | null {
  if (err instanceof ApiError) {
    return err.requestId;
  }
  if (err && typeof err === "object") {
    const direct = (err as { requestId?: unknown }).requestId;
    if (typeof direct === "string" && direct !== "") return direct;
    const headers = (err as { headers?: unknown }).headers;
    if (
      headers &&
      typeof (headers as { get?: unknown }).get === "function"
    ) {
      const id = (headers as { get: (name: string) => string | null }).get(
        REQUEST_ID_HEADER,
      );
      if (id && id !== "") return id;
    }
  }
  return null;
}

/**
 * Render an error as a short string suitable for inline error UI / toasts,
 * appending `(Reference: <id>)` when the underlying response carried an
 * `x-request-id` header so the operator can copy the id into log search.
 *
 * `baseOverride` lets callers substitute a more user-friendly summary
 * (e.g. `"Portal failed (HTTP 500)"`) instead of the raw `Error.message`
 * — the reference id is still appended on top.
 */
export function formatErrorMessage(
  err: unknown,
  baseOverride?: string,
): string {
  let base: string;
  if (typeof baseOverride === "string" && baseOverride !== "") {
    base = baseOverride;
  } else if (err instanceof Error) {
    base = err.message;
  } else if (err == null) {
    base = "Request failed";
  } else {
    base = String(err);
  }
  const id = getErrorRequestId(err);
  return id ? `${base} (Reference: ${id})` : base;
}

export type AuthTokenGetter = () => Promise<string | null | undefined>;

export interface ApiFetchOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  getToken?: AuthTokenGetter;
  /**
   * Opt-in retry on a non-idempotent verb (POST/PUT/PATCH/DELETE).
   * Idempotent verbs (GET/HEAD) are retried by default — set
   * ``retryMutation: true`` to extend that to a specific mutation
   * (e.g. an idempotent "POST that re-sends a notification we know is
   * deduped server-side").  Has no effect for GET/HEAD.
   */
  retryMutation?: boolean;
  /**
   * Force-disable retries for this single call.  Useful when the
   * caller wants a hard fail-fast (e.g. a quick liveness probe).
   */
  noRetry?: boolean;
}

/**
 * Resolve a request path against the configured backend base URL.
 *
 * `VITE_API_URL` is the Mac-Studio-cutover knob: when empty (default,
 * Replit deployment) every request uses a relative path so the proxied
 * frontend hits the FastAPI server on the same origin. When set
 * (e.g. `https://api.example.com`) every relative path is prepended
 * with that origin so the same Replit-hosted operator console can be
 * pointed at a self-hosted backend without rebuilding the bundle for
 * a different environment.
 *
 * Absolute URLs (`http://` / `https://`) are returned unchanged so
 * one-off integrations can still call third-party endpoints directly.
 *
 * Read inside the function (not at module scope) so tests can flip the
 * env via `vi.stubEnv("VITE_API_URL", ...)` between assertions.
 */
export function resolveApiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  const raw = (import.meta.env.VITE_API_URL ?? "").toString().trim();
  if (!raw) return path;

  // Validate strictly. A malformed `VITE_API_URL` (typo, missing
  // protocol, accidental whitespace) MUST NOT silently route an
  // authenticated request — `apiFetch` attaches the bearer token —
  // to wherever the URL parser lands. Fall back to the same-origin
  // relative path so a misconfigured deploy degrades to today's
  // behavior instead of leaking credentials.
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    if (typeof console !== "undefined") {
      console.warn(
        `[resolveApiUrl] VITE_API_URL is not a valid URL (${raw!}); ` +
          "falling back to same-origin requests.",
      );
    }
    return path;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    if (typeof console !== "undefined") {
      console.warn(
        `[resolveApiUrl] VITE_API_URL has unsupported protocol ` +
          `(${parsed.protocol}); falling back to same-origin requests.`,
      );
    }
    return path;
  }

  const base = raw.replace(/\/+$/, "");
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${cleanPath}`;
}

export async function apiFetch<T = unknown>(
  path: string,
  opts: ApiFetchOptions = {},
): Promise<T> {
  const {
    body,
    getToken,
    headers: headersInit,
    retryMutation,
    noRetry,
    ...init
  } = opts;
  const url = resolveApiUrl(path);
  const headers = new Headers(headersInit ?? undefined);
  if (body !== undefined && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  if (!headers.has("accept")) {
    headers.set("accept", "application/json");
  }
  if (!headers.has(REQUEST_ID_HEADER)) {
    headers.set(REQUEST_ID_HEADER, newRequestId());
  }
  if (getToken && !headers.has("authorization")) {
    const token = await getToken();
    if (token) headers.set("authorization", `Bearer ${token}`);
  }
  // Retry policy: idempotent verbs retry by default; mutations only
  // when explicitly opted in.  ``noRetry`` is the kill switch.  All
  // calls flow through the shared helper so the dashboard's retry
  // tile gets a single source of truth.
  const method = (init.method ?? "GET").toUpperCase();
  const retry = noRetry
    ? false
    : isIdempotent(method) || retryMutation === true;
  const res = await fetchWithRetry(
    url,
    {
      ...init,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    },
    { retry },
  );
  let parsed: unknown = undefined;
  const text = await res.text();
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!res.ok) {
    throw new ApiError(
      `HTTP ${res.status} ${res.statusText} :: ${path}`,
      res.status,
      parsed,
      res.headers.get(REQUEST_ID_HEADER),
    );
  }
  return parsed as T;
}
