/**
 * Mobile design tokens — mirrors the web cyberpunk-finance palette.
 *
 * The web side defines tokens in `artifacts/web/src/index.css` as HSL
 * triples; we resolve them to hex/rgba once here so React Native can
 * consume them without a CSS variable runtime. Keep the two in sync —
 * this is the only place mobile colors are defined.
 *
 * Dark is the default operator palette. Light is used when the device
 * is in light appearance mode.
 */

export type ThemeMode = "dark" | "light";

export interface Palette {
  background: string;
  foreground: string;

  card: string;
  cardForeground: string;

  primary: string;
  primaryForeground: string;

  secondary: string;
  secondaryForeground: string;

  muted: string;
  mutedForeground: string;

  accent: string;
  accentForeground: string;

  destructive: string;
  destructiveForeground: string;

  success: string;
  warning: string;

  border: string;
  input: string;
  borderSubtle: string;
  borderGlow: string;

  // Cyberpunk navy ramp
  navy600: string;
  navy700: string;
  navy800: string;
  navy850: string;
  navy900: string;
  navy950: string;

  // Bright accent palette
  accentCyan: string;
  accentBlue: string;
  accentPurple: string;
  accentMagenta: string;
  accentGreen: string;
  accentRed: string;
  accentAmber: string;

  // Glow shadow color tokens (consumed via shadowColor + elevation)
  glowCyan: string;
  glowGreen: string;
  glowRed: string;
  glowPurple: string;

  // Backwards-compat aliases (legacy tokens)
  text: string;
  tint: string;
}

export const dark: Palette = {
  // Mahlum-inspired deep navy background, matched to web :root.
  background: "#080d17",
  foreground: "#f0f4fa",

  card: "#0d1325",
  cardForeground: "#f0f4fa",

  primary: "#1de8e8",
  primaryForeground: "#06121a",

  secondary: "#121a2c",
  secondaryForeground: "#f0f4fa",

  muted: "#101a2f",
  mutedForeground: "#9aa6bd",

  accent: "#1de8e8",
  accentForeground: "#06121a",

  destructive: "#ff1744",
  destructiveForeground: "#ffffff",

  success: "#00e676",
  warning: "#f59e0b",

  border: "#1c2640",
  input: "#1c2640",
  borderSubtle: "rgba(255,255,255,0.06)",
  borderGlow: "rgba(0,229,255,0.30)",

  navy600: "#2a3658",
  navy700: "#202a45",
  navy800: "#161e33",
  navy850: "#10182a",
  navy900: "#0c1322",
  navy950: "#080d17",

  accentCyan: "#00e5ff",
  accentBlue: "#3399ff",
  accentPurple: "#a974ff",
  accentMagenta: "#ff4ec5",
  accentGreen: "#00e676",
  accentRed: "#ff1744",
  accentAmber: "#ffb020",

  glowCyan: "rgba(0,229,255,0.45)",
  glowGreen: "rgba(0,230,118,0.45)",
  glowRed: "rgba(255,23,68,0.45)",
  glowPurple: "rgba(169,116,255,0.45)",

  text: "#f0f4fa",
  tint: "#1de8e8",
};

export const light: Palette = {
  background: "#f7fafc",
  foreground: "#0f172a",

  card: "#ffffff",
  cardForeground: "#0f172a",

  primary: "#0891b2",
  primaryForeground: "#ffffff",

  secondary: "#e6edf5",
  secondaryForeground: "#0f172a",

  muted: "#eef2f7",
  mutedForeground: "#445170",

  accent: "#0891b2",
  accentForeground: "#ffffff",

  destructive: "#dc2626",
  destructiveForeground: "#ffffff",

  success: "#16a34a",
  warning: "#d97706",

  border: "#d4dbe6",
  input: "#d4dbe6",
  borderSubtle: "rgba(15,23,42,0.08)",
  borderGlow: "rgba(8,145,178,0.35)",

  navy600: "#bcc4d4",
  navy700: "#cdd4e0",
  navy800: "#d4dbe6",
  navy850: "#eef2f7",
  navy900: "#f0f4fa",
  navy950: "#f7fafc",

  accentCyan: "#0891b2",
  accentBlue: "#1e6fd9",
  accentPurple: "#7c3aed",
  accentMagenta: "#c2185b",
  accentGreen: "#15803d",
  accentRed: "#b91c1c",
  accentAmber: "#b45309",

  glowCyan: "rgba(8,145,178,0.30)",
  glowGreen: "rgba(21,128,61,0.30)",
  glowRed: "rgba(185,28,28,0.30)",
  glowPurple: "rgba(124,58,237,0.30)",

  text: "#0f172a",
  tint: "#0891b2",
};

/**
 * Typography scale — mirrors the web `Inter` weights ladder.
 */
export const typography = {
  family: {
    regular: "Inter_400Regular",
    medium: "Inter_500Medium",
    semibold: "Inter_600SemiBold",
    bold: "Inter_700Bold",
  },
  size: {
    xs: 10,
    sm: 11,
    base: 13,
    md: 14,
    lg: 16,
    xl: 20,
    xxl: 26,
    display: 32,
  },
  letterSpacing: {
    eyebrow: 2,
    label: 1.5,
    title: 1,
    body: 0,
  },
} as const;

/**
 * Border radii. Web defaults to 0 (sharp cyberpunk edges); mobile keeps
 * the same so cards/pills line up visually with the web.
 */
export const radii = {
  none: 0,
  sm: 2,
  md: 4,
  lg: 8,
  pill: 999,
} as const;

/**
 * Glow shadow recipes — cheap-to-render approximations of the web
 * box-shadow glow effects. Spread via `...glow.cyan` on a View style.
 */
export const glow = {
  cyan: {
    shadowColor: dark.accentCyan,
    shadowOpacity: 0.45,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 0 },
    elevation: 4,
  },
  green: {
    shadowColor: dark.accentGreen,
    shadowOpacity: 0.45,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 0 },
    elevation: 4,
  },
  red: {
    shadowColor: dark.accentRed,
    shadowOpacity: 0.45,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 0 },
    elevation: 4,
  },
  purple: {
    shadowColor: dark.accentPurple,
    shadowOpacity: 0.45,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 0 },
    elevation: 4,
  },
} as const;

const palettes: Record<ThemeMode, Palette> = { dark, light };

export function getPalette(mode: ThemeMode | null | undefined): Palette {
  return mode === "light" ? light : dark;
}

export const theme = {
  palettes,
  typography,
  radii,
  glow,
} as const;
