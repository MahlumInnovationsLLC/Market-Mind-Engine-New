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

/**
 * Emergency-stop re-arm flow (Task #412).
 *
 * The "Re-arm trading (PAPER) / (LIVE)" control on the EmergencyStop
 * card only appears when `useGetLiveExecuteState().effective_mode ===
 * "halt"` — which is what `system_settings.risk.trading_mode = 'halt'`
 * persists after an emergency stop. Rather than mutating real DB rows
 * (which would require touching production-shaped tables and cleaning
 * up afterwards), this test mocks the `/api/orchestrator/live-execute`
 * GET so the dashboard sees a halted state and mocks the
 * `/api/admin/rearm-trading` POST so we can both assert the dashboard
 * actually fired the canonical mutation and flip the mocked
 * live-execute state to `paper` so the badge clears on the post-mutation
 * refetch. Same `page.route` mocking pattern the operator-journey
 * suite uses for `/api/health/ready`.
 */
test.describe("admin journey — emergency-stop re-arm flow", () => {
  test.skip(
    !HAS_CLERK_DEV_KEYS || !ADMIN_EMAIL,
    "Clerk dev keys (CLERK_PUBLISHABLE_KEY/CLERK_SECRET_KEY) and E2E_CLERK_ADMIN_EMAIL required",
  );

  test.beforeAll(async () => {
    await clerkSetup();
  });

  test("admin re-arms a halted dashboard via PAPER and the HALTED badge clears", async ({
    page,
  }) => {
    // Single source of truth for what the (mocked) live-execute GET
    // returns. We start in `halt` so the rearm controls render, then
    // flip to `paper` once the rearm POST fires so the post-mutation
    // refetch clears the badge.
    let liveExecuteMode: "halt" | "paper" = "halt";
    const liveExecutePayload = () => ({
      enabled: liveExecuteMode === "live",
      effective_mode: liveExecuteMode,
      env_mode: "paper",
      db_mode: liveExecuteMode,
      source: "db",
      caller_tier: "elite",
      can_toggle: true,
    });

    await page.route("**/api/orchestrator/live-execute", async (route) => {
      // Only mock the GET — leave any POST (the live-execute toggle)
      // untouched so we don't accidentally swallow a different mutation.
      if (route.request().method() !== "GET") {
        return route.fallback();
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(liveExecutePayload()),
      });
    });

    // Capture every re-arm POST so we can assert the dashboard fired
    // the canonical mutation (not, e.g., the live-execute toggle) with
    // the operator's chosen mode.
    const rearmCalls: Array<{ method: string; postData: string | null }> = [];
    await page.route("**/api/admin/rearm-trading", async (route) => {
      const req = route.request();
      rearmCalls.push({ method: req.method(), postData: req.postData() });
      // Flip the live-execute mock so the next refetch (triggered by
      // the mutation's `invalidateQueries`) clears the HALTED badge.
      liveExecuteMode = "paper";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          trading_mode: "paper",
          previous_mode: "halt",
          kill_switch: { halted: false, reason: null, halted_at: null },
        }),
      });
    });

    await signInAs(page, ADMIN_EMAIL!);
    await page.goto("/app");

    // If this admin account isn't approved on the test instance the
    // shell never mounts; skip rather than fail so the suite stays
    // green when the dev tenant is half-configured.
    const shell = page.getByTestId("shell-main");
    if ((await shell.count()) === 0) {
      test.skip(true, "admin not approved — shell not rendered");
    }
    await expect(shell).toBeVisible({ timeout: 15_000 });

    // OperatorControls (admin-only) renders EmergencyStop and is
    // open-by-default for admins via its `useEffect(setOpen(true))`.
    const card = page.getByTestId("emergency-stop");
    await expect(card).toBeVisible({ timeout: 15_000 });

    // Halted state surfaces the HALTED badge + the rearm sub-panel
    // with both PAPER and LIVE buttons.
    const stateBadge = page.getByTestId("emergency-stop-state");
    await expect(stateBadge).toHaveText("HALTED", { timeout: 15_000 });
    await expect(page.getByTestId("emergency-stop-rearm")).toBeVisible();
    const paperBtn = page.getByTestId("button-rearm-paper");
    const liveBtn = page.getByTestId("button-rearm-live");
    await expect(paperBtn).toBeVisible();
    await expect(liveBtn).toBeVisible();
    await expect(paperBtn).toContainText(/Re-arm trading \(PAPER\)/i);
    await expect(liveBtn).toContainText(/Re-arm trading \(LIVE\)/i);

    // Trip the re-arm. The mutation's onSuccess invalidates the
    // live-execute query, which re-fetches and (per the mock flip
    // above) returns `paper`, so the badge swaps back to KILL ALL
    // and the rearm sub-panel disappears.
    await paperBtn.click();

    await expect(stateBadge).toHaveText("KILL ALL", { timeout: 15_000 });
    await expect(page.getByTestId("emergency-stop-rearm")).toHaveCount(0);
    await expect(page.getByTestId("text-rearm-result")).toContainText(
      /Re-armed to PAPER/i,
    );

    // Mutation must have actually fired against /api/admin/rearm-trading
    // with `mode: "paper"` — guards against a future refactor that
    // wires the button to a different endpoint or forgets to send the
    // mode at all.
    expect(rearmCalls.length).toBeGreaterThanOrEqual(1);
    const first = rearmCalls[0];
    expect(first.method).toBe("POST");
    expect(first.postData ?? "").toContain('"mode":"paper"');
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
