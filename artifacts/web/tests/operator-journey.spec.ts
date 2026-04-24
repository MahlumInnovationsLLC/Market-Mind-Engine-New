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

  /**
   * Free-tier upsell coverage (Task #393).
   *
   * The upgrade modal and the inline delayed-signals banner were wired
   * onto the dashboard / trade-detail surfaces without an automated
   * test, because exercising them end-to-end normally requires a
   * free-tier fixture user that has burned the daily distinct-market
   * quota or is currently throttled by the 15-minute delay path.
   *
   * To keep the test hermetic (and runnable against any deployment, not
   * just one with a fully provisioned free-tier user), we mock the
   * relevant backend endpoints to return the exact payload shape the
   * frontend reads from. That locks down both the payload contract and
   * the rendering — a future regen of `@workspace/api-client-react` or
   * a refactor of the dashboard that drops the structural cast on
   * `delayed` / `delay_minutes` will fail this suite immediately.
   */
  test("opening a 6th distinct market triggers the upgrade modal with a working pricing link", async ({
    page,
  }) => {
    // Walk through the real free-tier journey: a caller opens 6
    // distinct markets in a UTC day. The first 5 must succeed (no
    // upsell), and the 6th must 402 with the FastAPI HTTPException
    // shape that `FreeTierQuotaError` raises in
    // artifacts/api-server/app/free_tier.py.
    //
    // We can't rely on a real free-tier fixture user (the operator
    // account that drives the rest of this suite is approved/Pro and
    // bypasses the gate server-side), so we install a route handler
    // that simulates the backend's distinct-market accounting: it
    // tracks the set of refs the page has requested and 402s the 6th.
    // That keeps the assertion driven by the same trade-detail
    // navigation an end-user would do, while staying hermetic.
    const REFS = ["m1", "m2", "m3", "m4", "m5", "m6"] as const;
    const seen = new Set<string>();
    let lastBlockedRef: string | null = null;

    await page.route("**/api/dashboard/trades/**", async (route) => {
      const url = new URL(route.request().url());
      // Path is /api/dashboard/trades/{trade_ref}
      const ref = decodeURIComponent(url.pathname.split("/").pop() ?? "");
      seen.add(ref);

      if (seen.size > 5 && !["m1", "m2", "m3", "m4", "m5"].includes(ref)) {
        // 6th distinct market — fail closed with the upgrade payload.
        lastBlockedRef = ref;
        await route.fulfill({
          status: 402,
          contentType: "application/json",
          body: JSON.stringify({
            detail: {
              error: "upgrade_required",
              reason: "free_tier_daily_market_limit",
              limit: 5,
              viewed_today: 5,
              market_id: ref,
              resets_at: "00:00 UTC",
            },
          }),
        });
        return;
      }

      // First 5 distinct markets succeed with a minimal TradeDetail
      // payload. The detail page only needs `trade.id` + `series` to
      // render successfully without throwing, which is enough to
      // confirm the upsell does NOT appear on those visits.
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          trade: {
            id: 1000 + REFS.indexOf(ref as (typeof REFS)[number]),
            trade_id: ref,
            market_id: ref,
            market_title: `Test market ${ref}`,
            vertical: "test",
            league: "TEST",
            side: "yes",
            action: "buy",
            quantity: 1,
            price: 0.5,
            pnl: 0,
            model_prob: 0.6,
            model_edge: 0.05,
            kelly_fraction: 0.02,
            trading_mode: "paper",
            exchange: "kalshi",
            executed_at: new Date().toISOString(),
          },
          series: [],
        }),
      });
    });

    // First market: confirm sign-in + shell are reachable before we
    // sink time into 5 more navigations. Skip the whole block if the
    // operator account isn't approved (matching the rest of the suite).
    await page.goto(`/trade/${REFS[0]}`);
    const shell = page.getByTestId("shell-main");
    if ((await shell.count()) === 0) test.skip(true, "user not approved — shell not rendered");

    // The trade-detail page renders its title from `trade.market_title`
    // when the request succeeds. No upgrade modal should appear on the
    // first 5 visits.
    await expect(page.getByTestId("text-trade-title")).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByTestId("modal-upgrade-required")).toHaveCount(0);

    for (const ref of REFS.slice(1, 5)) {
      await page.goto(`/trade/${ref}`);
      await expect(page.getByTestId("text-trade-title")).toBeVisible({
        timeout: 15_000,
      });
      await expect(page.getByTestId("modal-upgrade-required")).toHaveCount(0);
    }

    // 6th distinct market — the route handler returns 402 and the
    // dashboard must surface both the inline upsell card and the
    // dialog with the limit / reset copy from the payload.
    await page.goto(`/trade/${REFS[5]}`);
    await expect(page.getByTestId("text-upgrade-required")).toBeVisible({
      timeout: 15_000,
    });
    const modal = page.getByTestId("modal-upgrade-required");
    await expect(modal).toBeVisible({ timeout: 15_000 });

    // Copy contract: a regen of @workspace/api-client-react that drops
    // one of these payload fields would surface as "undefined" in the
    // dialog body, so we lock each one down explicitly.
    await expect(modal).toContainText("5 distinct markets");
    await expect(modal).toContainText("opened 5 today");
    await expect(modal).toContainText("00:00 UTC");

    // Confirms our route handler actually 402'd on the 6th distinct
    // ref (and not on, say, an unrelated dashboard subscription poll
    // that happened to share the URL prefix).
    expect(lastBlockedRef).toBe("m6");
    expect(seen.size).toBeGreaterThanOrEqual(6);

    const pricingLink = page.getByTestId("link-upgrade-pricing");
    await expect(pricingLink).toBeVisible();
    await expect(pricingLink).toHaveAttribute("href", /\/pricing$/);

    // Stop intercepting before the navigation so the pricing page can
    // hit its real backend (tier catalog fetch) without 402-ing on a
    // shared URL prefix.
    await page.unroute("**/api/dashboard/trades/**");

    await pricingLink.click();
    await expect(page).toHaveURL(/\/pricing$/, { timeout: 15_000 });
    await expect(page.getByTestId("text-pricing-title")).toBeVisible({
      timeout: 15_000,
    });
  });

  test("dashboard renders delayed-signals banners above Recent Fills and Recent Debates", async ({
    page,
  }) => {
    // The frontend reads `delayed` / `delay_minutes` off the top-level
    // response object via a structural cast (see recent-trades.tsx and
    // swarm-debates.tsx). Mock both list endpoints to assert the
    // banner-rendering branch fires for both panels simultaneously.
    await page.route("**/api/dashboard/recent-trades**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [],
          delayed: true,
          delay_minutes: 15,
        }),
      });
    });
    await page.route("**/api/dashboard/recent-debates**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [],
          tier: "free",
          delayed: true,
          delay_minutes: 15,
        }),
      });
    });

    await page.goto("/app");
    const shell = page.getByTestId("shell-main");
    if ((await shell.count()) === 0) test.skip(true, "user not approved — shell not rendered");

    // Both panels must mount before we can assert the banners; their
    // GlassCard wrappers are the closest stable selectors.
    await expect(page.getByTestId("card-recent-trades")).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByTestId("card-swarm-debates")).toBeVisible({
      timeout: 15_000,
    });

    // The banners are rendered as the first child of each panel body
    // (above the table / debate rows), with stable per-panel test ids.
    const tradesBanner = page.getByTestId("banner-recent-trades-delayed");
    await expect(tradesBanner).toBeVisible({ timeout: 15_000 });
    await expect(tradesBanner).toContainText(/15-min delay/i);
    await expect(tradesBanner).toContainText(/upgrade to pro/i);

    const debatesBanner = page.getByTestId("banner-recent-debates-delayed");
    await expect(debatesBanner).toBeVisible({ timeout: 15_000 });
    await expect(debatesBanner).toContainText(/15-min delay/i);

    // Each banner ships its own pricing CTA. Anchor on the trades
    // banner's link to keep the assertion deterministic even though
    // both share the `link-delayed-upgrade` testid.
    const tradesUpgradeLink = tradesBanner.getByTestId("link-delayed-upgrade");
    await expect(tradesUpgradeLink).toBeVisible();
    await expect(tradesUpgradeLink).toHaveAttribute("href", /\/pricing$/);

    const debatesUpgradeLink = debatesBanner.getByTestId("link-delayed-upgrade");
    await expect(debatesUpgradeLink).toBeVisible();
    await expect(debatesUpgradeLink).toHaveAttribute("href", /\/pricing$/);
  });
});
