import { useColorScheme } from "react-native";

import colors from "@/constants/colors";
import { radii, typography } from "@/lib/theme";

/**
 * Returns the design tokens for the current color scheme.
 *
 * Dark is the default operator palette (cyberpunk-finance navy / cyan
 * with green-success / red-loss / purple-edge accents). Light is used
 * when the device is in light appearance mode.
 *
 * The returned object also exposes the typography scale and radii so
 * callers don't need to import multiple modules to render a single
 * screen.
 */
export function useColors() {
  const scheme = useColorScheme();
  const palette = scheme === "light" ? colors.light : colors.dark;
  return {
    ...palette,
    radius: colors.radius,
    radii,
    typography,
  };
}
