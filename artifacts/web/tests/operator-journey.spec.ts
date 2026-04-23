import { test, expect, type Page } from "@playwright/test";

/**
 * Operator-journey e2e (Task #237).
 *
 * Public surface (no auth) is exercised unconditionally. The
 * authenticated walk-through (Dashboard → Trading → Positions →
 * Pipeline → Intelligence → System → Profile → sign-out) is guarded
 * by `E2E_CLERK_USER_EMAIL` + `E2E_CLERK_USER_PASSWORD`; when those
 * env vars are absent the auth-gated portion skips so CI can still
 * smoke-test the unauthenticated surfaces.
 *
 * Each authenticated route asserts a page-specific KPI/card data-testid
 * (not just `shell-main`) so a regression that drops the page body
 * fails the test.
 */

const CLERK_EMAIL = process.env.E2E_CLERK_USER_EMAIL;
const CLERK_PASSWORD = process.env.E2E_CLERK_USER_PASSWORD;
const HAS_CLERK_CREDS = Boolean(CLERK_EMAIL && CLERK_PASSWORD);

async function expectNoChunkLoadError(page: Page) {
  await expect(page.locator("text=ChunkLoadError")).toHaveCount(0);
  await expect(page.locator("text=Failed to fetch dynamically imported module")).toHaveCount(0);
}

async function clerkSignIn(page: Page, email: string, password: string) {
  await page.goto("/sign-in");
  await page.locator('input[name="identifier"], input[type="email"]').fill(email);
  await page.getByRole("button", { name: /continue|next/i }).click();
  await page.locator('input[name="password"], input[type="password"]').fill(password);
  await page.getByRole("button", { name: /continue|sign in/i }).click();
  await page.waitForURL(/\/(app|sign-in)/, { timeout: 30_000 }).catch(() => {});
}

test.describe("operator journey — public surfaces", () => {
  test("marketing landing renders the hero and primary nav", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("link", { name: /pricing/i })).toBeVisible();
    await expect(page.getByRole("link", { name: /leaderboard/i })).toBeVisible();
    await expect(page.getByRole("link", { name: /about/i })).toBeVisible();
    await expect(page.getByRole("link", { name: /sign in/i })).toBeVisible();
    await expect(page.getByText(/Six analyst LLMs|prediction-market/i)).toBeVisible();
    await expectNoChunkLoadError(page);
  });

  test("pricing page (lazy chunk) renders all three tiers", async ({ page }) => {
    await page.goto("/pricing");
    await expect(page.getByText(/Free/i).first()).toBeVisible();
    await expect(page.getByText(/Pro/i).first()).toBeVisible();
    await expect(page.getByText(/Enterprise|Elite/i).first()).toBeVisible();
    await expectNoChunkLoadError(page);
  });

  test("about + leaderboard lazy chunks load without error", async ({ page }) => {
    await page.goto("/about");
    await expect(page.locator("body")).toBeVisible();
    await expectNoChunkLoadError(page);

    await page.goto("/leaderboard");
    await expect(page.locator("body")).toBeVisible();
    await expectNoChunkLoadError(page);
  });

  test("sign-in route mounts the Clerk widget", async ({ page }) => {
    await page.goto("/sign-in");
    await expect(page.locator('input[name="identifier"], input[type="email"]')).toBeVisible({
      timeout: 15_000,
    });
    await expectNoChunkLoadError(page);
  });
});

/**
 * Per-page assertion targets. Each entry pairs a route with a list of
 * `data-testid` values that must be present once the lazy chunk has
 * mounted. These are real cards/panels rendered by the pages under
 * test, not generic shell scaffolding.
 */
const AUTHED_ROUTES: Array<{ path: string; testIds: string[] }> = [
  {
    path: "/app",
    testIds: ["card-recent-trades", "card-trading-cycles", "card-health-snapshot"],
  },
  { path: "/trading", testIds: ["page-trading", "card-command-center"] },
  { path: "/positions", testIds: ["page-positions", "card-positions-table"] },
  { path: "/pipeline", testIds: ["page-pipeline"] },
  { path: "/intelligence", testIds: ["page-intelligence", "panel-intelligence-swarm"] },
  { path: "/system", testIds: ["page-system", "panel-system-health"] },
  { path: "/profile", testIds: ["page-profile", "card-profile-identity"] },
];

test.describe("operator journey — authenticated walk-through", () => {
  test.skip(!HAS_CLERK_CREDS, "E2E_CLERK_USER_EMAIL / _PASSWORD not set");

  test.beforeEach(async ({ page }) => {
    await clerkSignIn(page, CLERK_EMAIL!, CLERK_PASSWORD!);
  });

  test("skip-to-content link is the first focusable element", async ({ page }) => {
    await page.goto("/app");
    const shell = page.getByTestId("shell-main");
    if ((await shell.count()) === 0) test.skip(true, "user not approved — shell not rendered");

    await page.keyboard.press("Tab");
    await expect(page.getByTestId("link-skip-to-content")).toBeFocused();
    // Activating the link should land focus on <main>.
    await page.keyboard.press("Enter");
    await expect(page.locator("main#main-content")).toBeFocused();
  });

  for (const { path, testIds } of AUTHED_ROUTES) {
    test(`${path} renders its KPI/cards via the lazy chunk`, async ({ page }) => {
      await page.goto(path);
      const shell = page.getByTestId("shell-main");
      if ((await shell.count()) === 0) test.skip(true, "user not approved — shell not rendered");

      await expect(shell).toBeVisible();
      for (const id of testIds) {
        await expect(page.getByTestId(id).first()).toBeVisible({ timeout: 15_000 });
      }
      await expectNoChunkLoadError(page);
    });
  }

  test("sign-out clears the session and returns the user to the marketing surface", async ({
    page,
  }) => {
    await page.goto("/profile");
    const shell = page.getByTestId("shell-main");
    if ((await shell.count()) === 0) test.skip(true, "user not approved — shell not rendered");

    // Open the operator-id menu in the shell header and click "Sign out".
    await page.getByTestId("button-shell-user").click();
    await page.getByTestId("button-shell-sign-out").click();

    // Clerk redirects to BASE_URL (/) on sign-out. We expect the
    // marketing hero (signed-out) to be visible and the sign-in link to
    // re-appear in the public nav.
    await page.waitForURL(/\/$/, { timeout: 30_000 });
    await expect(page.getByRole("link", { name: /sign in/i })).toBeVisible();
    await expect(page.getByTestId("shell-main")).toHaveCount(0);
  });
});
