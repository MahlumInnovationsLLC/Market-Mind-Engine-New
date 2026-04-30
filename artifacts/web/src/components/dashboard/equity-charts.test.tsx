import { describe, expect, it } from "vitest";

import { fmtShortDate } from "./equity-charts";

describe("fmtShortDate", () => {
  it("returns the em-dash for an empty string", () => {
    expect(fmtShortDate("")).toBe("—");
  });

  it("returns the input unchanged when it is not a YYYY-MM-DD string", () => {
    expect(fmtShortDate("not-a-date")).toBe("not-a-date");
    expect(fmtShortDate("2026/02/20")).toBe("2026/02/20");
    expect(fmtShortDate("2026-02")).toBe("2026-02");
  });

  it("returns the input unchanged when components are non-numeric", () => {
    expect(fmtShortDate("YYYY-MM-DD")).toBe("YYYY-MM-DD");
  });

  it("renders the same calendar day the backend bucketed into, regardless of the host timezone", () => {
    // Regression for the Feb-20 / Feb-19 drift: the backend
    // (`get_public_equity_curve`) emits bucket keys as bare
    // `YYYY-MM-DD` strings derived from `date_trunc('day', executed_at
    // at time zone 'utc')`.  Parsing them via
    // `new Date("YYYY-MM-DDT00:00:00Z")` and then formatting with
    // `toLocaleDateString` made any user west of UTC see the *previous*
    // day in the chart tooltip — e.g. a real Feb-20 system loss
    // rendered as "Feb 19" in Pacific time, which made it look like a
    // genuine Feb-19 trade had been back-dated.  We assert the *month
    // and day-of-month* match the input components without round-trip
    // through any timezone arithmetic.
    const out = fmtShortDate("2026-02-20");
    // We cannot hard-code "Feb 20" because Vitest can run with any
    // host locale; instead we re-derive the expected label the same
    // way a healthy implementation must: from the *local* date
    // constructed from the literal components.
    const expected = new Date(2026, 1, 20).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
    expect(out).toBe(expected);
  });

  it("does not silently drift when the bucket key is January 1st", () => {
    // Jan 1 is the most-likely candidate for a year-rollover bug if a
    // future refactor reintroduces UTC parsing in a non-UTC locale.
    const out = fmtShortDate("2026-01-01");
    const expected = new Date(2026, 0, 1).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
    expect(out).toBe(expected);
  });
});
