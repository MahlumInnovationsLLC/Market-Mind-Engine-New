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
