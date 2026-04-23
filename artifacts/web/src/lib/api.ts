/**
 * Lightweight authenticated fetch helper for endpoints that aren't yet
 * in the OpenAPI spec (currently: billing).
 *
 * The generated `@workspace/api-client-react` client also uses fetch under
 * the hood and shares a session token getter, but it requires a regen step
 * to add new endpoints.  For ad-hoc routes this helper keeps things
 * lightweight while still flowing through the same Clerk JWT.
 */

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
}

export async function apiFetch<T = unknown>(
  path: string,
  opts: ApiFetchOptions = {},
): Promise<T> {
  const { body, getToken, headers: headersInit, ...init } = opts;
  const url = path.startsWith("http") ? path : path;
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
  const res = await fetch(url, {
    ...init,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
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
