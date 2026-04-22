import { dark, light, radii } from "@/lib/theme";

/**
 * Re-exports the cyberpunk-finance design tokens defined in
 * `lib/theme.ts`. Kept for back-compat with existing imports — new
 * code should import from `@/lib/theme` directly when accessing the
 * extended palette (navy ramp, accent palette, glow shadows).
 */
const colors = {
  light,
  dark,
  radius: radii.none,
};

export default colors;
