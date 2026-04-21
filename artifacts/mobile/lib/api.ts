const DOMAIN = process.env.EXPO_PUBLIC_DOMAIN;
const BASE_URL = DOMAIN ? `https://${DOMAIN}` : "";

let _tokenGetter: (() => Promise<string | null | undefined>) | null = null;

export function setTokenGetter(getter: typeof _tokenGetter): void {
  _tokenGetter = getter;
}

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
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
  const url = path.startsWith("http") ? path : `${BASE_URL}${path}`;
  const res = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
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
    throw new ApiError(`HTTP ${res.status} :: ${path}`, res.status, parsed);
  }
  return parsed as T;
}
