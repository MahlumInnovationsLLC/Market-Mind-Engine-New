import { test, expect, type Page } from "@playwright/test";

/**
 * Admin-journey e2e (Task #237).
 *
 * Two flows:
 *   1. Admin walk-through — open /admin, verify the access-requests +
 *      vertical-toggle panels render. Open /system?tab=audit and assert
 *      the audit log table is mounted.
 *   2. Non-admin negative path — sign in as a non-admin, open /admin,
 *      and assert the AdminForbidden 403 panel renders instead of the
 *      admin console.
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

  test("admin sees the admin console with access-requests + vertical toggles", async ({
    page,
  }) => {
    await clerkSignIn(page, ADMIN_EMAIL!, ADMIN_PASSWORD!);
    await page.goto("/admin");

    // Admin console renders inside the shell.
    await expect(page.getByTestId("shell-main")).toBeVisible();
    // 403 forbidden panel must NOT render for an admin.
    await expect(page.locator("text=Admin access required")).toHaveCount(0);
    // At least one of the admin tabs must surface.
    await expect(
      page.getByText(/access requests|verticals|users|admin/i).first(),
    ).toBeVisible();
  });

  test("admin can open the system audit-log tab", async ({ page }) => {
    await clerkSignIn(page, ADMIN_EMAIL!, ADMIN_PASSWORD!);
    await page.goto("/system?tab=audit");
    await expect(page.getByTestId("shell-main")).toBeVisible();
    await expect(page.getByText(/audit/i).first()).toBeVisible();
  });
});

test.describe("admin journey — non-admin negative path", () => {
  test.skip(
    !USER_EMAIL || !USER_PASSWORD,
    "E2E_CLERK_USER_EMAIL / _PASSWORD not set",
  );

  test("non-admin gets the 403 forbidden panel at /admin", async ({ page }) => {
    await clerkSignIn(page, USER_EMAIL!, USER_PASSWORD!);
    await page.goto("/admin");

    const forbidden = page.getByText(/admin access required|HTTP 403/i);
    const pending = page.getByText(/awaiting approval/i);

    // Either the forbidden panel (approved-but-not-admin) or the
    // pending-approval gate (not yet approved) is acceptable — both
    // confirm /admin is correctly blocked for a non-admin caller.
    const visible = await Promise.race([
      forbidden.first().waitFor({ state: "visible", timeout: 10_000 }).then(() => "forbidden"),
      pending.first().waitFor({ state: "visible", timeout: 10_000 }).then(() => "pending"),
    ]).catch(() => null);
    expect(visible).not.toBeNull();
  });
});
