import { test, expect, type Page } from "@playwright/test";
import { clerk, clerkSetup } from "@clerk/testing/playwright";

/**
 * Operator-journey e2e (Task #237 / #238).
 *
 * Public surface (no auth) is exercised unconditionally. The
 * authenticated walk-through (Dashboard → Trading → Positions →
 * Pipeline → Intelligence → System → Profile → sign-out) is guarded
 * by Clerk dev-instance creds:
 *   - `CLERK_PUBLISHABLE_KEY` + `CLERK_SECRET_KEY` (required by
 *     `clerkSetup`; these are dev keys, not prod).
 *   - `E2E_CLERK_USER_EMAIL` (the operator account's email — Clerk
 *     mints a sign-in ticket for that user via its backend API; no
 *     password / UI form fill is required).
 *
 * When any of those are absent the auth-gated portion skips so CI can
 * still smoke-test the unauthenticated surfaces.
 *
 * Each authenticated route asserts a page-specific KPI/card data-testid
 * (not just `shell-main`) so a regression that drops the page body
 * fails the test.
 */

const CLERK_USER_EMAIL = process.env.E2E_CLERK_USER_EMAIL;
const HAS_CLERK_DEV_KEYS = Boolean(
  process.env.CLERK_PUBLISHABLE_KEY && process.env.CLERK_SECRET_KEY,
);
const HAS_CLERK_AUTH = HAS_CLERK_DEV_KEYS && Boolean(CLERK_USER_EMAIL);

async function expectNoChunkLoadError(page: Page) {
  await expect(page.locator("text=ChunkLoadError")).toHaveCount(0);
  await expect(
    page.locator("text=Failed to fetch dynamically imported module"),
  ).toHaveCount(0);
}

