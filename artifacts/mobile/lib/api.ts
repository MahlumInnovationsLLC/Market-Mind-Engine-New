const DOMAIN = process.env.EXPO_PUBLIC_DOMAIN;

/**
 * Resolve `EXPO_PUBLIC_DOMAIN` into an HTTP base URL.
 *
 * `EXPO_PUBLIC_DOMAIN` can come in three forms in this monorepo
 * (mirrors what `lib/use-websocket.ts` accepts):
 *   - bare host:    "<id>.replit.dev"      (REPLIT_DEV_DOMAIN)
 *   - http origin:  "http://localhost:8080"
 *   - https origin: "https://app.example.com"
 *
 * On Replit the dev domain serves HTTPS so a bare host is upgraded to
 * `https://`. CI boots the API on plain `http://localhost:8080`, so
 * an explicit scheme prefix has to be respected verbatim — otherwise
 * we'd double-prefix and get `https://http://localhost:8080`, which
 * fails TLS and wedges the mobile dashboard tests.
 */
function resolveBaseUrl(domain: string | undefined): string {
  if (!domain) return "";
  if (/^https?:\/\//i.test(domain)) return domain.replace(/\/$/, "");
  return `https://${domain}`;
}

const BASE_URL = resolveBaseUrl(DOMAIN);

let _tokenGetter: (() => Promise<string | null | undefined>) | null = null;

export function setTokenGetter(getter: typeof _tokenGetter): void {
  _tokenGetter = getter;
}

export const REQUEST_ID_HEADER = "x-request-id";

/**
 * Generate a request id (uuid4 hex, no dashes) attached to every outbound
 * request via the `x-request-id` header.  The backend echoes it on the
 * response and binds it to its log context, so a single id stitches
 * mobile → API → swarm → gates → execution into one greppable trace.
 *
 * Mirrors the helper in `artifacts/web/src/lib/api.ts` and the one inside
 * the generated `@workspace/api-client-react` client so every entrypoint
 * the mobile app uses produces the same shape of id.
 */
export function newRequestId(): string {
  const cryptoApi: Crypto | undefined =
    typeof globalThis !== "undefined"
      ? (globalThis as { crypto?: Crypto }).crypto
      : undefined;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") {
    return cryptoApi.randomUUID().replace(/-/g, "");
  }
  let out = "";
  for (let i = 0; i < 32; i += 1) {
    out += Math.floor(Math.random() * 16).toString(16);
  }
  return out;
}

export class ApiError extends Error {
  status: number;
  body: unknown;
  /**
   * The `x-request-id` correlation id we sent on the failing request
   * (and that the backend echoes back on its response). Surface this
   * alongside any user-visible error message so support can grep the
   * server logs for the exact request that failed.
   */
  requestId: string;
  constructor(
    message: string,
    status: number,
    body: unknown,
    requestId: string,
  ) {
    super(`${message} [request_id=${requestId}]`);
    this.status = status;
    this.body = body;
    this.requestId = requestId;
  }
}

export async function apiFetch<T = unknown>(
  path: string,
  opts: { method?: string; body?: unknown; headers?: Record<string, string> } = {},
): Promise<T> {
  const { method = "GET", body, headers: extra = {} } = opts;
  const headers: Record<string, string> = { accept: "application/json", ...extra };
  if (body !== undefined) headers["content-type"] = "application/json";
  if (_tokenGetter) {
    const t = await _tokenGetter();
    if (t) headers["authorization"] = `Bearer ${t}`;
  }
  // Use whatever id the caller already attached (so a single user action
  // that fans out into several calls can share an id) — otherwise mint one.
  const requestId =
    Object.entries(headers).find(
      ([k]) => k.toLowerCase() === REQUEST_ID_HEADER,
    )?.[1] ?? newRequestId();
  headers[REQUEST_ID_HEADER] = requestId;

  const url = path.startsWith("http") ? path : `${BASE_URL}${path}`;
  const res = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  // The server echoes the id back; prefer that (in case middleware
  // rewrote it) but fall back to the one we sent so we always have one.
  const echoedId = res.headers.get(REQUEST_ID_HEADER) || requestId;

  const text = await res.text();
  let parsed: unknown = text;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      /* keep text */
    }
  }
  if (!res.ok) {
    // Log to the device console too so support can pull the id from
    // `npx react-native log-*` / Metro / Sentry breadcrumbs even if the
    // UI only shows the user-friendly message.
    console.warn(
      `[apiFetch] ${method} ${path} failed: HTTP ${res.status} (request_id=${echoedId})`,
    );
    throw new ApiError(
      `HTTP ${res.status} :: ${path}`,
      res.status,
      parsed,
      echoedId,
    );
  }
  return parsed as T;
}
