import { test, expect, type Page } from "@playwright/test";
import { clerk, clerkSetup } from "@clerk/testing/playwright";

/**
 * Vertical-performance widget e2e (Task #170).
 *
 * Companion to the Python unit tests for `dashboard.get_vertical_performance`.
 * Walks the operator-side widget mounted on `/app`:
 *
 *   - panel-vertical-performance container is visible.
 *   - At least one `card-vertical-perf-{slug}` renders with the widget's
 *     WIN%/TRADES/BRIER metric labels. The widget owns the `-perf-`
 *     namespace so it doesn't collide with the dashboard's
 *     `card-vertical-overview-{slug}` strip (Task #408).
 *   - Clicking each `button-range-{7,30,90}` fires a matching
 *     `/api/dashboard/vertical-performance?days=N` request and updates the
 *     active-button styling (border-primary).
 *   - For every rendered vertical the matching `chart-calibration-{slug}`
 *     and `chart-pit-{slug}` panels render — they may render either a
 *     Recharts surface or the `NO_SETTLED_DATA` empty state.
 *
 * Auth model mirrors `operator-journey.spec.ts`: programmatic Clerk
 * sign-in via the dev keys + `E2E_CLERK_USER_EMAIL` operator account.
 * The test skips when those creds are absent (e.g. offline CI) or when
 * the signed-in user has not been approved yet (no `shell-main`).
 */

const CLERK_USER_EMAIL = process.env.E2E_CLERK_USER_EMAIL;
const HAS_CLERK_DEV_KEYS = Boolean(
  process.env.CLERK_PUBLISHABLE_KEY && process.env.CLERK_SECRET_KEY,
);
const HAS_CLERK_AUTH = HAS_CLERK_DEV_KEYS && Boolean(CLERK_USER_EMAIL);

async function gotoDashboardOrSkip(page: Page) {
  await page.goto("/app");
  const shell = page.getByTestId("shell-main");
  if ((await shell.count()) === 0) {
    test.skip(true, "user not approved — shell not rendered");
  }
  await expect(shell).toBeVisible();
}

test.describe("vertical-performance widget", () => {
  test.skip(
    !HAS_CLERK_AUTH,
    "Clerk dev keys (CLERK_PUBLISHABLE_KEY/CLERK_SECRET_KEY) and E2E_CLERK_USER_EMAIL are required",
  );

  test.beforeAll(async () => {
    await clerkSetup();
  });

  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_USER_EMAIL! });
  });

  test("renders cards + metric labels for each rendered vertical", async ({
    page,
  }) => {
    await gotoDashboardOrSkip(page);

    const panel = page.getByTestId("panel-vertical-performance");
    await expect(panel).toBeVisible({ timeout: 20_000 });

    // Collect the verticals the widget actually rendered (the API decides
    // which slugs are present — we don't assume sports/weather/crypto are
    // all populated in this dev DB). The widget owns the `card-vertical-perf-`
    // namespace so this no longer needs to be scoped inside `panel` to
    // disambiguate from the dashboard's `card-vertical-overview-` strip.
    const cards = page.locator('[data-testid^="card-vertical-perf-"]');
    await expect(cards.first()).toBeVisible({ timeout: 20_000 });
    const cardCount = await cards.count();
    expect(cardCount).toBeGreaterThan(0);

    // Every rendered card must surface the widget's metric labels.
    for (let i = 0; i < cardCount; i += 1) {
      const card = cards.nth(i);
      await expect(card.getByText("WIN%", { exact: true })).toBeVisible();
      await expect(card.getByText("TRADES", { exact: true })).toBeVisible();
      await expect(card.getByText("BRIER", { exact: true })).toBeVisible();
      // N=<count> trade-count badge in the card header.
      await expect(card.getByText(/^N=\d+$/)).toBeVisible();
    }
  });

  test("range toggle 7/30/90 fires the matching API call and updates active styling", async ({
    page,
  }) => {
    await gotoDashboardOrSkip(page);

    const panel = page.getByTestId("panel-vertical-performance");
    await expect(panel).toBeVisible({ timeout: 20_000 });

    for (const days of [7, 30, 90] as const) {
      const button = panel.getByTestId(`button-range-${days}`);
      await expect(button).toBeVisible();

      // Click the range button and assert the corresponding API call fires.
      const [request] = await Promise.all([
        page.waitForRequest(
          (req) =>
            req.method() === "GET" &&
            req.url().includes("/api/dashboard/vertical-performance") &&
            new URL(req.url()).searchParams.get("days") === String(days),
          { timeout: 15_000 },
        ),
        button.click(),
      ]);
      expect(request.url()).toContain(`days=${days}`);

      // Active styling: the selected button picks up the primary border
      // utility (`border-primary`). The other two should not.
      await expect(button).toHaveClass(/border-primary/);
      for (const other of [7, 30, 90].filter((d) => d !== days)) {
        await expect(panel.getByTestId(`button-range-${other}`)).not.toHaveClass(
          /border-primary/,
        );
      }
    }
  });

  test("calibration + PIT panels render for every vertical (chart or empty state)", async ({
    page,
  }) => {
    await gotoDashboardOrSkip(page);

    const panel = page.getByTestId("panel-vertical-performance");
    await expect(panel).toBeVisible({ timeout: 20_000 });

    const cards = page.locator('[data-testid^="card-vertical-perf-"]');
    await expect(cards.first()).toBeVisible({ timeout: 20_000 });
    const cardCount = await cards.count();
    expect(cardCount).toBeGreaterThan(0);

    // Pull the slug suffix from each card-vertical-perf-* testid and
    // confirm the matching calibration and PIT panels are mounted. Each
    // panel either renders a Recharts <svg> or the NO_SETTLED_DATA copy.
    const slugs: string[] = [];
    for (let i = 0; i < cardCount; i += 1) {
      const tid = await cards.nth(i).getAttribute("data-testid");
      if (tid) slugs.push(tid.replace(/^card-vertical-perf-/, ""));
    }
    expect(slugs.length).toBe(cardCount);

    for (const slug of slugs) {
      const calibration = panel.getByTestId(`chart-calibration-${slug}`);
      const pit = panel.getByTestId(`chart-pit-${slug}`);
      await expect(calibration).toBeVisible();
      await expect(pit).toBeVisible();

      // Either a chart svg or the empty-state label must be present in
      // each panel — a blank panel would be a regression.
      const calibrationOk =
        (await calibration.locator("svg").count()) > 0 ||
        (await calibration.getByText("NO_SETTLED_DATA").count()) > 0;
      expect(calibrationOk).toBe(true);

      const pitOk =
        (await pit.locator("svg").count()) > 0 ||
        (await pit.getByText("NO_SETTLED_DATA").count()) > 0;
      expect(pitOk).toBe(true);
    }
  });
});
