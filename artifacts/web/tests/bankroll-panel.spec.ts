import { test, expect, type Page, type Route } from "@playwright/test";
import { clerk, clerkSetup } from "@clerk/testing/playwright";

/**
 * Bankroll-panel e2e (Task #410).
 *
 * Locks down the "Live Kalshi balance" tile that Task #176 added to the
 * operator dashboard. The Python helper behind
 * `/api/dashboard/bankroll/recent` already has unit coverage; what was
 * uncovered until now was the rendered UI — the current-balance text,
 * the 24h delta total, the per-reason breakdown, and the FRESH / STALE
 * / STOPPED freshness badge driven by `sync.last_tick_at` vs
 * `sync.interval_seconds`.
 *
 * Each case mocks `/api/dashboard/bankroll/recent` with a deterministic
 * feed so the assertions don't depend on whatever the live exchange
 * sync happens to be doing at test time.
 */

const CLERK_USER_EMAIL = process.env.E2E_CLERK_USER_EMAIL;
const HAS_CLERK_DEV_KEYS = Boolean(
  process.env.CLERK_PUBLISHABLE_KEY && process.env.CLERK_SECRET_KEY,
);
const HAS_CLERK_AUTH = HAS_CLERK_DEV_KEYS && Boolean(CLERK_USER_EMAIL);

type SyncState = {
  running: boolean;
  last_tick_at: number | null;
  interval_seconds: number;
  last_error?: string | null;
};

type BankrollItem = {
  id: number;
  occurred_at: string;
  exchange: string;
  reason: string;
  delta_cents: number;
  balance_cents: number;
};

type BankrollFeed = {
  exchange: string;
  current_balance_cents: number | null;
  last_event_at: string | null;
  items: BankrollItem[];
  sync: SyncState;
};

/**
 * Build a deterministic 4-row feed anchored at "now" so every event
 * lands inside the panel's 24h window regardless of when the test
 * runs.
 *
 *   sync     →   $0  delta @ -6h, balance $1,000.00
 *   fill     → +$50  delta @ -4h, balance $1,050.00
 *   fill     → -$20  delta @ -2h, balance $1,030.00
 *   deposit  → +$250 delta @ -1h, balance $1,280.00
 *
 *   current balance        = $1,280.00
 *   24h delta total        = +$280.00   (sync 0 + fill 30 + deposit 250)
 *   per-reason tile count  = 3          (sync, fill, deposit)
 */
function buildFeed(sync: SyncState): BankrollFeed {
  const now = Date.now();
  const iso = (offsetMs: number) =>
    new Date(now - offsetMs).toISOString().replace(/\.\d+Z$/, "Z");
  const items: BankrollItem[] = [
    {
      id: 1,
      occurred_at: iso(6 * 60 * 60 * 1000),
      exchange: "kalshi",
      reason: "sync",
      delta_cents: 0,
      balance_cents: 100_000,
    },
    {
      id: 2,
      occurred_at: iso(4 * 60 * 60 * 1000),
      exchange: "kalshi",
      reason: "fill",
      delta_cents: 5_000,
      balance_cents: 105_000,
    },
    {
      id: 3,
      occurred_at: iso(2 * 60 * 60 * 1000),
      exchange: "kalshi",
      reason: "fill",
      delta_cents: -2_000,
      balance_cents: 103_000,
    },
    {
      id: 4,
      occurred_at: iso(1 * 60 * 60 * 1000),
      exchange: "kalshi",
      reason: "deposit",
      delta_cents: 25_000,
      balance_cents: 128_000,
    },
  ];
  // The real endpoint returns chronological order with the newest row
  // also surfaced via `current_balance_cents` / `last_event_at`.
  const newest = items[items.length - 1];
  return {
    exchange: "kalshi",
    current_balance_cents: newest.balance_cents,
    last_event_at: newest.occurred_at,
    items,
    sync,
  };
}

async function mockBankrollFeed(page: Page, feed: BankrollFeed) {
  await page.route("**/api/dashboard/bankroll/recent*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(feed),
    });
  });
}

async function ensureSignedInOperator(page: Page): Promise<boolean> {
  await page.goto("/app");
  const shell = page.getByTestId("shell-main");
  if ((await shell.count()) === 0) return false;
  return true;
}

