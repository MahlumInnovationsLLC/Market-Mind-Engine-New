/**
 * Shared retry-with-backoff wrapper around `fetch`.
 *
 * Both `artifacts/web/src/lib/api.ts` and `artifacts/mobile/lib/api.ts`
 * delegate through this so the retry policy is identical across the two
 * front-ends.  Keeping it in one place also means the dashboard's
 * "real retries vs. user-facing failures" telemetry has a single
 * observer to hook into (`setRetryObserver`).
 *
 * Policy (intentionally narrow — Task #924):
 *
 *   * Idempotent verbs (GET, HEAD) auto-retry by default.
 *   * Non-idempotent verbs (POST, PUT, PATCH, DELETE) DO NOT auto-retry
 *     unless the caller opts in with `retryMutation: true`.
 *   * Retried responses: 5xx, plus the two transient 4xx codes
 *     (408 Request Timeout, 429 Too Many Requests).  All other 4xx
 *     responses are treated as the server's final answer.
 *   * Retried errors: any thrown error from `fetch` itself
 *     (DNS, TCP, TLS, connection-reset, per-attempt timeout).
 *     `AbortError` from the *caller's* signal short-circuits.
 *   * Up to 3 total attempts, exponential backoff
 *     (BASE=200ms, factor 2 → 200/400/800ms), ±25% jitter,
 *     capped at 5s total back-off time across the whole call.
 */

export interface RetryOptions {
  /**
   * Override the default idempotency rule.  When set, this beats the
   * verb-based default (GET/HEAD → true, everything else → false).
   * Mutations can opt in via `retryMutation: true` here OR via the
   * wrapper's `retryMutation` shortcut (see `apiFetch` callers).
   */
  retry?: boolean;
  /** Max total attempts including the first. Default 3. */
  maxAttempts?: number;
  /** First back-off in ms.  Default 200. Doubled each retry. */
  baseDelayMs?: number;
  /** Cap on the per-retry sleep (before jitter).  Default 5000. */
  maxDelayMs?: number;
  /** Cap on the cumulative back-off time across all retries.  Default 5000. */
  totalBudgetMs?: number;
  /** Per-attempt timeout in ms.  Default: no per-attempt timeout. */
  attemptTimeoutMs?: number;
  /**
   * Deterministic jitter source for tests.  Production passes `Math.random`
   * (the default).  Returns a value in [0, 1).
   */
  rng?: () => number;
  /**
   * Sleep implementation override for tests.  Receives the millisecond
   * delay; should return a promise that resolves after that delay.
   * Default: `setTimeout`.
   */
  sleep?: (ms: number) => Promise<void>;
}

/**
 * Counter shape passed to the retry observer.  The dashboard's
 * "request-failure" tile bumps on `final_failure`, "transient retries"
 * tile bumps on `retry`.
 */
export type RetryObserverEvent =
  | { kind: "retry"; attempt: number; reason: RetryReason; url: string; method: string }
  | { kind: "final_failure"; attempts: number; reason: RetryReason; url: string; method: string }
  | { kind: "success_after_retry"; attempts: number; url: string; method: string };

export type RetryReason =
  | { type: "network"; error: unknown }
  | { type: "status"; status: number }
  | { type: "timeout" }
  | { type: "budget_exhausted" };

export type RetryObserver = (event: RetryObserverEvent) => void;

let _observer: RetryObserver | null = null;

/**
 * Register a process-wide observer notified on every retry, final failure
 * and successful retry.  Pass `null` to clear.  Errors thrown inside the
 * observer are swallowed so observability never breaks the request.
 *
 * Used by the dashboard request-failure tile to distinguish "transient
 * blip we recovered from" (success_after_retry) from "user actually saw
 * an error" (final_failure).
 */
export function setRetryObserver(observer: RetryObserver | null): void {
  _observer = observer;
}

function emit(event: RetryObserverEvent): void {
  if (!_observer) return;
  try {
    _observer(event);
  } catch {
    /* observability must never break the request */
  }
}

const IDEMPOTENT_METHODS = new Set(["GET", "HEAD"]);
const RETRYABLE_4XX = new Set([408, 429]);

function isRetryableStatus(status: number): boolean {
  if (status >= 500 && status <= 599) return true;
  if (RETRYABLE_4XX.has(status)) return true;
  return false;
}

