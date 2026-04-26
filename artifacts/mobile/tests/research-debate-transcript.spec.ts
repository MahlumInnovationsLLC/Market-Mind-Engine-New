import { test, expect, type Page, type Route } from "@playwright/test";
import { clerk, clerkSetup } from "@clerk/testing/playwright";

/**
 * Mobile research debate-transcript e2e (Task #417).
 *
 * The Research tab (`app/(tabs)/research.tsx`) renders today's
 * top AutoResearch debates as tap targets that push to
 * `/research/[id]` (`app/research/[id].tsx`). The detail screen
 * branches on the `redacted` flag:
 *
 *   - Pro fixture (redacted=false): renders the moderator
 *     synthesis card and one analyst-verdict card per persona
 *     (`debate-detail-moderator` + `debate-detail-persona-*`).
 *   - Sub-Pro fixture (redacted=true): renders the upsell card
 *     (`debate-detail-upsell`) instead, and must NOT leak any of
 *     the analyst reasoning text.
 *
 * Both branches share the same React Query cache key
 * (`["dashboard","recent-debates","research"]`) so we mock
 * `/api/dashboard/recent-debates` once with both fixtures and
 * exercise each tap path in a separate test.
 *
 * Like the rest of the mobile suite we mock `/api/me/access` and
 * `/api/me/notifications` so the (tabs) layout mounts regardless of
 * the live operator's approval/admin state, and skip when Clerk
 * dev keys (`CLERK_PUBLISHABLE_KEY` + `CLERK_SECRET_KEY`) or
 * `E2E_CLERK_USER_EMAIL` are missing.
 */

const CLERK_USER_EMAIL = process.env.E2E_CLERK_USER_EMAIL;
const HAS_CLERK_DEV_KEYS = Boolean(
  process.env.CLERK_PUBLISHABLE_KEY && process.env.CLERK_SECRET_KEY,
);
const HAS_CLERK_AUTH = HAS_CLERK_DEV_KEYS && Boolean(CLERK_USER_EMAIL);

const PRO_DEBATE_ID = 4242;
const REDACTED_DEBATE_ID = 4243;
const PRO_MATCHUP = "Lakers vs. Celtics";
const REDACTED_MATCHUP = "Yankees vs. Red Sox";
const PRO_SYNTHESIS =
  "Moderator weighs the persona priors and lands on a measured YES with a tight band.";
const QUANT_REASONING =
  "Quant model places fair value at 0.62 with low residual variance across the last twelve comparable matchups.";
const NARRATIVE_REASONING =
  "Narrative analyst flags injury-report momentum and crowd factor as net YES.";

async function mockAccess(page: Page): Promise<void> {
  await page.route("**/api/me/access*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        approved: true,
        is_admin: false,
        email: "operator@example.com",
        user_id: "test-user",
      }),
    });
  });
}

async function mockNotifications(page: Page): Promise<void> {
  await page.route("**/api/me/notifications*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ unread_count: 0, items: [] }),
    });
  });
}

async function mockUwSignals(page: Page): Promise<void> {
  await page.route("**/api/dashboard/uw-signals*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    });
  });
}

async function mockDebates(page: Page): Promise<void> {
  // Use a fresh ISO timestamp on every request so the
  // research screen's `isToday` filter keeps both fixtures in
  // the rendered list even if the test takes a while to drive.
  await page.route(
    "**/api/dashboard/recent-debates*",
    async (route: Route) => {
      const now = new Date().toISOString();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          tier: "pro",
          delayed: false,
          items: [
            {
              id: PRO_DEBATE_ID,
              matchup: PRO_MATCHUP,
              league: "nba",
              market_type: "moneyline",
              consensus_probability: 0.62,
              confidence_band_low: 0.58,
              confidence_band_high: 0.66,
              agreement_rate: 0.8,
              approved: true,
              recommended_action: "yes",
              created_at: now,
              redacted: false,
              analyst_verdicts: [
                {
                  persona: "quant",
                  provider: "openai",
                  model: "gpt-4o",
                  probability: 0.61,
                  confidence: 0.7,
                  lean: "YES",
                  weight: 0.5,
                  reasoning: QUANT_REASONING,
                  latency_ms: 812,
                },
                {
                  persona: "narrative",
                  provider: "anthropic",
                  model: "claude-3-5-sonnet",
                  probability: 0.64,
                  confidence: 0.65,
                  lean: "YES",
                  weight: 0.5,
                  reasoning: NARRATIVE_REASONING,
                  latency_ms: 940,
                },
              ],
              moderator_synthesis: {
                model: "gpt-4o",
                consensus_probability: 0.62,
                confidence_band_low: 0.58,
                confidence_band_high: 0.66,
                agreement_rate: 0.8,
                recommended_action: "yes",
                synthesis: PRO_SYNTHESIS,
                latency_ms: 1200,
                cost: 0.04,
                gatekeeper: {
                  model: "gpt-4o-mini",
                  approved: true,
                  recommended_action: "yes",
                  veto_reason: null,
                },
              },
            },
            {
              id: REDACTED_DEBATE_ID,
              matchup: REDACTED_MATCHUP,
              league: "mlb",
              market_type: "moneyline",
              consensus_probability: 0.55,
              confidence_band_low: 0.5,
              confidence_band_high: 0.6,
              agreement_rate: 0.6,
              approved: true,
              recommended_action: "yes",
              created_at: now,
              redacted: true,
              analyst_verdicts: null,
              moderator_synthesis: null,
            },
          ],
        }),
      });
    },
  );
}