test.describe("operator dashboard — bankroll panel", () => {
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

  test("renders current balance, 24h delta, per-reason tiles, and a FRESH sync badge", async ({
    page,
  }) => {
    const nowS = Math.floor(Date.now() / 1000);
    const feed = buildFeed({
      running: true,
      last_tick_at: nowS - 5, // ageS=5s, interval=60s ⇒ ageS <= 3*interval ⇒ FRESH
      interval_seconds: 60,
      last_error: null,
    });
    await mockBankrollFeed(page, feed);

    if (!(await ensureSignedInOperator(page))) {
      test.skip(true, "user not approved — shell not rendered");
    }

    const card = page.getByTestId("card-bankroll-panel");
    await expect(card).toBeVisible({ timeout: 15_000 });

    // Current balance text matches the most recent row ($1,280.00 from
    // newest balance_cents=128000).
    await expect(page.getByTestId("bankroll-current-balance")).toHaveText(
      "$1,280.00",
    );

    // 24h delta sums all four rows: 0 + 50 + (-20) + 250 = +$280.00.
    // The component renders a leading "+" / "−" (U+2212), not ASCII "-".
    const delta = page.getByTestId("bankroll-24h-delta");
    await expect(delta).toBeVisible();
    await expect(delta).toHaveText(/24h\s+\+\$280\.00/);

    // Sync indicator: FRESH (running + within 3x interval).
    const sync = page.getByTestId("bankroll-sync-indicator");
    await expect(sync).toBeVisible();
    await expect(sync).toHaveAttribute("data-sync-tone", "fresh");
    await expect(sync).toContainText("FRESH");

    // Per-reason breakdown: one tile per distinct reason in the feed.
    const breakdown = page.getByTestId("bankroll-reason-breakdown");
    await expect(breakdown).toBeVisible();
    await expect(page.getByTestId("bankroll-reason-sync")).toBeVisible();
    await expect(page.getByTestId("bankroll-reason-fill")).toBeVisible();
    await expect(page.getByTestId("bankroll-reason-deposit")).toBeVisible();
    // sync had a zero net delta; fill nets +$30; deposit is +$250.
    await expect(page.getByTestId("bankroll-reason-deposit")).toContainText(
      "+$250.00",
    );
    await expect(page.getByTestId("bankroll-reason-fill")).toContainText(
      "+$30.00",
    );
    // No extra tiles beyond the three reasons present in the feed.
    await expect(
      breakdown.locator('[data-testid^="bankroll-reason-"]'),
    ).toHaveCount(3);

    // Sparkline mounts (recharts container is rendered, not the empty
    // placeholder).
    const chart = page.getByTestId("chart-bankroll-sparkline");
    await expect(chart).toBeVisible();
    await expect(chart).not.toContainText("NO_RECENT_EVENTS");
  });

  test("flags the sync indicator STALE when last tick is older than 3× the interval", async ({
    page,
  }) => {
    const nowS = Math.floor(Date.now() / 1000);
    const feed = buildFeed({
      running: true,
      // ageS=10min, interval=60s ⇒ ageS > 3*interval ⇒ STALE.
      last_tick_at: nowS - 10 * 60,
      interval_seconds: 60,
      last_error: null,
    });
    await mockBankrollFeed(page, feed);

    if (!(await ensureSignedInOperator(page))) {
      test.skip(true, "user not approved — shell not rendered");
    }

    await expect(page.getByTestId("card-bankroll-panel")).toBeVisible({
      timeout: 15_000,
    });
    const sync = page.getByTestId("bankroll-sync-indicator");
    await expect(sync).toHaveAttribute("data-sync-tone", "stale");
    await expect(sync).toContainText("STALE");
  });

  test("reports STOPPED when the bankroll-sync loop is not running", async ({
    page,
  }) => {
    const nowS = Math.floor(Date.now() / 1000);
    const feed = buildFeed({
      // running:false ⇒ tone falls through to "down" ⇒ STOPPED label,
      // regardless of how recent last_tick_at happens to be.
      running: false,
      last_tick_at: nowS - 1,
      interval_seconds: 60,
      last_error: "manually stopped",
    });
    await mockBankrollFeed(page, feed);

    if (!(await ensureSignedInOperator(page))) {
      test.skip(true, "user not approved — shell not rendered");
    }

    await expect(page.getByTestId("card-bankroll-panel")).toBeVisible({
      timeout: 15_000,
    });
    const sync = page.getByTestId("bankroll-sync-indicator");
    await expect(sync).toHaveAttribute("data-sync-tone", "down");
    await expect(sync).toContainText("STOPPED");
  });
});
