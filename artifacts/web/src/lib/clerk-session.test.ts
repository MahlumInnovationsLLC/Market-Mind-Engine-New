/**
 * Unit tests for the cookie-based "is the visitor signed in?" heuristic
 * used by the marketing surface to render the right CTA without waiting
 * for the full Clerk SDK chunk to load.
 *
 * The helper just sniffs the non-secret `__client_uat` cookie that
 * Clerk itself sets, so we drive it by mutating `document.cookie`
 * inside jsdom. Anything that decides UI gating off this output (the
 * marketing nav CTA, the pricing page upgrade button, etc.) needs the
 * branches below to stay correct.
 */
import { afterEach, describe, expect, it } from "vitest";
import { hasClerkSession } from "./clerk-session";

function setCookies(cookies: string[]): void {
  // jsdom lets us write each `key=value` pair separately to compose a
  // cookie jar — assigning the joined string just stores it as one
  // cookie, which doesn't match real browser behaviour.
  for (const c of cookies) {
    document.cookie = c;
  }
}

function clearCookies(): void {
  for (const raw of document.cookie.split(";")) {
    const [name] = raw.trim().split("=");
    if (name) {
      document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    }
  }
}

afterEach(() => {
  clearCookies();
});

describe("hasClerkSession", () => {
  it("returns false when no cookies are set at all", () => {
    expect(hasClerkSession()).toBe(false);
  });

  it("returns false when __client_uat is the signed-out sentinel (0)", () => {
    setCookies(["__client_uat=0"]);
    expect(hasClerkSession()).toBe(false);
  });

  it("returns true when __client_uat is a positive unix timestamp", () => {
    setCookies(["__client_uat=1717000000"]);
    expect(hasClerkSession()).toBe(true);
  });

  it("recognises Clerk's per-instance suffixed cookie name", () => {
    // Clerk multi-instance setups suffix the cookie (e.g.
    // `__client_uat_abc123`) — the helper has to match both.
    setCookies(["__client_uat_abc123=1717000000"]);
    expect(hasClerkSession()).toBe(true);
  });

  it("ignores unrelated cookies even when other state is present", () => {
    setCookies([
      "theme=dark",
      "consent=true",
      "_ga=GA1.2.123",
    ]);
    expect(hasClerkSession()).toBe(false);
  });

  it("returns false when the cookie value is non-numeric garbage", () => {
    setCookies(["__client_uat=not-a-number"]);
    expect(hasClerkSession()).toBe(false);
  });

  it("returns false when the cookie value is negative", () => {
    setCookies(["__client_uat=-42"]);
    expect(hasClerkSession()).toBe(false);
  });

  it("tolerates extra whitespace between cookie pairs", () => {
    // Real `document.cookie` strings come back with `;` separators and
    // a leading space; the helper's `.trim()` is the reason this works.
    document.cookie = "theme=dark";
    document.cookie = "__client_uat=1717000000";
    expect(hasClerkSession()).toBe(true);
  });
});