function defaultRetryForMethod(method: string): boolean {
  return IDEMPOTENT_METHODS.has(method.toUpperCase());
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Throw an `AbortError`-shaped DOMException-or-Error. Lets the wrapper
 * propagate a caller cancel the same way `fetch()` itself would: with
 * an error whose `.name === "AbortError"` so existing `catch` blocks
 * keep working uniformly.
 */
function abortError(reason?: unknown): Error {
  if (
    typeof DOMException !== "undefined" &&
    typeof DOMException === "function"
  ) {
    try {
      return new DOMException("Aborted", "AbortError");
    } catch {
      /* fall through */
    }
  }
  const err = new Error(
    reason instanceof Error ? reason.message : "Aborted",
  ) as Error & { name: string };
  err.name = "AbortError";
  return err;
}

/**
 * Sleep that resolves after ``ms`` OR rejects immediately when the
 * caller's AbortSignal fires.  Without this, a caller cancel issued
 * during back-off would be silently delayed until the timer expires
 * (and possibly carry the loop into another fetch attempt) — a
 * correctness bug the architect review caught on the first pass.
 */
function abortableSleep(
  ms: number,
  signal: AbortSignal | null,
  sleep: (ms: number) => Promise<void>,
): Promise<void> {
  if (!signal) return sleep(ms);
  if (signal.aborted) return Promise.reject(abortError(signal.reason));
  return new Promise((resolve, reject) => {
    let settled = false;
    const onAbort = () => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      reject(abortError(signal.reason));
    };
    signal.addEventListener("abort", onAbort, { once: true });
    sleep(ms).then(
      () => {
        if (settled) return;
        settled = true;
        signal.removeEventListener("abort", onAbort);
        resolve();
      },
      (err) => {
        if (settled) return;
        settled = true;
        signal.removeEventListener("abort", onAbort);
        reject(err);
      },
    );
  });
}

function resolveMethod(init: RequestInit | undefined): string {
  return (init?.method ?? "GET").toUpperCase();
}

function resolveUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (typeof URL !== "undefined" && input instanceof URL) return input.toString();
  return (input as Request).url;
}

/**
 * Was the error thrown by `fetch` an abort triggered by the caller's
 * own AbortSignal (vs. our per-attempt timeout)?  Caller-initiated
 * aborts must NOT be retried — that's a user/code decision we have
 * to respect.
 */
function isCallerAbort(error: unknown, callerSignal: AbortSignal | undefined | null): boolean {
  if (!callerSignal || !callerSignal.aborted) return false;
  if (!(error instanceof Error)) return false;
  // DOMException("AbortError") and the polyfilled AbortError both show
  // up as `error.name === "AbortError"`.
  return error.name === "AbortError";
}

/**
 * Compute the next back-off in ms, capped by the per-step ceiling, with
 * ±25% jitter.  Exposed for tests.
 */
export function computeBackoff(
  attempt: number,
  baseDelayMs: number,
  maxDelayMs: number,
  rng: () => number,
): number {
  // attempt is 1-based: first retry uses base * 2^0 = base.
  const raw = baseDelayMs * Math.pow(2, Math.max(0, attempt - 1));
  const capped = Math.min(raw, maxDelayMs);
  // Symmetric jitter in [-25%, +25%].
  const jitterFactor = 1 + (rng() - 0.5) * 0.5;
  return Math.max(0, Math.round(capped * jitterFactor));
}

/**
 * `fetch` wrapper that adds idempotency-aware retry with exponential
 * back-off and a total-time budget.  Returns the final `Response`
 * (which may itself be a 4xx/5xx — the wrapper does NOT throw on
 * non-OK; that's the caller's job).  Throws only when:
 *
 *   * the caller's `signal` aborted the whole call,
 *   * every attempt failed with a network error AND the budget is
 *     exhausted (in which case the last error is re-thrown).
 */
