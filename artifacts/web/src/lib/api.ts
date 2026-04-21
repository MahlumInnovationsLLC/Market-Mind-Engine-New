/**
 * Lightweight authenticated fetch helper for endpoints that aren't yet
 * in the OpenAPI spec (currently: billing).
 *
 * The generated `@workspace/api-client-react` client also uses fetch under
 * the hood and shares a session token getter, but it requires a regen step
 * to add new endpoints.  For ad-hoc routes this helper keeps things
 * lightweight while still flowing through the same Clerk JWT.
 */

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
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
    );
  }
  return parsed as T;
}
