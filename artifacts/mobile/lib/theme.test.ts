/**
 * Unit tests for the mobile design-token palette resolver.
 *
 * `getPalette` is the single entry point every mobile screen / component
 * uses to pick its colour set (via `useColors`), so a regression in the
 * dark/light routing here would invert colour-mode for the entire app.
 *
 * The tests below also pin the *shape* of the palettes so a sloppy
 * rename (e.g. dropping `accentRed` while bumping the cyberpunk theme)
 * fails the suite instead of silently stripping the colour out of the
 * dashboard.
 */
import { dark, getPalette, glow, light, radii, theme, typography } from "@/lib/theme";

describe("getPalette routing", () => {
  it("returns the dark palette by default (no mode)", () => {
    expect(getPalette(undefined)).toBe(dark);
    expect(getPalette(null)).toBe(dark);
  });

  it("returns the dark palette when explicitly asked", () => {
    expect(getPalette("dark")).toBe(dark);
  });

  it("returns the light palette when asked", () => {
    expect(getPalette("light")).toBe(light);
  });

  it("dark and light palettes use distinct background colours", () => {
    expect(dark.background).not.toBe(light.background);
    expect(dark.foreground).not.toBe(light.foreground);
  });
});

describe("palette shape parity", () => {
  it("dark and light palettes expose the same set of token keys", () => {
    expect(Object.keys(dark).sort()).toEqual(Object.keys(light).sort());
  });

  const REQUIRED_TOKENS = [
    "background",
    "foreground",
    "card",
    "cardForeground",
    "primary",
    "primaryForeground",
    "secondary",
    "secondaryForeground",
    "muted",
    "mutedForeground",
    "accent",
    "accentForeground",
    "destructive",
    "destructiveForeground",
    "success",
    "warning",
    "border",
    "input",
    "borderSubtle",
    "borderGlow",
    "navy600",
    "navy700",
    "navy800",
    "navy850",
    "navy900",
    "navy950",
    "accentCyan",
    "accentBlue",
    "accentPurple",
    "accentMagenta",
    "accentGreen",
    "accentRed",
    "accentAmber",
    "glowCyan",
    "glowGreen",
    "glowRed",
    "glowPurple",
    "text",
    "tint",
  ] as const;

  it.each(REQUIRED_TOKENS)("dark palette defines '%s'", (token) => {
    expect(typeof dark[token]).toBe("string");
    expect(dark[token]).not.toBe("");
  });

  it.each(REQUIRED_TOKENS)("light palette defines '%s'", (token) => {
    expect(typeof light[token]).toBe("string");
    expect(light[token]).not.toBe("");
  });

  it("backwards-compat aliases mirror the canonical tokens", () => {
    // `text`/`tint` are legacy aliases used by older screens; they must
    // stay in sync with `foreground`/`primary` so the two ways of
    // pulling the same colour produce identical pixels.
    expect(dark.text).toBe(dark.foreground);
    expect(dark.tint).toBe(dark.primary);
    expect(light.text).toBe(light.foreground);
    expect(light.tint).toBe(light.primary);
  });
});

describe("typography scale", () => {
  it("exports a sensible size ladder (xs < sm < base < md < lg < xl < xxl < display)", () => {
    const ladder = [
      typography.size.xs,
      typography.size.sm,
      typography.size.base,
      typography.size.md,
      typography.size.lg,
      typography.size.xl,
      typography.size.xxl,
      typography.size.display,
    ];
    for (let i = 1; i < ladder.length; i += 1) {
      expect(ladder[i]).toBeGreaterThan(ladder[i - 1]);
    }
  });

  it("references the four Inter weights mobile screens render in", () => {
    expect(typography.family.regular).toMatch(/Inter/);
    expect(typography.family.medium).toMatch(/Inter/);
    expect(typography.family.semibold).toMatch(/Inter/);
    expect(typography.family.bold).toMatch(/Inter/);
  });
});

describe("radii", () => {
  it("keeps `none` at zero (cyberpunk sharp edges) and `pill` very large", () => {
    expect(radii.none).toBe(0);
    expect(radii.pill).toBeGreaterThanOrEqual(999);
  });
});

describe("glow recipes", () => {
  it("expose the four shadow tints used by the dashboard cards", () => {
    expect(glow.cyan.shadowColor).toBeTruthy();
    expect(glow.green.shadowColor).toBeTruthy();
    expect(glow.red.shadowColor).toBeTruthy();
    expect(glow.purple.shadowColor).toBeTruthy();
  });

  it("each recipe carries a non-zero elevation so Android renders the shadow", () => {
    for (const tint of ["cyan", "green", "red", "purple"] as const) {
      expect(glow[tint].elevation).toBeGreaterThan(0);
      expect(glow[tint].shadowRadius).toBeGreaterThan(0);
    }
  });
});

describe("aggregated `theme` export", () => {
  it("bundles palettes + typography + radii + glow under a single object", () => {
    expect(theme.palettes.dark).toBe(dark);
    expect(theme.palettes.light).toBe(light);
    expect(theme.typography).toBe(typography);
    expect(theme.radii).toBe(radii);
    expect(theme.glow).toBe(glow);
  });
});