export async function fetchWithRetry(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  opts: RetryOptions = {},
): Promise<Response> {
  const method = resolveMethod(init);
  const url = resolveUrl(input);
  const retry = opts.retry ?? defaultRetryForMethod(method);
  const maxAttempts = Math.max(1, opts.maxAttempts ?? 3);
  const baseDelayMs = opts.baseDelayMs ?? 200;
  const maxDelayMs = opts.maxDelayMs ?? 5000;
  const totalBudgetMs = opts.totalBudgetMs ?? 5000;
  const attemptTimeoutMs = opts.attemptTimeoutMs;
  const rng = opts.rng ?? Math.random;
  const sleep = opts.sleep ?? defaultSleep;

  const callerSignal = init?.signal ?? null;

  let attempt = 0;
  let budgetSpent = 0;
  let lastError: unknown = null;
  let lastResponse: Response | null = null;

  // If retries are disabled, the loop runs exactly once — the policy
  // collapses to "single shot, surface what fetch returns".
  const effectiveMax = retry ? maxAttempts : 1;

  while (attempt < effectiveMax) {
    attempt += 1;

    // Per-attempt timeout controller that ALSO mirrors the caller's
    // signal so an outer abort cancels the in-flight attempt too.
    let perAttemptTimer: ReturnType<typeof setTimeout> | null = null;
    let combinedSignal: AbortSignal | undefined = callerSignal ?? undefined;
    let perAttemptAborted = false;
    let attemptController: AbortController | null = null;

    if (attemptTimeoutMs !== undefined || callerSignal) {
      attemptController = new AbortController();
      combinedSignal = attemptController.signal;
      if (callerSignal) {
        if (callerSignal.aborted) {
          attemptController.abort();
        } else {
          const onAbort = () => attemptController!.abort();
          callerSignal.addEventListener("abort", onAbort, { once: true });
        }
      }
      if (attemptTimeoutMs !== undefined) {
        perAttemptTimer = setTimeout(() => {
          perAttemptAborted = true;
          attemptController!.abort();
        }, attemptTimeoutMs);
      }
    }

    try {
      const response = await fetch(input, { ...(init ?? {}), signal: combinedSignal });
      if (perAttemptTimer) clearTimeout(perAttemptTimer);
      lastResponse = response;

      if (response.ok || !retry || !isRetryableStatus(response.status)) {
        if (attempt > 1 && response.ok) {
          emit({ kind: "success_after_retry", attempts: attempt, url, method });
        }
        return response;
      }

      // Retryable status — decide whether we have another shot.
      const isLast = attempt >= effectiveMax;
      if (isLast) {
        emit({
          kind: "final_failure",
          attempts: attempt,
          reason: { type: "status", status: response.status },
          url,
          method,
        });
        return response;
      }
      emit({
        kind: "retry",
        attempt,
        reason: { type: "status", status: response.status },
        url,
        method,
      });
      // Drain the body so the connection can be released before we sleep.
      try {
        await response.clone().text();
      } catch {
        /* ignore */
      }
    } catch (error) {
      if (perAttemptTimer) clearTimeout(perAttemptTimer);
      lastError = error;

      if (isCallerAbort(error, callerSignal)) {
        // The caller asked us to stop — never retry that.
        throw error;
      }

      const isLast = attempt >= effectiveMax;
      const reason: RetryReason = perAttemptAborted
        ? { type: "timeout" }
        : { type: "network", error };

      if (!retry || isLast) {
        emit({ kind: "final_failure", attempts: attempt, reason, url, method });
        throw error;
      }
      emit({ kind: "retry", attempt, reason, url, method });
    }

    // Sleep before the next attempt — but only if we still have budget.
    const delay = computeBackoff(attempt, baseDelayMs, maxDelayMs, rng);
    if (budgetSpent + delay > totalBudgetMs) {
      emit({
        kind: "final_failure",
        attempts: attempt,
        reason: { type: "budget_exhausted" },
        url,
        method,
      });
      if (lastResponse) return lastResponse;
      throw lastError ?? new Error("fetchWithRetry: retry budget exhausted");
    }
    budgetSpent += delay;
    // Caller cancel must short-circuit the back-off immediately — we
    // can't keep sleeping past an abort, otherwise the next loop
    // iteration would issue another fetch the caller already gave up
    // on (architect review #924).
    await abortableSleep(delay, callerSignal, sleep);
  }

  // Loop exited without returning — should be unreachable in practice
  // because every branch above returns or throws, but TypeScript needs it.
  /* c8 ignore next 5 */
  if (lastResponse) return lastResponse;
  throw lastError ?? new Error("fetchWithRetry: no attempts made");
}

/** Visible to callers that want to keep their own policy in sync. */
export function isIdempotent(method: string): boolean {
  return defaultRetryForMethod(method);
}
