import { test, expect, type Page } from "@playwright/test";
import { clerk, clerkSetup } from "@clerk/testing/playwright";

/**
 * Mobile operator-journey e2e (Task #244).
 *
 * Drives the Expo react-native-web export of `artifacts/mobile`
 * through the same sign-in → dashboard → sign-out path the web
 * operator-journey spec exercises. The dashboard assertions hit the
 * exact same `data-testid`s the web suite uses
 * (`card-recent-trades`, `card-trading-cycles`,
 * `card-health-snapshot`) — react-native-web converts each
 * `testID` prop into `data-testid` on the rendered DOM node, so the
 * operator-facing KPIs we check on web have to be present on mobile
 * too.
 *
 * Like the web spec, the auth-gated tests skip when Clerk dev keys
 * (`CLERK_PUBLISHABLE_KEY` + `CLERK_SECRET_KEY`) or the operator
 * email (`E2E_CLERK_USER_EMAIL`) are missing so the public bits can
 * still smoke-test in CI without a Clerk testing instance.
 */

const CLERK_USER_EMAIL = process.env.E2E_CLERK_USER_EMAIL;
const HAS_CLERK_DEV_KEYS = Boolean(
  process.env.CLERK_PUBLISHABLE_KEY && process.env.CLERK_SECRET_KEY,
);
const HAS_CLERK_AUTH = HAS_CLERK_DEV_KEYS && Boolean(CLERK_USER_EMAIL);

async function expectNoBundleError(page: Page) {
  await expect(page.locator("text=ChunkLoadError")).toHaveCount(0);
  await expect(
    page.locator("text=Failed to fetch dynamically imported module"),
  ).toHaveCount(0);
  await expect(
    page.locator("text=Unable to resolve module"),
  ).toHaveCount(0);
}

test.describe("mobile operator journey — public surface", () => {
  test("Expo web bundle mounts and routes signed-out users to sign-in", async ({
    page,
  }) => {
    await page.goto("/");
    // The Expo web bundle hydrates into <div id="root"> (or the
    // body for newer expo-router builds). Either way, *something*
    // must render — a blank screen is the regression we care about.
    await expect(page.locator("body")).not.toBeEmpty({ timeout: 20_000 });
    // Signed-out users are redirected to the (auth) group's
    // sign-in screen by `app/(tabs)/_layout.tsx`. The screen
    // renders the OPERATOR SIGN-IN headline.
    await expect(
      page.getByText(/operator sign-in/i).first(),
    ).toBeVisible({ timeout: 20_000 });
    await expectNoBundleError(page);
  });
});

test.describe("mobile operator journey — authenticated walk-through", () => {
  test.skip(
    !HAS_CLERK_AUTH,
    "Clerk dev keys (CLERK_PUBLISHABLE_KEY/CLERK_SECRET_KEY) and E2E_CLERK_USER_EMAIL are required",
  );

  test.beforeAll(async () => {
    await clerkSetup();
  });

  test.beforeEach(async ({ page }) => {
    // Land on the bundle so Clerk has time to initialise before we
    // mint a sign-in ticket. Same pattern as the web suite.
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_USER_EMAIL! });
  });

  test("dashboard renders the same KPIs/cards as the web operator journey", async ({
    page,
  }) => {
    // The (tabs)/index screen is the dashboard; on react-native-web
    // it's served at `/` once signed in.
    await page.goto("/");

    // The dashboard fetches `/api/dashboard/overview`; if the user
    // isn't approved the API returns 403 and the screen renders the
    // AWAITING APPROVAL panel instead of the KPIs. Skip rather than
    // fail — matches the web spec's `shell-main` gate.
    const awaiting = page.getByText(/awaiting approval/i);
    if (await awaiting.isVisible().catch(() => false)) {
      test.skip(true, "user not approved — dashboard not rendered");
    }

    await expect(page.getByTestId("card-recent-trades")).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByTestId("card-trading-cycles")).toBeVisible();
    await expect(page.getByTestId("card-health-snapshot")).toBeVisible();
    await expect(page.getByTestId("overview-period-toggle")).toBeVisible();
    await expectNoBundleError(page);
  });

  test("sign-out clears the session and bounces back to sign-in", async ({
    page,
  }) => {
    await page.goto("/");

    const awaiting = page.getByText(/awaiting approval/i);
    const onDashboard = await page
      .getByTestId("card-recent-trades")
      .isVisible()
      .catch(() => false);
    if (
      !onDashboard &&
      !(await awaiting.isVisible().catch(() => false))
    ) {
      test.skip(true, "dashboard never rendered — nothing to sign out from");
    }

    await clerk.signOut({ page });

    // After sign-out the (tabs) layout flips back to the sign-in
    // redirect. We assert the OPERATOR SIGN-IN headline reappears
    // and the dashboard KPIs are gone.
    await page.goto("/");
    await expect(
      page.getByText(/operator sign-in/i).first(),
    ).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("card-recent-trades")).toHaveCount(0);
    await expectNoBundleError(page);
  });
});
