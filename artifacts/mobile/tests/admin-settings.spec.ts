import { test, expect, type Page, type Route } from "@playwright/test";
import { clerk, clerkSetup } from "@clerk/testing/playwright";

/**
 * Mobile admin settings e2e (Task #184).
 *
 * The Settings tab (`app/(tabs)/settings.tsx`) is the phone-based
 * twin of the web operator console's emergency-stop + LIVE_EXECUTE
 * controls — it talks to `/api/orchestrator/live-execute` and
 * `/api/admin/emergency-stop`. A regression here would silently lock
 * operators out of their kill switch, so we cover:
 *
 *   1. Tab visibility — non-admins must NOT see the System tab and
 *      direct navigation to /settings must NOT render the controls.
 *   2. Admins must see the System tab, and:
 *      - the trading-mode toggle must POST
 *        `/api/orchestrator/live-execute` with `{ enabled: true }`
 *        after the two-tap arm-then-confirm flow,
 *      - the emergency-stop confirm button must POST
 *        `/api/admin/emergency-stop` with the operator-supplied
 *        `reason` payload.
 *
 * To stay independent of whatever role the Clerk operator account
 * happens to hold today, we mock `/api/me/access` per-test instead
 * of relying on real admin assignment. Sign-in still uses the same
 * Clerk testing helper as `operator-journey.spec.ts` so the (tabs)
 * layout actually mounts.
 */

const CLERK_USER_EMAIL = process.env.E2E_CLERK_USER_EMAIL;
const HAS_CLERK_DEV_KEYS = Boolean(
  process.env.CLERK_PUBLISHABLE_KEY && process.env.CLERK_SECRET_KEY,
);
const HAS_CLERK_AUTH = HAS_CLERK_DEV_KEYS && Boolean(CLERK_USER_EMAIL);

type AccessShape = {
  approved: boolean;
  is_admin: boolean;
  email: string;
  user_id: string;
};

async function mockAccess(page: Page, isAdmin: boolean): Promise<void> {
  const body: AccessShape = {
    approved: true,
    is_admin: isAdmin,
    email: "operator@example.com",
    user_id: "test-user",
  };
  await page.route("**/api/me/access*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

/**
 * The (tabs) layout polls `/api/me/notifications?limit=50` for the
 * unread badge — stub it so the tab bar doesn't flap on mount.
 */
async function mockNotifications(page: Page): Promise<void> {
  await page.route("**/api/me/notifications*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ unread_count: 0, items: [] }),
    });
  });
}

test.describe("mobile admin settings — visibility + actions", () => {
  test.skip(
    !HAS_CLERK_AUTH,
    "Clerk dev keys (CLERK_PUBLISHABLE_KEY/CLERK_SECRET_KEY) and E2E_CLERK_USER_EMAIL are required",
  );

  test.beforeAll(async () => {
    await clerkSetup();
  });

  test.beforeEach(async ({ page }) => {
    // Land on the bundle so Clerk has time to initialise before we
    // mint a sign-in ticket. Same pattern as operator-journey.spec.
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_USER_EMAIL! });
  });

  test("non-admin users do not see the System tab and /settings does not render the controls", async ({
    page,
  }) => {
    await mockAccess(page, false);
    await mockNotifications(page);

    // Direct-navigate to /settings. settings.tsx redirects non-admins
    // back to the dashboard, and the (tabs) layout hides the tab via
    // `href: null`, so the controls and the System tab label must
    // never appear.
    await page.goto("/settings");

    // Wait for the tab bar to mount so we know the (tabs) layout has
    // resolved access — the Overview tab always renders for any
    // signed-in user, so its presence is a deterministic signal that
    // the layout has finished its access query.
    await expect(page.locator("text=Overview").first()).toBeVisible({
      timeout: 20_000,
    });

    await expect(page.getByTestId("button-live-execute-toggle")).toHaveCount(0);
    await expect(page.getByTestId("button-emergency-stop-arm")).toHaveCount(0);
    await expect(page.locator("text=System")).toHaveCount(0);
  });

  test("admin sees the System tab; toggle + emergency-stop POST the expected payloads", async ({
    page,
  }) => {
    await mockAccess(page, true);
    await mockNotifications(page);

    let liveState = {
      enabled: false,
      effective_mode: "paper",
      env_mode: "paper",
      db_mode: null as string | null,
      source: "env",
      caller_tier: "elite",
      can_toggle: true,
    };
    let togglePostBody: unknown = null;
    let stopPostBody: unknown = null;

    await page.route(
      "**/api/orchestrator/live-execute*",
      async (route: Route) => {
        const req = route.request();
        if (req.method() === "POST") {
          togglePostBody = req.postDataJSON();
          const enabled = !!(togglePostBody as { enabled?: boolean }).enabled;
          liveState = {
            ...liveState,
            enabled,
            effective_mode: enabled ? "live" : "paper",
            db_mode: enabled ? "live" : "paper",
            source: "db",
          };
        }
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(liveState),
        });
      },
    );

    await page.route(
      "**/api/admin/emergency-stop*",
      async (route: Route) => {
        const req = route.request();
        if (req.method() === "POST") {
          stopPostBody = req.postDataJSON();
        }
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            ok: true,
            canceled_orders: 3,
            trading_mode: "halt",
            reason: "settlement feed lag",
          }),
        });
      },
    );

    await page.goto("/settings");

    // Tab bar must surface "System" for admins.
    await expect(page.locator("text=System").first()).toBeVisible({
      timeout: 20_000,
    });
    // The trading-mode card must mount.
    await expect(page.getByTestId("button-live-execute-toggle")).toBeVisible({
      timeout: 20_000,
    });

    // First tap "arms" the confirm state, second tap fires the POST.
    await page.getByTestId("button-live-execute-toggle").click();
    await page.getByTestId("button-live-execute-toggle").click();

    await expect.poll(() => togglePostBody, { timeout: 10_000 }).toEqual({
      enabled: true,
    });

    // Badge should reflect the mocked LIVE response.
    await expect(page.getByTestId("live-execute-state")).toHaveText(/LIVE/, {
      timeout: 10_000,
    });

    // Emergency-stop arm → reason → confirm.
    await page.getByTestId("button-emergency-stop-arm").click();
    await page
      .getByTestId("input-emergency-stop-reason")
      .fill("settlement feed lag");
    await page.getByTestId("button-emergency-stop-confirm").click();

    await expect.poll(() => stopPostBody, { timeout: 10_000 }).toEqual({
      reason: "settlement feed lag",
    });

    // Result line should appear on success.
    await expect(page.getByTestId("text-emergency-stop-result")).toBeVisible({
      timeout: 10_000,
    });
  });
});
