import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the Expo mobile artifact (Task #244).
 *
 * The Expo app ships the same Clerk-backed sign-in + dashboard flow
 * as the web operator console, so we exercise it through Expo's
 * react-native-web export. The suite drives whatever Expo server is
 * already listening at `E2E_MOBILE_BASE_URL` (defaults to the dev
 * workflow on port 5000) — start it with either:
 *
 *   pnpm --filter @workspace/mobile run dev      # Metro web dev
 *   pnpm --filter @workspace/mobile run build && \
 *     pnpm --filter @workspace/mobile run serve  # static export
 *
 * Authenticated flows require Clerk dev keys
 * (`CLERK_PUBLISHABLE_KEY` + `CLERK_SECRET_KEY`) plus an
 * `E2E_CLERK_USER_EMAIL` operator account; without them the
 * auth-gated tests skip themselves the same way the web suite does.
 */
const BASE_URL =
  process.env.E2E_MOBILE_BASE_URL ??
  process.env.PLAYWRIGHT_BASE_URL ??
  "http://localhost:5000";

export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 414, height: 896 } },
    },
  ],
});
