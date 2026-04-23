import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the operator console e2e suite.
 *
 * Two specs ship with the redesign sweep (Tasks #228–#237):
 *   - `tests/operator-journey.spec.ts` — public marketing/pricing/sign-in
 *     surfaces and the signed-in operator IA (Dashboard → Trading →
 *     Positions → Pipeline → Intelligence → System → Profile → sign-out).
 *   - `tests/admin-journey.spec.ts` — admin-only tabs (audit log,
 *     runtime-settings, vertical toggles) plus a non-admin negative path.
 *
 * Authenticated paths require a Clerk test user; either set
 * `E2E_CLERK_USER_EMAIL` + `E2E_CLERK_USER_PASSWORD` in the env or run
 * the suite through the platform's testing skill (which signs in
 * programmatically). When those creds are absent the auth-gated tests
 * skip themselves rather than failing — the public-surface assertions
 * still run.
 *
 * Base URL defaults to the local Vite dev server; override with
 * `E2E_BASE_URL=https://example.replit.app` to point at any deploy.
 */
const BASE_URL =
  process.env.E2E_BASE_URL ??
  process.env.PLAYWRIGHT_BASE_URL ??
  "http://localhost:5173";

export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI
    ? [
        ["github"],
        ["list"],
        ["html", { open: "never" }],
        ["json", { outputFile: "test-results/results.json" }],
      ]
    : "list",
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 720 } },
    },
  ],
});
