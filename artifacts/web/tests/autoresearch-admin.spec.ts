import { test, expect, type Page, type Route } from "@playwright/test";
import { clerk, clerkSetup } from "@clerk/testing/playwright";

/**
 * AutoResearch admin tab e2e (Task #420).
 *
 * Covers the new System → Research panel
 * (`artifacts/web/src/components/intelligence/autoresearch-panel.tsx`)
 * plus the audit-log entries written by the underlying admin endpoints
 * (`/api/admin/autoresearch/{features,run,features/{name}/decision}`
 * and `/api/admin/audit-log`).
 *
 * Backend interaction is mocked at the network layer with `page.route`
 * so the test does not depend on the discovery scheduler having any
 * real candidates to act on. A single in-test `state` object stitches
 * the four endpoints together — promote/reject/reset mutate the seeded
 * candidate's status AND append a synthetic `admin_audit_log` row, so
 * switching to the Audit tab actually surfaces the operator's
 * decisions, exactly the way it would against a live API.
 *
 * Sign-in uses Clerk's testing helper (passwordless email-ticket
 * strategy) so the test works with dev keys alone.
 *
 * Required env:
 *   - `CLERK_PUBLISHABLE_KEY` + `CLERK_SECRET_KEY` (Clerk dev keys).
 *   - `E2E_CLERK_ADMIN_EMAIL` (admin operator email).
 *
 * Skips itself when those are absent so CI without Clerk creds keeps
 * passing the rest of the suite.
 */

const ADMIN_EMAIL = process.env.E2E_CLERK_ADMIN_EMAIL;
const HAS_CLERK_DEV_KEYS = Boolean(
  process.env.CLERK_PUBLISHABLE_KEY && process.env.CLERK_SECRET_KEY,
);

type AutoResearchFeatureFixture = {
  id: number;
  name: string;
  vertical: string;
  signal_type: string;
  aggregator: string;
  status: "shadow" | "production" | "rejected";
  ic: number;
  sharpe: number;
  monotonicity: number;
  n_samples: number;
  last_evaluated_at: string;
  promoted_at: string | null;
  promoted_by: string | null;
  rejected_at: string | null;
  rejected_by: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
};

type AuditEntryFixture = {
  id: number;
  actor_user_id: string;
  actor_email: string;
  target_user_id: string | null;
  target_email: string | null;
  action: string;
  before_value: Record<string, unknown> | null;
  after_value: Record<string, unknown> | null;
  created_at: string;
};

type DecisionKind = "promote" | "reject" | "reset";

const DECISION_TO_STATUS: Record<DecisionKind, AutoResearchFeatureFixture["status"]> = {
  promote: "production",
  reject: "rejected",
  reset: "shadow",
};

function makeFeature(): AutoResearchFeatureFixture {
  return {
    id: 1,
    name: "uw__sentiment__beta__crypto",
    vertical: "crypto",
    signal_type: "sentiment",
    aggregator: "beta",
    status: "shadow",
    ic: 0.214,
    sharpe: 1.42,
    monotonicity: 0.81,
    n_samples: 1280,
    last_evaluated_at: "2026-04-24T11:30:00Z",
    promoted_at: null,
    promoted_by: null,
    rejected_at: null,
    rejected_by: null,
    metadata: {},
    created_at: "2026-04-20T08:15:00Z",
  };
}

/**
 * Wire up the four endpoints the AutoResearch panel + Audit tab hit.
 *
 * Returns the shared mutable state so individual test steps can read
 * the latest snapshot (e.g. assert auditEntries.length grew by N).
 */
