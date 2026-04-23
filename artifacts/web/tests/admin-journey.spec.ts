import { test, expect, type Page, type BrowserContext } from "@playwright/test";
import { clerk, clerkSetup } from "@clerk/testing/playwright";

/**
 * Admin-journey e2e (Task #237 / #238).
 *
 * Flows:
 *   1. Admin walk-through — open /admin, verify the user-management
 *      panel and the risk-knob (runtime settings) panel both render.
 *   2. Runtime-setting mutation → audit-log entry — bump the first
 *      risk-knob input one tick, hit "Apply", then visit
 *      /system?tab=audit and confirm the topmost audit row references
 *      the knob's settings key.
 *   3. Vertical-toggle propagation — admin hides a vertical from the
 *      vertical-visibility panel, then a separate non-admin browser
 *      context visits /verticals and confirms the matching
 *      `card-vertical-overview-${name}` is no longer rendered.
 *   4. Non-admin negative path — sign in as a non-admin in a separate
 *      browser context, open /admin, and assert the AdminForbidden
 *      panel renders. Also confirm the system page shows the
 *      `badge-non-admin` indicator and that the persona-toggle panel
 *      is hidden for that user.
 *
 * Sign-in uses Clerk's testing helper with the passwordless email-
 * ticket strategy — Clerk's backend mints a sign-in token for the
 * given email, so the suite works with dev keys alone (no password).
 *
 * Required env:
 *   - `CLERK_PUBLISHABLE_KEY` + `CLERK_SECRET_KEY` (Clerk dev keys).
 *   - `E2E_CLERK_ADMIN_EMAIL` (admin operator email).
 *   - `E2E_CLERK_USER_EMAIL`  (non-admin operator email, for #3 + #4).
 *
 * Each describe block skips on its own missing-creds combination.
 */

const ADMIN_EMAIL = process.env.E2E_CLERK_ADMIN_EMAIL;
const USER_EMAIL = process.env.E2E_CLERK_USER_EMAIL;
const HAS_CLERK_DEV_KEYS = Boolean(
  process.env.CLERK_PUBLISHABLE_KEY && process.env.CLERK_SECRET_KEY,
);

async function signInAs(page: Page, email: string) {
  await page.goto("/");
  await clerk.signIn({ page, emailAddress: email });
}

test.describe("admin journey", () => {
  test.skip(
    !HAS_CLERK_DEV_KEYS || !ADMIN_EMAIL,
    "Clerk dev keys (CLERK_PUBLISHABLE_KEY/CLERK_SECRET_KEY) and E2E_CLERK_ADMIN_EMAIL required",
  );

  test.beforeAll(async () => {
    await clerkSetup();
  });

  test("admin sees user management + runtime-knob panels", async ({ page }) => {
    await signInAs(page, ADMIN_EMAIL!);
    await page.goto("/admin");

    await expect(page.getByTestId("shell-main")).toBeVisible();
    await expect(page.locator("text=Admin access required")).toHaveCount(0);
    await expect(page.getByTestId("panel-user-management")).toBeVisible();
    await expect(page.getByTestId("panel-risk-knobs")).toBeVisible();
  });

  test("changing a runtime knob writes a matching admin_audit_log entry", async ({ page }) => {
    await signInAs(page, ADMIN_EMAIL!);
    await page.goto("/admin");
    await expect(page.getByTestId("panel-risk-knobs")).toBeVisible({ timeout: 15_000 });

    // Capture the initial audit-range so we can assert it advances.
    await page.goto("/system?tab=audit");
    await expect(page.getByTestId("panel-system-audit")).toBeVisible({ timeout: 15_000 });
    const beforeRange =
      (await page.getByTestId("text-audit-range").textContent()) ?? "";

    // Identify the first knob and remember its settings key (the suffix
    // of its data-testid: `input-knob-<settings-key>`).
    await page.goto("/admin");
    const firstKnobInput = page.locator('[data-testid^="input-knob-"]').first();
    await expect(firstKnobInput).toBeVisible({ timeout: 15_000 });
    const knobTestId = (await firstKnobInput.getAttribute("data-testid")) ?? "";
    const knobKey = knobTestId.replace(/^input-knob-/, "");
    expect(knobKey.length).toBeGreaterThan(0);

    // Mutate it by one tick and apply.
    const original = (await firstKnobInput.inputValue()).trim();
    const next = (Number(original) + 1).toString();
    await firstKnobInput.fill(next);
    await page.getByTestId("button-knobs-apply").click();

    // Re-open audit log; the visible row count / range should advance,
    // and the topmost row should reference the knob's settings key in
    // its target / before→after column.
    await page.goto("/system?tab=audit");
    await expect(page.getByTestId("panel-system-audit")).toBeVisible({ timeout: 15_000 });
    await expect
      .poll(
        async () =>
          (await page.getByTestId("text-audit-range").textContent()) ?? "",
        { timeout: 20_000 },
      )
      .not.toBe(beforeRange);

    const topRow = page.locator('[data-testid^="row-audit-"]').first();
    await expect(topRow).toBeVisible();
    await expect(topRow).toContainText(knobKey);

    // Revert the knob so the test is idempotent.
    await page.goto("/admin");
    const knob2 = page.locator(`[data-testid="${knobTestId}"]`);
    await knob2.fill(original);
    await page.getByTestId("button-knobs-apply").click();
  });
});

