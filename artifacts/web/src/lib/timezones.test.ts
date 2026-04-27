/**
 * Unit tests for the web timezone helpers backing the quiet-hours
 * surfaces — the Profile page hint and the recent-alerts feed
 * annotations (task #629). Mirrors the mobile coverage in
 * `artifacts/mobile/lib/timezones.test.ts`.
 */
import { describe, it, expect } from "vitest";

import { isInQuietHours, offsetForZone } from "./timezones";

describe("offsetForZone", () => {
  it("formats a non-empty UTC offset for UTC", () => {
    const off = offsetForZone("UTC");
    expect(off).toMatch(/GMT/);
  });

  it("returns a recognisable offset for a well-known zone", () => {
    expect(offsetForZone("America/New_York")).toMatch(/GMT/);
    expect(offsetForZone("Europe/London")).toMatch(/GMT/);
  });

  it("returns empty string for an invalid zone instead of throwing", () => {
    expect(offsetForZone("Mars/Olympus")).toBe("");
  });
});

describe("isInQuietHours", () => {
  // 2026-04-26 04:00 UTC -> 00:00 in America/New_York (EDT, UTC-04:00)
  // and 13:00 in Asia/Tokyo (UTC+09:00).
  const nycMidnightUtc = new Date("2026-04-26T04:00:00Z");
  // 2026-04-26 18:30 UTC -> 14:30 in America/New_York.
  const dayUtc = new Date("2026-04-26T18:30:00Z");

  it("returns true for an overnight window when local hour is past the start", () => {
    expect(isInQuietHours(22, 7, "America/New_York", nycMidnightUtc)).toBe(
      true,
    );
  });

  it("returns false when the same instant is daytime in a different zone", () => {
    expect(isInQuietHours(22, 7, "Asia/Tokyo", nycMidnightUtc)).toBe(false);
  });

  it("returns false during the day for an overnight window", () => {
    expect(isInQuietHours(22, 7, "America/New_York", dayUtc)).toBe(false);
  });

  it("handles a same-day window (start < end)", () => {
    expect(isInQuietHours(9, 17, "America/New_York", dayUtc)).toBe(true);
    expect(isInQuietHours(18, 23, "America/New_York", dayUtc)).toBe(false);
  });

  it("treats equal start/end as no window", () => {
    expect(isInQuietHours(10, 10, "America/New_York", dayUtc)).toBe(false);
  });

  it("returns false when any of start/end/zone is missing", () => {
    expect(isInQuietHours(null, 7, "America/New_York", dayUtc)).toBe(false);
    expect(isInQuietHours(22, null, "America/New_York", dayUtc)).toBe(false);
    expect(isInQuietHours(22, 7, null, dayUtc)).toBe(false);
    expect(isInQuietHours(22, 7, "", dayUtc)).toBe(false);
  });

  it("returns false for out-of-range or non-integer hours", () => {
    expect(isInQuietHours(-1, 7, "America/New_York", dayUtc)).toBe(false);
    expect(isInQuietHours(22, 24, "America/New_York", dayUtc)).toBe(false);
    expect(isInQuietHours(22.5, 7, "America/New_York", dayUtc)).toBe(false);
  });

  it("returns false for an invalid zone instead of throwing", () => {
    expect(isInQuietHours(22, 7, "Mars/Olympus", dayUtc)).toBe(false);
  });
});