test.describe("mobile research — debate transcript", () => {
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

  test("Pro fixture renders moderator synthesis and analyst persona blocks", async ({
    page,
  }) => {
    await mockAccess(page);
    await mockNotifications(page);
    await mockUwSignals(page);
    await mockDebates(page);

    await page.goto("/research");

    // The Research tab card is keyed by id and is the tap target
    // that pushes the detail route.
    const card = page.getByTestId(`debate-card-${PRO_DEBATE_ID}`);
    await expect(card).toBeVisible({ timeout: 20_000 });
    await expect(card.getByText(PRO_MATCHUP)).toBeVisible();

    await card.click();

    // Detail screen mounts with the summary card at the top, then
    // the moderator synthesis card, then one persona card per analyst.
    await expect(page.getByTestId("debate-detail-summary")).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByTestId("debate-detail-moderator")).toBeVisible();
    await expect(
      page.getByTestId("debate-detail-moderator").getByText(PRO_SYNTHESIS),
    ).toBeVisible();

    await expect(page.getByTestId("debate-detail-persona-quant")).toBeVisible();
    await expect(
      page
        .getByTestId("debate-detail-persona-quant")
        .getByText(QUANT_REASONING),
    ).toBeVisible();

    await expect(
      page.getByTestId("debate-detail-persona-narrative"),
    ).toBeVisible();
    await expect(
      page
        .getByTestId("debate-detail-persona-narrative")
        .getByText(NARRATIVE_REASONING),
    ).toBeVisible();

    // The upsell card must NOT render for the Pro fixture.
    await expect(page.getByTestId("debate-detail-upsell")).toHaveCount(0);
  });

  test("redacted fixture renders the upsell card and hides analyst reasoning", async ({
    page,
  }) => {
    await mockAccess(page);
    await mockNotifications(page);
    await mockUwSignals(page);
    await mockDebates(page);

    await page.goto("/research");

    const card = page.getByTestId(`debate-card-${REDACTED_DEBATE_ID}`);
    await expect(card).toBeVisible({ timeout: 20_000 });
    await expect(card.getByText(REDACTED_MATCHUP)).toBeVisible();

    await card.click();

    // Summary still renders (the matchup/banding header is fine for
    // any tier), but the body must collapse to the upsell.
    await expect(page.getByTestId("debate-detail-summary")).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByTestId("debate-detail-upsell")).toBeVisible();
    await expect(
      page.getByTestId("button-debate-detail-upgrade"),
    ).toBeVisible();

    // Analyst persona blocks and moderator synthesis must NOT
    // render — those are the Pro-only payloads we're gating.
    await expect(page.getByTestId("debate-detail-moderator")).toHaveCount(0);
    await expect(
      page.locator('[data-testid^="debate-detail-persona-"]'),
    ).toHaveCount(0);

    // And no reasoning text should leak anywhere on the page even
    // if a future regression sneaks the verdicts back into the DOM.
    await expect(page.getByText(QUANT_REASONING)).toHaveCount(0);
    await expect(page.getByText(NARRATIVE_REASONING)).toHaveCount(0);
    await expect(page.getByText(PRO_SYNTHESIS)).toHaveCount(0);
  });
});