test.describe("operator journey — public surfaces", () => {
  test("marketing landing renders the hero and primary nav", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("link", { name: /pricing/i }).first()).toBeVisible();
    await expect(page.getByRole("link", { name: /leaderboard/i }).first()).toBeVisible();
    await expect(page.getByRole("link", { name: /about/i }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: /sign in/i }).first()).toBeVisible();
    await expect(
      page.getByText(/Six analyst LLMs|prediction-market/i).first(),
    ).toBeVisible();
    await expectNoChunkLoadError(page);
  });

  test("pricing page (lazy chunk) renders the tier catalog scaffold", async ({ page }) => {
    await page.goto("/pricing");
    // The pricing route always renders its title even if the tier
    // catalog API call is still in flight or unreachable in CI.
    await expect(page.getByTestId("text-pricing-title")).toBeVisible({ timeout: 15_000 });
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

  test("sign-in route loads cleanly (no lazy chunk errors)", async ({ page }) => {
    await page.goto("/sign-in");
    await expect(page).toHaveURL(/\/sign-in/);
    // The SPA root mounts something (SignInPage when Clerk reaches the
    // browser, NotFound when Clerk is unreachable in offline CI). The
    // important regression to catch is a lazy-chunk failure — that's
    // what would white-screen the app for real users.
    await expect(page.locator("#root")).not.toBeEmpty({ timeout: 10_000 });
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
  test.skip(
    !HAS_CLERK_AUTH,
    "Clerk dev keys (CLERK_PUBLISHABLE_KEY/CLERK_SECRET_KEY) and E2E_CLERK_USER_EMAIL are required",
  );

  test.beforeAll(async () => {
    // Fetches a Clerk testing token tied to the dev instance so
    // sign-in can be done programmatically (no UI form fill).
    await clerkSetup();
  });

  test.beforeEach(async ({ page }) => {
    // Land on a non-protected page that loads Clerk before signing in.
    await page.goto("/");
    // Passwordless ticket strategy: Clerk's backend mints a sign-in
    // token for the operator email. No password is required, matching
    // the testing-skill `testClerkAuth: true` semantic (works with
    // dev keys alone).
    await clerk.signIn({ page, emailAddress: CLERK_USER_EMAIL! });
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

    await clerk.signOut({ page });

    // Clerk redirects to BASE_URL (/) on sign-out. We expect the
    // marketing hero (signed-out) to be visible and the sign-in button
    // to re-appear in the public nav.
    await page.goto("/");
    await expect(page.getByRole("button", { name: /sign in/i }).first()).toBeVisible();
    await expect(page.getByTestId("shell-main")).toHaveCount(0);
  });

  /**
   * Readiness panel coverage (Task #349).
   *
   * The Health tab embeds a readiness section sourced from
   * `/api/health/ready` (HealthPanel → ReadinessSection). It surfaces
   * a READY/NOT READY badge plus a tile per ready-gate check. These
   * two cases lock down both the live happy path and the explicit
   * 503-with-failing-check rendering so a regression in either the
   * endpoint shape or the panel logic fails the suite.
   */
  test("Health → readiness panel renders the badge and each known check tile", async ({
    page,
  }) => {
    await page.goto("/system?tab=health");
    const shell = page.getByTestId("shell-main");
    if ((await shell.count()) === 0) test.skip(true, "user not approved — shell not rendered");

    // Health is the default tab on /system; the panel should mount
    // on first navigation.
    await expect(page.getByTestId("panel-system-health")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("panel-readiness")).toBeVisible({ timeout: 15_000 });

    // Badge starts as PENDING while the readiness fetch is in flight,
    // then settles to READY or NOT READY once the API responds.
    const badge = page.getByTestId("readiness-badge");
    await expect(badge).toBeVisible();
    await expect(badge).toHaveText(/^(READY|NOT READY)$/, { timeout: 15_000 });

    // The API always reports all four ready-gate checks by name
    // (see _ready_gate_watcher in artifacts/api-server/app/main.py),
    // so each one should render as its own tile in the panel.
    for (const name of ["migrations", "secrets", "models", "feeds"] as const) {
      await expect(
        page.getByTestId(`readiness-check-${name}`),
      ).toBeVisible({ timeout: 15_000 });
    }
  });

  test("Health → readiness panel highlights failing check when /api/health/ready returns 503", async ({
    page,
  }) => {
    // Force a deterministic NOT READY response so the failing-check
    // rendering path is exercised regardless of the live API state.
    // FastAPI wraps HTTPException(detail=...) as { "detail": ... }.
    const now = Math.floor(Date.now() / 1000);
    const detail = {
      status: "not_ready",
      ready: false,
      started_at: now - 30,
      completed_at: null,
      elapsed_seconds: 30,
      checks: {
        migrations: {
          name: "migrations",
          ok: true,
          detail: "head=abc123",
          checked_at: now,
        },
        secrets: {
          name: "secrets",
          ok: true,
          detail: "required secrets present",
          checked_at: now,
        },
        models: {
          name: "models",
          ok: true,
          detail: "3 predictor(s) loaded",
          checked_at: now,
        },
        feeds: {
          name: "feeds",
          ok: false,
          detail: "no first poll within 120s: kalshi",
          checked_at: now,
        },
      },
    };

    await page.route("**/api/health/ready", async (route) => {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail }),
      });
    });

    await page.goto("/system?tab=health");
    const shell = page.getByTestId("shell-main");
    if ((await shell.count()) === 0) test.skip(true, "user not approved — shell not rendered");

    await expect(page.getByTestId("panel-readiness")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("readiness-badge")).toHaveText("NOT READY", {
      timeout: 15_000,
    });

    // The `failing :: <names>` summary should call out the failing
    // check by name (and only that one, since the other three are ok).
    const failing = page.getByTestId("readiness-failing");
    await expect(failing).toBeVisible();
    await expect(failing).toContainText(/^failing\s*::/i);
    await expect(failing).toContainText("feeds");
    await expect(failing).not.toContainText("migrations");
    await expect(failing).not.toContainText("secrets");
    await expect(failing).not.toContainText("models");

    // The failing tile gets the destructive ring; the passing ones
    // do not. This is what visually distinguishes the broken check
    // for the operator scanning the panel.
    const feedsTile = page.getByTestId("readiness-check-feeds");
    await expect(feedsTile).toBeVisible();
    await expect(feedsTile).toHaveClass(/ring-destructive/);
    await expect(feedsTile).toContainText("kalshi");

    for (const name of ["migrations", "secrets", "models"] as const) {
      const tile = page.getByTestId(`readiness-check-${name}`);
      await expect(tile).toBeVisible();
      await expect(tile).not.toHaveClass(/ring-destructive/);
    }
  });
});
