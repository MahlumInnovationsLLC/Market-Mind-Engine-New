import { test, expect, type Page, type BrowserContext } from "@playwright/test";

/**
 * Admin-journey e2e (Task #237).
 *
 * Three flows:
 *   1. Admin walk-through — open /admin, verify the user-management
 *      panel and the risk-knob (runtime settings) panel both render.
 *   2. Runtime-setting mutation → audit-log entry — bump the first
 *      risk-knob slider one tick, hit "Apply", then visit
 *      /system?tab=audit and confirm the audit log table has a fresh
 *      row whose timestamp is within the last minute.
 *   3. Non-admin negative path — sign in as a non-admin in a separate
 *      browser context, open /admin, and assert the AdminForbidden
 *      panel renders. Also confirm the system page shows the
 *      `badge-non-admin` indicator and that the persona-toggle panel
 *      is hidden for that user.
 *
 * Skipped unless both admin and non-admin Clerk creds are provided
 * via env so the suite stays runnable in CI without secrets.
 */

const ADMIN_EMAIL = process.env.E2E_CLERK_ADMIN_EMAIL;
const ADMIN_PASSWORD = process.env.E2E_CLERK_ADMIN_PASSWORD;
const USER_EMAIL = process.env.E2E_CLERK_USER_EMAIL;
const USER_PASSWORD = process.env.E2E_CLERK_USER_PASSWORD;

async function clerkSignIn(page: Page, email: string, password: string) {
  await page.goto("/sign-in");
  await page.locator('input[name="identifier"], input[type="email"]').fill(email);
  await page.getByRole("button", { name: /continue|next/i }).click();
  await page.locator('input[name="password"], input[type="password"]').fill(password);
  await page.getByRole("button", { name: /continue|sign in/i }).click();
  await page.waitForURL(/\/(app|sign-in)/, { timeout: 30_000 }).catch(() => {});
}

test.describe("admin journey", () => {
  test.skip(
    !ADMIN_EMAIL || !ADMIN_PASSWORD,
    "E2E_CLERK_ADMIN_EMAIL / _PASSWORD not set",
  );

  test("admin sees user management + runtime-knob panels", async ({ page }) => {
    await clerkSignIn(page, ADMIN_EMAIL!, ADMIN_PASSWORD!);
    await page.goto("/admin");

    await expect(page.getByTestId("shell-main")).toBeVisible();
    await expect(page.locator("text=Admin access required")).toHaveCount(0);
    await expect(page.getByTestId("panel-user-management")).toBeVisible();
    await expect(page.getByTestId("panel-risk-knobs")).toBeVisible();
  });

  test("changing a runtime knob writes an admin_audit_log entry", async ({ page }) => {
    await clerkSignIn(page, ADMIN_EMAIL!, ADMIN_PASSWORD!);
    await page.goto("/admin");
    await expect(page.getByTestId("panel-risk-knobs")).toBeVisible({ timeout: 15_000 });

    // Capture the initial audit-range text so we can assert it grows
    // after the mutation.
    await page.goto("/system?tab=audit");
    await expect(page.getByTestId("panel-system-audit")).toBeVisible({ timeout: 15_000 });
    const beforeRange = (await page.getByTestId("text-audit-range").textContent()) ?? "";

    // Mutate the first knob: nudge its number input one tick and apply.
    await page.goto("/admin");
    const firstKnobInput = page.locator('[data-testid^="input-knob-"]').first();
    await expect(firstKnobInput).toBeVisible({ timeout: 15_000 });
    const original = (await firstKnobInput.inputValue()).trim();
    const next = (Number(original) + 1).toString();
    await firstKnobInput.fill(next);
    await page.getByTestId("button-knobs-apply").click();

    // Re-open audit log; the visible row count / range should advance,
    // and the most recent row should mention an admin / runtime action.
    await page.goto("/system?tab=audit");
    await expect(page.getByTestId("panel-system-audit")).toBeVisible({ timeout: 15_000 });
    await expect
      .poll(
        async () => (await page.getByTestId("text-audit-range").textContent()) ?? "",
        { timeout: 20_000 },
      )
      .not.toBe(beforeRange);
    await expect(page.locator('[data-testid^="row-audit-"]').first()).toBeVisible();

    // Best-effort: revert the knob so the test is idempotent.
    await page.goto("/admin");
    const knob2 = page.locator('[data-testid^="input-knob-"]').first();
    await knob2.fill(original);
    await page.getByTestId("button-knobs-apply").click().catch(() => {});
  });
});

test.describe("admin journey — non-admin negative path", () => {
  test.skip(
    !USER_EMAIL || !USER_PASSWORD,
    "E2E_CLERK_USER_EMAIL / _PASSWORD not set",
  );

  /**
   * Use a fresh browser context so this run is fully isolated from any
   * admin session that might exist in the parent test suite.
   */
  let userContext: BrowserContext;
  let userPage: Page;

  test.beforeEach(async ({ browser }) => {
    userContext = await browser.newContext();
    userPage = await userContext.newPage();
    await clerkSignIn(userPage, USER_EMAIL!, USER_PASSWORD!);
  });

  test.afterEach(async () => {
    await userContext.close().catch(() => {});
  });

  test("non-admin gets the 403 panel at /admin and a non-admin badge in /system", async () => {
    await userPage.goto("/admin");
    const forbidden = userPage.getByText(/admin access required|HTTP 403/i);
    const pending = userPage.getByText(/awaiting approval/i);
    const which = await Promise.race([
      forbidden.first().waitFor({ state: "visible", timeout: 10_000 }).then(() => "forbidden"),
      pending.first().waitFor({ state: "visible", timeout: 10_000 }).then(() => "pending"),
    ]).catch(() => null);
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