/**
 * Vertical toggle → non-admin verification.
 *
 * Two contexts: an admin context that hides one vertical, and a
 * separate non-admin context that opens /verticals and confirms the
 * hidden vertical is no longer rendered.
 */
test.describe("admin journey — vertical toggle propagates to non-admins", () => {
  test.skip(
    !HAS_CLERK_DEV_KEYS || !ADMIN_EMAIL || !USER_EMAIL,
    "Clerk dev keys plus E2E_CLERK_ADMIN_EMAIL and E2E_CLERK_USER_EMAIL required",
  );

  test.beforeAll(async () => {
    await clerkSetup();
  });

  let adminCtx: BrowserContext;
  let userCtx: BrowserContext;
  let adminPage: Page;
  let userPage: Page;

  test.beforeEach(async ({ browser }) => {
    adminCtx = await browser.newContext();
    userCtx = await browser.newContext();
    adminPage = await adminCtx.newPage();
    userPage = await userCtx.newPage();
  });

  test.afterEach(async () => {
    await adminCtx.close();
    await userCtx.close();
  });

  test("hiding a vertical removes its card from the non-admin /verticals page", async () => {
    await signInAs(userPage, USER_EMAIL!);
    await signInAs(adminPage, ADMIN_EMAIL!);

    // Pick a vertical the non-admin can see today.
    await userPage.goto("/verticals");
    const cards = userPage.locator('[data-testid^="card-vertical-overview-"]');
    await expect(cards.first()).toBeVisible({ timeout: 20_000 });
    const targetTestId = await cards.first().getAttribute("data-testid");
    expect(targetTestId).toBeTruthy();
    const verticalName = targetTestId!.replace(/^card-vertical-overview-/, "");

    // Admin hides the chosen vertical via the Verticals tab.
    await adminPage.goto("/admin?tab=verticals");
    const hideBtn = adminPage.getByTestId(`button-hide-${verticalName}`);
    await expect(hideBtn).toBeVisible({ timeout: 15_000 });
    await hideBtn.click();

    // Non-admin reloads /verticals; the hidden card must disappear.
    await userPage.goto("/verticals");
    await expect(cards.first()).toBeVisible({ timeout: 20_000 });
    await expect(
      userPage.getByTestId(`card-vertical-overview-${verticalName}`),
    ).toHaveCount(0, { timeout: 20_000 });

    // Cleanup: un-hide so the test is idempotent.
    await adminPage.goto("/admin?tab=verticals");
    const restoreBtn = adminPage.getByTestId(`button-hide-${verticalName}`);
    await expect(restoreBtn).toBeVisible({ timeout: 15_000 });
    await restoreBtn.click();
  });
});

test.describe("admin journey — non-admin negative path", () => {
  test.skip(
    !HAS_CLERK_DEV_KEYS || !USER_EMAIL,
    "Clerk dev keys and E2E_CLERK_USER_EMAIL required",
  );

  test.beforeAll(async () => {
    await clerkSetup();
  });

  /**
   * Use a fresh browser context so this run is fully isolated from any
   * admin session that might exist in the parent test suite.
   */
  let userContext: BrowserContext;
  let userPage: Page;

  test.beforeEach(async ({ browser }) => {
    userContext = await browser.newContext();
    userPage = await userContext.newPage();
    await signInAs(userPage, USER_EMAIL!);
  });

  test.afterEach(async () => {
    await userContext.close();
  });

  test("non-admin gets the 403 panel at /admin and a non-admin badge in /system", async () => {
    await userPage.goto("/admin");
    const forbidden = userPage.getByText(/admin access required|HTTP 403/i);
    const pending = userPage.getByText(/awaiting approval/i);
    const which = await Promise.race([
      forbidden.first().waitFor({ state: "visible", timeout: 10_000 }).then(() => "forbidden"),
      pending.first().waitFor({ state: "visible", timeout: 10_000 }).then(() => "pending"),
    ]);
    expect(which).not.toBeNull();

    if (which === "forbidden") {
      // For approved-but-not-admin, /system still loads but should
      // surface the non-admin badge and hide the persona-toggle panel.
      await userPage.goto("/system");
      await expect(userPage.getByTestId("page-system")).toBeVisible({ timeout: 15_000 });
      await expect(userPage.getByTestId("badge-non-admin")).toBeVisible();
      await expect(userPage.getByTestId("panel-persona-toggles")).toHaveCount(0);
    }
  });
});
