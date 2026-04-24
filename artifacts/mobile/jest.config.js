/**
 * Jest configuration for the Expo mobile artifact.
 *
 * Uses the official `jest-expo` preset so React Native and Expo modules
 * resolve correctly, then layers on workspace-specific paths/mocks. The
 * preset also teaches Jest how to transform the soup of TypeScript /
 * JSX / Flow / ESM-only modules that Expo and React Native ship —
 * without it, even importing a single mobile file blows up at parse
 * time.
 *
 * Today the suite runs two flavours of test side-by-side:
 *   - Component tests under `**\/__tests__/*.test.tsx` (rendered with
 *     @testing-library/react-native, run via the `mobile-tests`
 *     workflow).
 *   - Pure-logic unit tests like `lib/vertical-filter.test.ts`.
 *
 * Both flavours feed into the same Istanbul coverage report — CI uses
 * `--coverage` to track the surface so reviewers can see when frontend
 * changes drop the number. Playwright e2e specs live in `tests/` and
 * are excluded here so we don't try to run them through Jest.
 */
module.exports = {
  preset: "jest-expo",
  rootDir: ".",
  testMatch: [
    "<rootDir>/**/__tests__/**/*.test.(ts|tsx|js|jsx)",
    "<rootDir>/**/?(*.)+(spec|test).(ts|tsx|js|jsx)",
  ],
  testPathIgnorePatterns: [
    "/node_modules/",
    "<rootDir>/tests/",
    "<rootDir>/.expo/",
    "<rootDir>/dist/",
  ],
  setupFiles: ["<rootDir>/jest.setup.js"],
  moduleNameMapper: {
    // Match the `@/*` path alias declared in tsconfig.json so test
    // files can import the same way runtime code does.
    "^@/(.*)$": "<rootDir>/$1",
  },
  // pnpm hoists into `.pnpm/<pkg>@<ver>/node_modules/<pkg>`. The default
  // jest-expo `transformIgnorePatterns` only matches `node_modules/<pkg>`,
  // so we re-anchor it to also match the pnpm layout.
  transformIgnorePatterns: [
    "node_modules/(?!.*(?:(jest-)?react-native|@react-native(-community)?|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-clone-referenced-element|@react-native-community/.*|@react-navigation/.*|expo-modules-core|@unimodules/.*|sentry-expo|native-base|react-native-svg|react-native-purchases|react-native-safe-area-context|@clerk/.*))",
  ],
  // Coverage scope mirrors the web side: only count source we author,
  // skip generated routes, native shims, and the build entrypoint.
  collectCoverageFrom: [
    "app/**/*.{ts,tsx}",
    "components/**/*.{ts,tsx}",
    "constants/**/*.{ts,tsx}",
    "hooks/**/*.{ts,tsx}",
    "lib/**/*.{ts,tsx}",
    "!**/*.d.ts",
    "!**/*.test.{ts,tsx}",
    "!**/__mocks__/**",
  ],
  coverageDirectory: "coverage",
  // `json-summary` is what the CI commenter parses; the others are for
  // humans (text in the log, html on disk).
  coverageReporters: ["text", "html", "json", "json-summary"],
};