async function installAutoResearchMocks(page: Page) {
  const state: {
    feature: AutoResearchFeatureFixture;
    auditEntries: AuditEntryFixture[];
    decisionCalls: Array<{ name: string; decision: DecisionKind }>;
    runCalls: number;
    nextAuditId: number;
  } = {
    feature: makeFeature(),
    auditEntries: [],
    decisionCalls: [],
    runCalls: 0,
    nextAuditId: 1000,
  };

  const fulfillJson = (route: Route, body: unknown, status = 200) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });

  // GET /api/admin/autoresearch/features (no path suffix).
  await page.route(/\/api\/admin\/autoresearch\/features(?:\?|$)/, (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return fulfillJson(route, {
      items: [state.feature],
      scheduler: {
        enabled: true,
        running: true,
        interval_seconds: 1800,
        last_run_at: "2026-04-24T11:00:00Z",
      },
    });
  });

  // POST /api/admin/autoresearch/features/{name}/decision
  await page.route(/\/api\/admin\/autoresearch\/features\/[^/]+\/decision$/, (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const url = new URL(route.request().url());
    const name = decodeURIComponent(
      url.pathname.split("/features/")[1].split("/decision")[0],
    );
    let body: { decision?: DecisionKind } = {};
    try {
      body = JSON.parse(route.request().postData() ?? "{}");
    } catch {
      // ignore — caught by the assertion below
    }
    const decision = body.decision;
    if (
      decision !== "promote" &&
      decision !== "reject" &&
      decision !== "reset"
    ) {
      return fulfillJson(route, { detail: `bad decision ${String(decision)}` }, 400);
    }
    const before = { ...state.feature };
    const newStatus = DECISION_TO_STATUS[decision];
    state.feature = {
      ...state.feature,
      status: newStatus,
      promoted_at: newStatus === "production" ? new Date().toISOString() : null,
      promoted_by: newStatus === "production" ? "admin@e2e.local" : null,
      rejected_at: newStatus === "rejected" ? new Date().toISOString() : null,
      rejected_by: newStatus === "rejected" ? "admin@e2e.local" : null,
    };
    state.decisionCalls.push({ name, decision });
    state.auditEntries.unshift({
      id: state.nextAuditId++,
      actor_user_id: "user_admin_e2e",
      actor_email: "admin@e2e.local",
      target_user_id: null,
      target_email: `autoresearch:${name}`,
      action: `autoresearch_${decision}`,
      before_value: {
        status: before.status,
        ic: before.ic,
        sharpe: before.sharpe,
      },
      after_value: {
        status: newStatus,
        ic: state.feature.ic,
        sharpe: state.feature.sharpe,
      },
      created_at: new Date().toISOString(),
    });
    return fulfillJson(route, state.feature);
  });

  // POST /api/admin/autoresearch/run
  await page.route(/\/api\/admin\/autoresearch\/run$/, (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    state.runCalls += 1;
    return fulfillJson(route, {
      started_at: "2026-04-24T11:45:00Z",
      completed_at: "2026-04-24T11:45:02Z",
      duration_seconds: 2.3,
      verticals_examined: 6,
      candidates_evaluated: 4,
      candidates_persisted: 1,
      insufficient_samples: 0,
      errors: [],
    });
  });

  // GET /api/admin/audit-log?limit=...&offset=...
  await page.route(/\/api\/admin\/audit-log(?:\?|$)/, (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const url = new URL(route.request().url());
    const action = url.searchParams.get("action") ?? "";
    const target = url.searchParams.get("target") ?? "";
    const limit = Math.max(1, Number(url.searchParams.get("limit") ?? 50));
    const offset = Math.max(0, Number(url.searchParams.get("offset") ?? 0));
    let items = state.auditEntries.slice();
    if (action) items = items.filter((e) => e.action.includes(action));
    if (target) {
      items = items.filter((e) =>
        (e.target_email ?? e.target_user_id ?? "").includes(target),
      );
    }
    const total = items.length;
    const paged = items.slice(offset, offset + limit);
    return fulfillJson(route, { items: paged, total, limit, offset });
  });

  return state;
}

