/**
 * Unit tests for the curated IANA timezone list and search helpers
 * powering the mobile quiet-hours picker.
 *
 * The component layer is exercised by the Playwright suite; these
 * cover the pure helpers that decide which zones the picker shows
 * and what the selected zone's offset / city display look like.
 */
import {
  TIMEZONES,
  findTimezone,
  isInQuietHours,
  offsetForZone,
  searchTimezones,
} from "@/lib/timezones";

describe("TIMEZONES catalog", () => {
  it("includes UTC and a representative spread of regions", () => {
    const ids = TIMEZONES.map((t) => t.id);
    expect(ids).toContain("UTC");
    expect(ids).toContain("America/New_York");
    expect(ids).toContain("Europe/London");
    expect(ids).toContain("Asia/Tokyo");
    expect(ids).toContain("Australia/Sydney");
    expect(ids).toContain("Africa/Johannesburg");
    expect(ids).toContain("Pacific/Auckland");
  });

  it("has unique IDs", () => {
    const ids = TIMEZONES.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("uses only IANA-resolvable zones", () => {
    for (const zone of TIMEZONES) {
      expect(() =>
        new Intl.DateTimeFormat("en-US", { timeZone: zone.id }).format(
          new Date(),
        ),
      ).not.toThrow();
    }
  });
});

describe("findTimezone", () => {
  it("returns the entry for a known IANA id", () => {
    const found = findTimezone("Europe/London");
    expect(found?.city).toBe("London");
    expect(found?.region).toBe("Europe");
  });

  it("returns null for unknown or empty input", () => {
    expect(findTimezone(null)).toBeNull();
    expect(findTimezone("")).toBeNull();
    expect(findTimezone("Mars/Olympus")).toBeNull();
  });
});

describe("searchTimezones", () => {
  it("returns the full list for an empty query", () => {
    expect(searchTimezones("")).toHaveLength(TIMEZONES.length);
    expect(searchTimezones("   ")).toHaveLength(TIMEZONES.length);
  });

  it("matches against IANA id case-insensitively", () => {
    const results = searchTimezones("new_york");
    expect(results.map((r) => r.id)).toContain("America/New_York");
  });

  it("matches against the friendly city name", () => {
    const results = searchTimezones("tokyo");
    expect(results.map((r) => r.id)).toContain("Asia/Tokyo");
  });

  it("matches IANA city words even when the user types a space", () => {
    const results = searchTimezones("los angeles");
    expect(results.map((r) => r.id)).toContain("America/Los_Angeles");
  });

  it("matches against region", () => {
    const results = searchTimezones("europe");
    expect(results.length).toBeGreaterThan(5);
    for (const r of results) {
      expect(r.region.toLowerCase()).toContain("europe");
    }
  });

  it("returns no rows when nothing matches", () => {
    expect(searchTimezones("zzz_nothing_matches")).toEqual([]);
  });
});

describe("isInQuietHours", () => {
  // 2026-04-26 04:00 UTC -> 00:00 in America/New_York (EDT, UTC-04:00)
  // and 13:00 in Asia/Tokyo (UTC+09:00). At this instant a 22 -> 7
  // overnight window catches the NYC user but not the Tokyo user —
  // the whole reason the chosen zone matters.
  const nycMidnightUtc = new Date("2026-04-26T04:00:00Z");
  // 2026-04-26 18:30 UTC -> 14:30 in America/New_York (well outside
  // a 22 -> 7 overnight window).
  const dayUtc = new Date("2026-04-26T18:30:00Z");

  it("returns true for an overnight window when the local hour is past the start", () => {
    expect(isInQuietHours(22, 7, "America/New_York", nycMidnightUtc)).toBe(
      true,
    );
  });

  it("returns false when the same instant is daytime in a different zone", () => {
    // 13:00 Tokyo — clearly outside [22, 24) ∪ [0, 7).
    expect(isInQuietHours(22, 7, "Asia/Tokyo", nycMidnightUtc)).toBe(false);
  });

  it("returns false during the day for an overnight window", () => {
    expect(isInQuietHours(22, 7, "America/New_York", dayUtc)).toBe(false);
  });

  it("handles a same-day window (start < end)", () => {
    // 14:30 NYC is inside [9, 17)
    expect(isInQuietHours(9, 17, "America/New_York", dayUtc)).toBe(true);
    // and outside [18, 23)
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

describe("offsetForZone", () => {
  it("formats a non-empty UTC offset for known zones", () => {
    const off = offsetForZone("UTC");
    expect(off).toMatch(/GMT/);
  });

  it("returns an offset string for any zone in the catalog", () => {
    for (const zone of TIMEZONES) {
      expect(offsetForZone(zone.id)).not.toBe("");
    }
  });

  it("returns empty string for an invalid zone instead of throwing", () => {
    expect(offsetForZone("Mars/Olympus")).toBe("");
  });
});
