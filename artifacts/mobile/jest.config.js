/**
 * Jest configuration for component tests.
 *
 * Uses the official jest-expo preset so React Native and Expo modules
 * resolve correctly, then layers on workspace-specific paths/mocks.
 *
 * Playwright e2e specs live in `tests/` and are excluded here so we
 * don't try to run them through Jest.
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
  ],
  setupFiles: ["<rootDir>/jest.setup.js"],
  moduleNameMapper: {
    "^@/(.*)$": "<rootDir>/$1",
  },
  // pnpm hoists into `.pnpm/<pkg>@<ver>/node_modules/<pkg>`. The default
  // jest-expo `transformIgnorePatterns` only matches `node_modules/<pkg>`,
  // so we re-anchor it to also match the pnpm layout.
  transformIgnorePatterns: [
    "node_modules/(?!.*(?:(jest-)?react-native|@react-native(-community)?|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-clone-referenced-element|@react-native-community/.*|@react-navigation/.*|expo-modules-core|@unimodules/.*|sentry-expo|native-base|react-native-svg|react-native-purchases|react-native-safe-area-context|@clerk/.*))",
  ],
};