test.describe("System → Research (AutoResearch admin tab)", () => {
  test.skip(
    !HAS_CLERK_DEV_KEYS || !ADMIN_EMAIL,
    "Clerk dev keys (CLERK_PUBLISHABLE_KEY/CLERK_SECRET_KEY) and E2E_CLERK_ADMIN_EMAIL required",
  );

  test.beforeAll(async () => {
    await clerkSetup();
  });

  // Track every confirm() the panel pops so we can assert the
  // confirmation UX stays wired (run + each promote/reject/reset).
  let dialogMessages: string[] = [];

  test.beforeEach(async ({ page }) => {
    dialogMessages = [];
    page.on("dialog", (dialog) => {
      dialogMessages.push(dialog.message());
      void dialog.accept();
    });
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: ADMIN_EMAIL! });
  });

  test("table renders, run-cycle posts, and promote/reject/reset write audit entries", async ({
    page,
  }) => {
    const state = await installAutoResearchMocks(page);

    await page.goto("/system?tab=research");

    const shell = page.getByTestId("shell-main");
    if ((await shell.count()) === 0) {
      test.skip(true, "admin user not approved — shell not rendered");
    }

    // Panel + table should mount and surface our seeded candidate.
    const panel = page.getByTestId("panel-system-research");
    await expect(panel).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("card-autoresearch")).toBeVisible();
    await expect(page.getByTestId("table-autoresearch")).toBeVisible();

    const featureName = state.feature.name;
    const row = page.getByTestId(`row-autoresearch-${featureName}`);
    await expect(row).toBeVisible();
    const statusPill = page.getByTestId(`pill-autoresearch-${featureName}-status`);
    await expect(statusPill).toHaveText(/SHADOW/);

    // Scheduler footer comes from the GET /features payload above.
    await expect(page.getByTestId("text-autoresearch-scheduler")).toContainText(
      /SCHED ON/,
    );

    // Run cycle — the panel pops a confirm() (auto-accepted in
    // beforeEach), POSTs /run, then renders the EVAL/PERSIST/duration
    // summary on success.
    await page.getByTestId("button-autoresearch-run").click();
    await expect(page.getByTestId("text-autoresearch-run-result")).toContainText(
      /EVAL 4.*PERSIST 1.*2\.3s/,
    );
    expect(state.runCalls).toBe(1);

    // PROMOTE — status pill flips to PRODUCTION and Promote disables.
    await page.getByTestId(`button-promote-${featureName}`).click();
    await expect(statusPill).toHaveText(/PRODUCTION/, { timeout: 10_000 });
    await expect(page.getByTestId(`button-promote-${featureName}`)).toBeDisabled();

    // REJECT — pill flips to REJECTED and Reject disables.
    await page.getByTestId(`button-reject-${featureName}`).click();
    await expect(statusPill).toHaveText(/REJECTED/, { timeout: 10_000 });
    await expect(page.getByTestId(`button-reject-${featureName}`)).toBeDisabled();

    // RESET — pill returns to SHADOW and both Promote / Reject re-enable.
    await page.getByTestId(`button-reset-${featureName}`).click();
    await expect(statusPill).toHaveText(/SHADOW/, { timeout: 10_000 });
    await expect(page.getByTestId(`button-promote-${featureName}`)).toBeEnabled();
    await expect(page.getByTestId(`button-reject-${featureName}`)).toBeEnabled();

    // All three decisions hit the API in order, with the right body.
    expect(state.decisionCalls).toEqual([
      { name: featureName, decision: "promote" },
      { name: featureName, decision: "reject" },
      { name: featureName, decision: "reset" },
    ]);

    // Each guarded action must have popped its own confirm() — one for
    // run + one per decision (4 total, in order). Verifying the exact
    // text catches accidental removal of the confirmation UX.
    expect(dialogMessages).toEqual([
      "Run the AutoResearch mining cycle now?",
      `Are you sure you want to promote candidate "${featureName}"?`,
      `Are you sure you want to reject candidate "${featureName}"?`,
      `Are you sure you want to reset candidate "${featureName}"?`,
    ]);

    // Audit-log tab must reflect every decision. Three new rows, newest
    // first, each tagged with the matching `autoresearch_*` action and
    // the candidate name in the target column.
    await page.goto("/system?tab=audit");
    await expect(page.getByTestId("panel-system-audit")).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByTestId("panel-audit-log")).toBeVisible();

    const rows = page.locator('[data-testid^="row-audit-"]');
    await expect(rows).toHaveCount(3, { timeout: 15_000 });
    // Newest first → reset, reject, promote.
    await expect(rows.nth(0)).toContainText("autoresearch_reset");
    await expect(rows.nth(0)).toContainText(`autoresearch:${featureName}`);
    await expect(rows.nth(1)).toContainText("autoresearch_reject");
    await expect(rows.nth(2)).toContainText("autoresearch_promote");
    // Before/after JSON column should mention the status transitions.
    await expect(rows.nth(2)).toContainText(/"status":"shadow"/);
    await expect(rows.nth(2)).toContainText(/"status":"production"/);
  });
});
