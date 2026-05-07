import { describe, it, expect, afterEach, vi, beforeEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

// `CardTile` calls `useGetMyAccess()` and `useAuth()` internally to decide
// whether to show admin-only "Poll now" controls. The badge/cluster/freshness
// rendering this test exercises does not depend on either, so we stub them
// out so the test stays a pure render check (no QueryClientProvider, no
// ClerkProvider needed).
vi.mock("@clerk/react", () => ({
  useAuth: () => ({ getToken: async () => null, isSignedIn: false }),
}));

vi.mock("@workspace/api-client-react", () => ({
  useGetMyAccess: () => ({ data: undefined, isLoading: false, error: null }),
}));

// `CryptoAccuracyPanel` calls `apiFetch` directly inside a `useEffect` to
// load the rollup. The other tests in this file render pure presentation
// components and never trigger the fetch, so a hoisted top-level mock is
// safe and stays out of their way.
const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  apiFetch: apiFetchMock,
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  formatErrorMessage: (e: unknown, fallback?: string) =>
    fallback ?? (e instanceof Error ? e.message : String(e)),
}));

// We render CardTile directly so the test only depends on the badge
// rendering logic, not the full /system page (auth, fetch, polling).
import {
  CardTile,
  CryptoAccuracyPanel,
  CryptoFreshnessWidget,
  CryptoOutcomeBadge,
  SurrogateBacktestPanel,
} from "./health";

afterEach(() => cleanup());

const baseModelsCard = {
  key: "models",
  label: "Model artifacts",
  category: "passive" as const,
  status: "ok" as const,
  detail: "3 predictor(s) loaded",
  checked_at: "2026-04-24T00:00:00Z",
  latency_ms: 4,
};

describe("ManifestSignatureBadge (rendered via CardTile)", () => {
  it("renders a 'verified' badge when signature is ok", () => {
    render(
      <CardTile
        card={{
          ...baseModelsCard,
          extras: {
            manifest_signature: "ok",
            manifest_reason: null,
            manifest_error: null,
          },
        }}
      />,
    );
    const badge = screen.getByTestId("manifest-signature-badge");
    expect(badge.getAttribute("data-signature")).toBe("ok");
    expect(badge.textContent).toMatch(/verified/i);
    // "ok" badge is informational only — not a link.
    expect(badge.tagName.toLowerCase()).toBe("span");
    expect(badge.className).toMatch(/success/);
  });

  it("renders a muted 'not enforced' link when signature is skipped", () => {
    render(
      <CardTile
        card={{
          ...baseModelsCard,
          extras: {
            manifest_signature: "skipped",
            manifest_reason: null,
            manifest_error: null,
          },
        }}
      />,
    );
    const badge = screen.getByTestId("manifest-signature-badge");
    expect(badge.getAttribute("data-signature")).toBe("skipped");
    expect(badge.textContent).toMatch(/not enforced/i);
    // "Skipped" deserves a link to the docs that explain how to set
    // AXIOM_MANIFEST_PUBLIC_KEY so an operator can self-serve.
    expect(badge.tagName.toLowerCase()).toBe("a");
    const href = badge.getAttribute("href") ?? "";
    expect(href).toMatch(/manifest-signature-enforcement/);
    expect(badge.getAttribute("target")).toBe("_blank");
    // Muted/gray styling — must NOT use the destructive palette.
    expect(badge.className).not.toMatch(/destructive/);
  });

  it("renders a loud 'invalid' badge with the error string when signature is invalid", () => {
    const errMsg =
      "signature invalid: ed25519 verification failed; manifest may have been tampered with";
    render(
      <CardTile
        card={{
          ...baseModelsCard,
          status: "down",
          detail: "manifest mismatch",
          extras: {
            manifest_signature: "invalid",
            manifest_reason: "signature_invalid",
            manifest_error: errMsg,
          },
        }}
      />,
    );
    const badge = screen.getByTestId("manifest-signature-badge");
    expect(badge.getAttribute("data-signature")).toBe("invalid");
    expect(badge.textContent).toMatch(/invalid/i);
    // Operators must see WHY it failed — distinguish tampering from
    // a transient download/decode problem.
    const errEl = screen.getByTestId("manifest-signature-error");
    expect(errEl.textContent).toBe(errMsg);
    // Loud (red/destructive) styling and links to the docs.
    expect(badge.tagName.toLowerCase()).toBe("a");
    expect(badge.className).toMatch(/destructive/);
    const href = badge.getAttribute("href") ?? "";
    expect(href).toMatch(/manifest-signature-enforcement/);
  });

  it("falls back to a generic message when 'invalid' is reported without an error string", () => {
    render(
      <CardTile
        card={{
          ...baseModelsCard,
          status: "down",
          extras: {
            manifest_signature: "invalid",
            manifest_reason: "signature_invalid",
            manifest_error: null,
          },
        }}
      />,
    );
    const errEl = screen.getByTestId("manifest-signature-error");
    expect(errEl.textContent).toMatch(/signature verification failed/i);
  });

  it("renders no signature badge when the loader has not reported yet", () => {
    render(
      <CardTile
        card={{
          ...baseModelsCard,
          status: "degraded",
          detail: "loading…",
          extras: { manifest_signature: null },
        }}
      />,
    );
    expect(screen.queryByTestId("manifest-signature-badge")).toBeNull();
  });

  it("renders no signature badge for non-models cards", () => {
    render(
      <CardTile
        card={{
          key: "settlement",
          label: "Settlement loop",
          category: "passive",
          status: "ok",
          detail: "interval=30s",
          checked_at: null,
          latency_ms: null,
          extras: { manifest_signature: "ok" },
        }}
      />,
    );
    // Even if the extras happen to include a signature key, only the
    // "models" card surfaces the badge — preventing accidental
    // mis-rendering on unrelated cards.
    expect(screen.queryByTestId("manifest-signature-badge")).toBeNull();
  });
});

const baseClusterCard = {
  key: "kalshi_cluster",
  label: "Kalshi cluster auth",
  category: "passive" as const,
  checked_at: "2026-04-24T00:00:00Z",
  latency_ms: null,
};

describe("KalshiClusterDetails (rendered via CardTile)", () => {
  it("renders configured_env and key_belongs_to in green when the probe passed", () => {
    render(
      <CardTile
        card={{
          ...baseClusterCard,
          status: "ok",
          detail: "authenticated against cluster 'prod'",
          extras: {
            checked: true,
            ok: true,
            configured_env: "prod",
            key_belongs_to: "prod",
            detail: "authenticated against cluster 'prod'",
          },
        }}
      />,
    );
    const tile = screen.getByTestId("kalshi-cluster-details").closest("div")!
      .parentElement!;
    expect(tile.className).toMatch(/success/);
    expect(
      screen.getByTestId("kalshi-cluster-configured-env").textContent,
    ).toBe("prod");
    const belongs = screen.getByTestId("kalshi-cluster-key-belongs-to");
    expect(belongs.textContent).toBe("prod");
    // No mismatch — no destructive highlight.
    expect(belongs.className).not.toMatch(/destructive/);
  });

  it("renders red and surfaces the wrong-cluster error when the key belongs elsewhere", () => {
    const errMsg =
      "Kalshi credentials look valid but were rejected by cluster 'prod' — the key authenticates against 'demo'. Set KALSHI_ENV=demo.";
    render(
      <CardTile
        card={{
          ...baseClusterCard,
          status: "down",
          detail: errMsg,
          extras: {
            checked: true,
            ok: false,
            configured_env: "prod",
            key_belongs_to: "demo",
            error: errMsg,
          },
        }}
      />,
    );
    const tile = screen.getByTestId("kalshi-cluster-details").closest("div")!
      .parentElement!;
    // Red border / destructive palette signals the failure.
    expect(tile.className).toMatch(/destructive/);
    expect(
      screen.getByTestId("kalshi-cluster-configured-env").textContent,
    ).toBe("prod");
    const belongs = screen.getByTestId("kalshi-cluster-key-belongs-to");
    expect(belongs.textContent).toBe("demo");
    // The mismatch must be highlighted in destructive color so the
    // operator sees it without reading the error text.
    expect(belongs.className).toMatch(/destructive/);
    // The full human-readable error appears in the card's detail row.
    expect(screen.getByText(errMsg)).toBeTruthy();
  });

  it("renders an unconfigured placeholder when the probe has not run yet", () => {
    render(
      <CardTile
        card={{
          ...baseClusterCard,
          status: "unconfigured",
          detail: "cluster probe has not run yet",
          extras: { checked: false },
        }}
      />,
    );
    expect(
      screen.getByTestId("kalshi-cluster-configured-env").textContent,
    ).toBe("—");
    expect(
      screen.getByTestId("kalshi-cluster-key-belongs-to").textContent,
    ).toBe("—");
    // Pre-probe: no "last checked" line — there's nothing to render.
    expect(screen.queryByTestId("kalshi-cluster-last-checked")).toBeNull();
  });

  it("shows a relative 'last checked' age when the probe has run recently", () => {
    const epoch = Date.now() / 1000 - 120; // 2 minutes ago
    render(
      <CardTile
        card={{
          ...baseClusterCard,
          status: "ok",
          detail: "authenticated against cluster 'prod'",
          extras: {
            checked: true,
            ok: true,
            configured_env: "prod",
            key_belongs_to: "prod",
            checked_at_epoch: epoch,
            checked_at: new Date(epoch * 1000).toISOString(),
            probe_interval_seconds: 600,
          },
        }}
      />,
    );
    const last = screen.getByTestId("kalshi-cluster-last-checked");
    // 2-minute-old probe rendered as "2m ago" (formatAge rounds down).
    expect(last.textContent).toMatch(/2m ago/);
    // Fresh result must not be flagged as stale.
    expect(last.getAttribute("data-stale")).toBe("false");
    expect(last.className).not.toMatch(/warning/);
    expect(screen.queryByTestId("kalshi-cluster-stale-warning")).toBeNull();
  });

  it("flags 'last checked' as stale when older than 3x the probe interval", () => {
    // 40 minutes old vs 10-minute interval ⇒ > 3x cadence ⇒ stale.
    const epoch = Date.now() / 1000 - 40 * 60;
    render(
      <CardTile
        card={{
          ...baseClusterCard,
          status: "ok",
          detail: "authenticated against cluster 'prod'",
          extras: {
            checked: true,
            ok: true,
            configured_env: "prod",
            key_belongs_to: "prod",
            checked_at_epoch: epoch,
            checked_at: new Date(epoch * 1000).toISOString(),
            probe_interval_seconds: 600,
          },
        }}
      />,
    );
    const last = screen.getByTestId("kalshi-cluster-last-checked");
    expect(last.getAttribute("data-stale")).toBe("true");
    // Warning palette so a green status doesn't hide a wedged probe.
    expect(last.className).toMatch(/warning/);
    const warn = screen.getByTestId("kalshi-cluster-stale-warning");
    expect(warn.textContent).toMatch(/stale/i);
  });

  it("uses the API-provided probe interval to decide what counts as stale", () => {
    // 5-minute-old probe with a 60-second cadence ⇒ also stale (>3x).
    const epoch = Date.now() / 1000 - 5 * 60;
    render(
      <CardTile
        card={{
          ...baseClusterCard,
          status: "ok",
          detail: "authenticated against cluster 'prod'",
          extras: {
            checked: true,
            ok: true,
            configured_env: "prod",
            key_belongs_to: "prod",
            checked_at_epoch: epoch,
            checked_at: new Date(epoch * 1000).toISOString(),
            probe_interval_seconds: 60,
          },
        }}
      />,
    );
    const last = screen.getByTestId("kalshi-cluster-last-checked");
    expect(last.getAttribute("data-stale")).toBe("true");
  });

  it("falls back to parsing checked_at when the API omits the epoch field", () => {
    // Older API builds may only send the ISO-8601 string. Make sure we
    // still surface a relative age and don't render "unknown".
    const isoTwoMinAgo = new Date(Date.now() - 120_000).toISOString();
    render(
      <CardTile
        card={{
          ...baseClusterCard,
          status: "ok",
          detail: "authenticated against cluster 'prod'",
          extras: {
            checked: true,
            ok: true,
            configured_env: "prod",
            key_belongs_to: "prod",
            checked_at: isoTwoMinAgo,
          },
        }}
      />,
    );
    expect(
      screen.getByTestId("kalshi-cluster-last-checked").textContent,
    ).toMatch(/m ago/);
  });

  it("does not render the kalshi-cluster details on unrelated cards", () => {
    render(
      <CardTile
        card={{
          key: "kalshi_ws",
          label: "Kalshi WebSocket",
          category: "passive",
          status: "ok",
          detail: "connected",
          checked_at: null,
          latency_ms: null,
          extras: { configured_env: "prod", key_belongs_to: "prod" },
        }}
      />,
    );
    expect(screen.queryByTestId("kalshi-cluster-details")).toBeNull();
  });
});

const baseFreshnessCard = {
  key: "crypto_feed_freshness",
  label: "Crypto feed freshness",
  category: "passive" as const,
  checked_at: "2026-04-24T00:00:00Z",
  latency_ms: null,
};

describe("CryptoFreshnessWidget", () => {
  it("renders one row per (symbol, granularity) pair sorted worst-first", () => {
    render(
      <CryptoFreshnessWidget
        card={{
          ...baseFreshnessCard,
          status: "down",
          detail: "1/3 fresh",
          extras: {
            fresh_count: 1,
            total_count: 3,
            pairs: [
              {
                symbol: "BTC",
                granularity: "1m",
                expected_seconds: 60,
                max_ts: "2026-04-24T00:00:00Z",
                age_seconds: 30,
                yellow_threshold_seconds: 180,
                red_threshold_seconds: 600,
                status: "ok",
              },
              {
                symbol: "ETH",
                granularity: "1m",
                expected_seconds: 60,
                max_ts: "2026-04-24T00:00:00Z",
                age_seconds: 250,
                yellow_threshold_seconds: 180,
                red_threshold_seconds: 600,
                status: "degraded",
              },
              {
                symbol: "BTC",
                granularity: "5m",
                expected_seconds: 300,
                max_ts: null,
                age_seconds: null,
                yellow_threshold_seconds: 900,
                red_threshold_seconds: 3000,
                status: "down",
              },
            ],
          },
        }}
      />,
    );

    const widget = screen.getByTestId("crypto-freshness-widget");
    expect(widget).toBeTruthy();
    // Header summarises the fresh count so the operator gets context
    // without scanning the whole table.
    expect(widget.textContent).toMatch(/1\/3 fresh/);

    // Worst pair (status=down) must come first, then degraded, then ok.
    const rows = widget.querySelectorAll("tbody tr");
    expect(rows.length).toBe(3);
    expect(rows[0].getAttribute("data-status")).toBe("down");
    expect(rows[1].getAttribute("data-status")).toBe("degraded");
    expect(rows[2].getAttribute("data-status")).toBe("ok");

    // The "no bars" pair shows a clear sentinel rather than "0s old".
    expect(
      screen.getByTestId("crypto-pair-age-BTC-5m").textContent,
    ).toMatch(/no bars/);
    // Fresh BTC/1m bar shows the rounded age.
    expect(
      screen.getByTestId("crypto-pair-age-BTC-1m").textContent,
    ).toMatch(/30s/);
  });

  it("sorts oldest-first within the same status bucket", () => {
    render(
      <CryptoFreshnessWidget
        card={{
          ...baseFreshnessCard,
          status: "degraded",
          detail: "2/2 degraded",
          extras: {
            fresh_count: 0,
            total_count: 2,
            pairs: [
              {
                symbol: "BTC",
                granularity: "1m",
                expected_seconds: 60,
                max_ts: "2026-04-24T00:00:00Z",
                age_seconds: 200,
                yellow_threshold_seconds: 180,
                red_threshold_seconds: 600,
                status: "degraded",
              },
              {
                symbol: "ETH",
                granularity: "1m",
                expected_seconds: 60,
                max_ts: "2026-04-24T00:00:00Z",
                age_seconds: 500,
                yellow_threshold_seconds: 180,
                red_threshold_seconds: 600,
                status: "degraded",
              },
            ],
          },
        }}
      />,
    );

    const rows = screen
      .getByTestId("crypto-freshness-widget")
      .querySelectorAll("tbody tr");
    // Oldest (ETH 500s) bubbles above BTC (200s) so the operator
    // sees the most-stale pair first.
    expect(rows[0].getAttribute("data-testid")).toBe("crypto-pair-ETH-1m");
    expect(rows[1].getAttribute("data-testid")).toBe("crypto-pair-BTC-1m");
  });

  it("colours the age cell to match the pair status", () => {
    render(
      <CryptoFreshnessWidget
        card={{
          ...baseFreshnessCard,
          status: "down",
          detail: "0/1 fresh",
          extras: {
            fresh_count: 0,
            total_count: 1,
            pairs: [
              {
                symbol: "BTC",
                granularity: "1m",
                expected_seconds: 60,
                max_ts: "2026-04-23T23:00:00Z",
                age_seconds: 3600,
                yellow_threshold_seconds: 180,
                red_threshold_seconds: 600,
                status: "down",
              },
            ],
          },
        }}
      />,
    );
    const ageCell = screen.getByTestId("crypto-pair-age-BTC-1m");
    expect(ageCell.className).toMatch(/destructive/);
  });

  it("renders nothing when the extras have no pairs (e.g. unconfigured)", () => {
    const { container } = render(
      <CryptoFreshnessWidget
        card={{
          ...baseFreshnessCard,
          status: "unconfigured",
          detail: "COINAPI_API_KEY / COINGECKO_API_KEY not set",
          extras: {},
        }}
      />,
    );
    // No pairs ⇒ no widget rendered. The generic card list still
    // shows the card itself with the unconfigured detail.
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("crypto-freshness-widget")).toBeNull();
  });
});

const baseSoccerFeedCard = {
  key: "feed:sportradar_epl_injuries",
  label: "sportradar_epl_injuries",
  category: "feed" as const,
  checked_at: "2026-04-26T00:00:00Z",
  latency_ms: null,
};

describe("Soccer season metadata (rendered via CardTile)", () => {
  it("shows the cached season id + resolved-at without any badges on a clean poll", () => {
    render(
      <CardTile
        card={{
          ...baseSoccerFeedCard,
          status: "ok",
          detail: "wrote 0 row(s)",
          extras: {
            cadence_seconds: 900,
            metadata: {
              season_id: "sr:season:CLEAN",
              season_id_resolved_at: "2026-04-25T12:00:00+00:00",
              tournament_id: "sr:tournament:17",
              season_stuck: false,
              season_last_refresh_at: null,
              season_last_refresh_outcome: null,
            },
          },
        }}
      />,
    );
    const wrap = screen.getByTestId("feed-season-feed:sportradar_epl_injuries");
    expect(wrap).toBeTruthy();
    expect(
      screen.getByTestId("feed-season-id-feed:sportradar_epl_injuries")
        .textContent,
    ).toBe("sr:season:CLEAN");
    // Neither badge fires when the season is happily pinned.
    expect(
      screen.queryByTestId(
        "feed-season-rolled-over-feed:sportradar_epl_injuries",
      ),
    ).toBeNull();
    expect(
      screen.queryByTestId(
        "feed-season-stuck-feed:sportradar_epl_injuries",
      ),
    ).toBeNull();
  });

  it("shows the 'season rolled over' badge for one polling interval after a fresh rotation", () => {
    // A rotation that just happened — the badge must be visible so an
    // operator notices the season-id change on at least one refresh.
    const justNow = new Date(Date.now() - 5_000).toISOString();
    render(
      <CardTile
        card={{
          ...baseSoccerFeedCard,
          status: "ok",
          detail: "wrote 12 row(s)",
          extras: {
            cadence_seconds: 900,
            metadata: {
              season_id: "sr:season:NEW",
              season_previous_id: "sr:season:OLD",
              season_id_resolved_at: justNow,
              season_stuck: false,
              season_last_refresh_at: justNow,
              season_last_refresh_outcome: "rotated",
            },
          },
        }}
      />,
    );
    const badge = screen.getByTestId(
      "feed-season-rolled-over-feed:sportradar_epl_injuries",
    );
    expect(badge.textContent).toMatch(/rolled over/i);
    // Tooltip names the previous id so an operator can see the diff.
    expect(badge.getAttribute("title") ?? "").toMatch(/sr:season:OLD/);
    expect(
      screen.queryByTestId(
        "feed-season-stuck-feed:sportradar_epl_injuries",
      ),
    ).toBeNull();
  });

  it("hides the 'rolled over' badge once the rotation is older than the cadence window", () => {
    // Rotation happened > 1 polling interval ago — the transient badge
    // must drop off so it doesn't permanently clutter the card.
    const oneHourAgo = new Date(Date.now() - 3_600_000).toISOString();
    render(
      <CardTile
        card={{
          ...baseSoccerFeedCard,
          status: "ok",
          detail: "wrote 12 row(s)",
          extras: {
            cadence_seconds: 900, // 15min cadence < 1h ago ⇒ badge expired
            metadata: {
              season_id: "sr:season:NEW",
              season_previous_id: "sr:season:OLD",
              season_id_resolved_at: oneHourAgo,
              season_stuck: false,
              season_last_refresh_at: oneHourAgo,
              season_last_refresh_outcome: "rotated",
            },
          },
        }}
      />,
    );
    expect(
      screen.queryByTestId(
        "feed-season-rolled-over-feed:sportradar_epl_injuries",
      ),
    ).toBeNull();
    // Cached id still shown.
    expect(
      screen.getByTestId("feed-season-id-feed:sportradar_epl_injuries")
        .textContent,
    ).toBe("sr:season:NEW");
  });

  it("shows the 'season id stuck' badge when the refresh failed to resolve a new id", () => {
    render(
      <CardTile
        card={{
          ...baseSoccerFeedCard,
          status: "degraded",
          detail:
            "season id stuck (outcome=same_id) :: cached=sr:season:STUCK",
          extras: {
            cadence_seconds: 900,
            metadata: {
              season_id: "sr:season:STUCK",
              season_id_resolved_at: "2026-04-20T00:00:00+00:00",
              season_stuck: true,
              season_last_refresh_at: "2026-04-26T00:00:00+00:00",
              season_last_refresh_outcome: "same_id",
            },
          },
        }}
      />,
    );
    const badge = screen.getByTestId(
      "feed-season-stuck-feed:sportradar_epl_injuries",
    );
    expect(badge.textContent).toMatch(/stuck/i);
    // The stuck badge must NOT also show the (transient) "rolled over"
    // badge — those two states are mutually exclusive and showing both
    // would be misleading.
    expect(
      screen.queryByTestId(
        "feed-season-rolled-over-feed:sportradar_epl_injuries",
      ),
    ).toBeNull();
  });

  it("renders the season-history details block (newest first) when the metadata carries a ring buffer", () => {
    // Simulate two prior rotation/stuck attempts so the operator can
    // scan back through them on the health card.  The component must
    // render them newest-first so the most recent event is at the top
    // (instead of the operator having to scroll past months of older
    // entries).
    render(
      <CardTile
        card={{
          ...baseSoccerFeedCard,
          status: "ok",
          detail: "wrote 12 row(s)",
          extras: {
            cadence_seconds: 900,
            metadata: {
              season_id: "sr:season:NEW",
              season_previous_id: "sr:season:OLD",
              season_id_resolved_at: "2026-04-26T00:00:00+00:00",
              season_stuck: false,
              season_last_refresh_at: "2026-04-26T00:00:00+00:00",
              season_last_refresh_outcome: "rotated",
              season_history: [
                {
                  at: "2026-04-20T00:00:00+00:00",
                  from_id: "sr:season:STALE",
                  to_id: "sr:season:STALE",
                  outcome: "same_id",
                },
                {
                  at: "2026-04-26T00:00:00+00:00",
                  from_id: "sr:season:OLD",
                  to_id: "sr:season:NEW",
                  outcome: "rotated",
                },
              ],
            },
          },
        }}
      />,
    );
    const wrap = screen.getByTestId(
      "feed-season-history-feed:sportradar_epl_injuries",
    );
    expect(wrap).toBeTruthy();
    // Toggle reflects the entry count so an operator can tell at a
    // glance whether the buffer holds anything useful.
    expect(
      screen.getByTestId(
        "feed-season-history-toggle-feed:sportradar_epl_injuries",
      ).textContent,
    ).toMatch(/history \(2\)/i);
    const entries = screen.getAllByTestId(
      /^feed-season-history-entry-feed:sportradar_epl_injuries-\d+$/,
    );
    expect(entries).toHaveLength(2);
    // Newest first ⇒ the rotated entry is at index 0, the older
    // same_id entry at index 1.
    expect(entries[0].getAttribute("data-outcome")).toBe("rotated");
    expect(entries[0].textContent).toMatch(/sr:season:OLD/);
    expect(entries[0].textContent).toMatch(/sr:season:NEW/);
    expect(entries[1].getAttribute("data-outcome")).toBe("same_id");
  });

  it("does not render the history block when the metadata has no entries", () => {
    render(
      <CardTile
        card={{
          ...baseSoccerFeedCard,
          status: "ok",
          detail: "wrote 0 row(s)",
          extras: {
            cadence_seconds: 900,
            metadata: {
              season_id: "sr:season:CLEAN",
              season_id_resolved_at: "2026-04-25T12:00:00+00:00",
              season_stuck: false,
              season_history: [],
            },
          },
        }}
      />,
    );
    expect(
      screen.queryByTestId(
        "feed-season-history-feed:sportradar_epl_injuries",
      ),
    ).toBeNull();
  });

  it("ignores malformed history entries (missing outcome) without crashing", () => {
    // Defensive narrowing: an upgrade hiccup that drops the `outcome`
    // field on one entry must not blow up the whole card render.
    render(
      <CardTile
        card={{
          ...baseSoccerFeedCard,
          status: "ok",
          detail: "wrote 0 row(s)",
          extras: {
            cadence_seconds: 900,
            metadata: {
              season_id: "sr:season:CLEAN",
              season_id_resolved_at: "2026-04-25T12:00:00+00:00",
              season_stuck: false,
              season_history: [
                { at: "2026-04-26T00:00:00+00:00" }, // dropped (no outcome)
                {
                  at: "2026-04-26T01:00:00+00:00",
                  from_id: "sr:season:A",
                  to_id: "sr:season:B",
                  outcome: "rotated",
                },
              ],
            },
          },
        }}
      />,
    );
    const entries = screen.getAllByTestId(
      /^feed-season-history-entry-feed:sportradar_epl_injuries-\d+$/,
    );
    expect(entries).toHaveLength(1);
    expect(entries[0].getAttribute("data-outcome")).toBe("rotated");
  });

  it("renders no season block on non-feed cards even if extras carry a metadata field", () => {
    render(
      <CardTile
        card={{
          ...baseModelsCard,
          extras: {
            metadata: { season_id: "sr:season:LEAK", season_stuck: true },
          },
        }}
      />,
    );
    // Only feed cards render the season metadata block — leaking the
    // section onto unrelated cards (e.g. models) would confuse
    // operators.
    expect(
      screen.queryByTestId("feed-season-models"),
    ).toBeNull();
  });
});

describe("CryptoOutcomeBadge", () => {
  it("renders a green YES badge for a settled YES market", () => {
    render(<CryptoOutcomeBadge outcome="yes" />);
    const badge = screen.getByTestId("crypto-outcome-badge");
    expect(badge.getAttribute("data-outcome")).toBe("yes");
    expect(badge.textContent).toMatch(/yes/i);
    expect(badge.className).toMatch(/success/);
    expect(badge.className).not.toMatch(/destructive/);
  });

  it("renders a red NO badge for a settled NO market", () => {
    render(<CryptoOutcomeBadge outcome="NO" />);
    const badge = screen.getByTestId("crypto-outcome-badge");
    expect(badge.getAttribute("data-outcome")).toBe("no");
    expect(badge.textContent).toMatch(/no/i);
    expect(badge.className).toMatch(/destructive/);
  });

  it("renders a muted VOID badge for a voided market (refunded — no winning side)", () => {
    render(<CryptoOutcomeBadge outcome="void" />);
    const badge = screen.getByTestId("crypto-outcome-badge");
    expect(badge.getAttribute("data-outcome")).toBe("void");
    expect(badge.textContent).toMatch(/void/i);
    // VOID must NOT use the win/loss palette so it doesn't masquerade
    // as either side.
    expect(badge.className).not.toMatch(/success/);
    expect(badge.className).not.toMatch(/destructive/);
  });

  it("renders 'pending' when the row hasn't been settled yet", () => {
    render(<CryptoOutcomeBadge outcome={null} />);
    expect(screen.queryByTestId("crypto-outcome-badge")).toBeNull();
    const pending = screen.getByTestId("crypto-outcome-pending");
    expect(pending.textContent).toMatch(/pending/i);
  });
});

const baseLeaderboardCard = {
  key: "feed:sportradar_tournament_leaderboards",
  label: "Feed :: sportradar_tournament_leaderboards",
  category: "passive" as const,
  checked_at: "2026-04-26T00:00:00Z",
  latency_ms: null,
};

describe("Tournament leaderboard refresh (rendered via CardTile)", () => {
  it("shows the live-event count and most recent refresh entrant total", () => {
    render(
      <CardTile
        card={{
          ...baseLeaderboardCard,
          status: "ok",
          detail: "wrote 142 row(s)",
          extras: {
            cadence_seconds: 120,
            last_ok_at: "2026-04-26T00:00:00+00:00",
            metadata: {
              events_live: 2,
              last_refresh_count: 142,
              last_refresh_at: "2026-04-26T00:00:00+00:00",
              zero_rows_since: null,
            },
          },
        }}
      />,
    );
    expect(
      screen.getByTestId(
        "feed-leaderboard-events-feed:sportradar_tournament_leaderboards",
      ).textContent,
    ).toBe("2");
    expect(
      screen.getByTestId(
        "feed-leaderboard-refresh-count-feed:sportradar_tournament_leaderboards",
      ).textContent,
    ).toMatch(/142 entrants/);
    expect(
      screen.getByTestId(
        "feed-leaderboard-last-success-feed:sportradar_tournament_leaderboards",
      ).textContent,
    ).toContain("2026-04-26T00:00:00+00:00");
    // No streak ⇒ no stale badge.
    expect(
      screen.queryByTestId(
        "feed-leaderboard-stale-feed:sportradar_tournament_leaderboards",
      ),
    ).toBeNull();
  });

  it("does NOT fire the stale badge when the zero-row streak is shorter than 10 minutes", () => {
    const fiveMinutesAgo = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    render(
      <CardTile
        card={{
          ...baseLeaderboardCard,
          status: "ok",
          detail: "wrote 0 row(s)",
          extras: {
            cadence_seconds: 120,
            last_ok_at: "2026-04-26T00:00:00+00:00",
            metadata: {
              events_live: 1,
              last_refresh_count: 0,
              last_refresh_at: new Date().toISOString(),
              zero_rows_since: fiveMinutesAgo,
            },
          },
        }}
      />,
    );
    expect(
      screen.queryByTestId(
        "feed-leaderboard-stale-feed:sportradar_tournament_leaderboards",
      ),
    ).toBeNull();
  });

  it("fires the red stale-leaderboard badge after >10 minutes of zero-row refreshes while events are live", () => {
    const fifteenMinutesAgo = new Date(Date.now() - 15 * 60 * 1000).toISOString();
    render(
      <CardTile
        card={{
          ...baseLeaderboardCard,
          status: "ok",
          detail: "wrote 0 row(s)",
          extras: {
            cadence_seconds: 120,
            last_ok_at: "2026-04-26T00:00:00+00:00",
            metadata: {
              events_live: 1,
              last_refresh_count: 0,
              last_refresh_at: new Date().toISOString(),
              zero_rows_since: fifteenMinutesAgo,
            },
          },
        }}
      />,
    );
    const badge = screen.getByTestId(
      "feed-leaderboard-stale-feed:sportradar_tournament_leaderboards",
    );
    expect(badge.textContent).toMatch(/stale leaderboard/i);
    // Tooltip surfaces when the streak began so an operator can see
    // exactly how long it's been silent.
    expect(badge.getAttribute("title") ?? "").toContain(fifteenMinutesAgo);
  });

  it("does NOT fire the stale badge when no events are currently live, even on a long zero-row streak", () => {
    const oneHourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    render(
      <CardTile
        card={{
          ...baseLeaderboardCard,
          status: "ok",
          detail: "wrote 0 row(s)",
          extras: {
            cadence_seconds: 120,
            last_ok_at: "2026-04-26T00:00:00+00:00",
            metadata: {
              events_live: 0,
              last_refresh_count: 0,
              last_refresh_at: new Date().toISOString(),
              // Backend would normally clear this when no events are
              // live, but defensive: even if a stale value lingers,
              // the badge stays off because there's no live event to
              // be stale about.
              zero_rows_since: oneHourAgo,
            },
          },
        }}
      />,
    );
    expect(
      screen.queryByTestId(
        "feed-leaderboard-stale-feed:sportradar_tournament_leaderboards",
      ),
    ).toBeNull();
  });

  it("lists each refreshed event with sport, id, and entrant count", () => {
    render(
      <CardTile
        card={{
          ...baseLeaderboardCard,
          status: "ok",
          detail: "wrote 142 row(s)",
          extras: {
            cadence_seconds: 120,
            last_ok_at: "2026-04-26T00:00:00+00:00",
            metadata: {
              events_live: 2,
              last_refresh_count: 142,
              last_refresh_at: "2026-04-26T00:00:00+00:00",
              zero_rows_since: null,
              per_event: [
                { sport: "PGA", event_id: "tid-123", entrants: 130 },
                { sport: "F1", event_id: "eid-456", entrants: 12 },
              ],
            },
          },
        }}
      />,
    );
    const list = screen.getByTestId(
      "feed-leaderboard-per-event-feed:sportradar_tournament_leaderboards",
    );
    expect(list.textContent).toMatch(/PGA/);
    expect(list.textContent).toMatch(/tid-123/);
    expect(list.textContent).toMatch(/130 entrants/);
    expect(list.textContent).toMatch(/F1/);
    expect(list.textContent).toMatch(/eid-456/);
    expect(list.textContent).toMatch(/12 entrants/);
    // Healthy rows should NOT carry the destructive palette.
    const pgaRow = screen.getByTestId(
      "feed-leaderboard-event-feed:sportradar_tournament_leaderboards-PGA-tid-123",
    );
    expect(pgaRow.getAttribute("data-zero")).toBe("false");
    expect(pgaRow.className).not.toMatch(/destructive/);
  });

  it("visually distinguishes events that refreshed 0 entrants this cycle", () => {
    render(
      <CardTile
        card={{
          ...baseLeaderboardCard,
          status: "ok",
          detail: "wrote 130 row(s)",
          extras: {
            cadence_seconds: 120,
            last_ok_at: "2026-04-26T00:00:00+00:00",
            metadata: {
              events_live: 2,
              last_refresh_count: 130,
              last_refresh_at: "2026-04-26T00:00:00+00:00",
              zero_rows_since: null,
              per_event: [
                { sport: "PGA", event_id: "tid-123", entrants: 130 },
                {
                  sport: "F1",
                  event_id: "eid-456",
                  entrants: 0,
                  error: "HTTP 500 from sportradar",
                },
              ],
            },
          },
        }}
      />,
    );
    const healthy = screen.getByTestId(
      "feed-leaderboard-event-feed:sportradar_tournament_leaderboards-PGA-tid-123",
    );
    const zeroed = screen.getByTestId(
      "feed-leaderboard-event-feed:sportradar_tournament_leaderboards-F1-eid-456",
    );
    expect(healthy.getAttribute("data-zero")).toBe("false");
    expect(zeroed.getAttribute("data-zero")).toBe("true");
    // The zero-entrant row should pick up the destructive palette so
    // it stands out from the healthy ones; the healthy row must not.
    expect(zeroed.className).toMatch(/destructive/);
    expect(healthy.className).not.toMatch(/destructive/);
    // Per-event errors surface via tooltip so the operator can see
    // why a particular tournament refreshed 0 entrants.
    expect(zeroed.getAttribute("title")).toMatch(/HTTP 500/);
  });

  it("sorts per-event rows so zero-entrant rows pin to the top, then the rest by entrant count descending", () => {
    // The refresher walks tournaments in whatever order it stashed
    // them (PGA bucket first, then F1). On a busy cycle operators
    // need the zero-entrant rows pinned to the top so they're
    // impossible to miss, then the heavy hitters float up so the
    // card scans at a glance. Ties on entrant count break by
    // sport + event id so equal-count rows stay put across renders.
    render(
      <CardTile
        card={{
          ...baseLeaderboardCard,
          status: "ok",
          detail: "wrote 220 row(s)",
          extras: {
            cadence_seconds: 120,
            last_ok_at: "2026-04-26T00:00:00+00:00",
            metadata: {
              events_live: 5,
              last_refresh_count: 220,
              last_refresh_at: "2026-04-26T00:00:00+00:00",
              zero_rows_since: null,
              per_event: [
                { sport: "PGA", event_id: "tid-mid", entrants: 50 },
                { sport: "PGA", event_id: "tid-big", entrants: 130 },
                { sport: "F1", event_id: "eid-zero-b", entrants: 0 },
                { sport: "F1", event_id: "eid-tie", entrants: 50 },
                { sport: "F1", event_id: "eid-zero-a", entrants: 0 },
              ],
            },
          },
        }}
      />,
    );
    const list = screen.getByTestId(
      "feed-leaderboard-per-event-feed:sportradar_tournament_leaderboards",
    );
    const rowKeys = Array.from(list.querySelectorAll("li")).map((li) =>
      li.getAttribute("data-testid"),
    );
    expect(rowKeys).toEqual([
      // Zero-entrant rows first, ordered by sport + event id so the
      // list is stable across renders.
      "feed-leaderboard-event-feed:sportradar_tournament_leaderboards-F1-eid-zero-a",
      "feed-leaderboard-event-feed:sportradar_tournament_leaderboards-F1-eid-zero-b",
      // Then the biggest refresh.
      "feed-leaderboard-event-feed:sportradar_tournament_leaderboards-PGA-tid-big",
      // Then the two 50-entrant rows; F1 sorts before PGA on the
      // tiebreaker so the order is deterministic regardless of how
      // the refresher walked them.
      "feed-leaderboard-event-feed:sportradar_tournament_leaderboards-F1-eid-tie",
      "feed-leaderboard-event-feed:sportradar_tournament_leaderboards-PGA-tid-mid",
    ]);
  });

  it("omits the per-event list when the refresher hasn't reported any events yet", () => {
    render(
      <CardTile
        card={{
          ...baseLeaderboardCard,
          status: "ok",
          detail: "wrote 0 row(s)",
          extras: {
            cadence_seconds: 120,
            last_ok_at: "2026-04-26T00:00:00+00:00",
            metadata: {
              events_live: 0,
              last_refresh_count: 0,
              last_refresh_at: "2026-04-26T00:00:00+00:00",
              zero_rows_since: null,
              per_event: [],
            },
          },
        }}
      />,
    );
    expect(
      screen.queryByTestId(
        "feed-leaderboard-per-event-feed:sportradar_tournament_leaderboards",
      ),
    ).toBeNull();
  });

  it("does not render the leaderboard block on unrelated feed cards", () => {
    // The block must be gated on the leaderboard feed key — otherwise
    // every Sportradar feed would sprout an empty 'events live: 0'
    // line and confuse operators.
    render(
      <CardTile
        card={{
          key: "feed:sportradar_epl_injuries",
          label: "Feed :: sportradar_epl_injuries",
          category: "passive" as const,
          checked_at: "2026-04-26T00:00:00Z",
          latency_ms: null,
          status: "ok",
          detail: "wrote 0 row(s)",
          extras: {
            metadata: { events_live: 99, last_refresh_count: 99 },
          },
        }}
      />,
    );
    expect(
      screen.queryByTestId(
        "feed-leaderboard-feed:sportradar_epl_injuries",
      ),
    ).toBeNull();
  });
});


describe("CryptoAccuracyPanel", () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  // Stub `setInterval` so the polling timer doesn't keep the test
  // event loop alive after the assertions land. The hoisted
  // `vi.useFakeTimers()` would also work but interferes with
  // testing-library's microtask flushing.
  it("renders both rolling-window cards with brier and hit rate", async () => {
    apiFetchMock.mockResolvedValueOnce({
      league: "CRYPTO",
      windows: [
        {
          window_hours: 24,
          n_settled: 12,
          n_scored: 10,
          n_void: 2,
          brier: 0.1875,
          hit_rate: 0.6,
        },
        {
          window_hours: 24 * 7,
          n_settled: 80,
          n_scored: 75,
          n_void: 5,
          brier: 0.2234,
          hit_rate: 0.5466,
        },
      ],
    });

    const token = vi.fn(async () => null);
    render(<CryptoAccuracyPanel token={token} />);

    await waitFor(() => {
      expect(screen.getByTestId("crypto-accuracy-window-24h")).toBeTruthy();
    });

    // 24h card.
    expect(
      screen.getByTestId("crypto-accuracy-window-24h-brier").textContent,
    ).toBe("0.1875");
    expect(
      screen.getByTestId("crypto-accuracy-window-24h-hit-rate").textContent,
    ).toBe("60.0%");
    expect(
      screen.getByTestId("crypto-accuracy-window-24h-counts").textContent,
    ).toMatch(/scored 10/);
    expect(
      screen.getByTestId("crypto-accuracy-window-24h-counts").textContent,
    ).toMatch(/void 2/);

    // 7d card label collapses to "7d" rather than "168h".
    const week = screen.getByTestId("crypto-accuracy-window-168h");
    expect(week.textContent).toMatch(/last 7d/);
    expect(
      screen.getByTestId("crypto-accuracy-window-168h-brier").textContent,
    ).toBe("0.2234");
    expect(
      screen.getByTestId("crypto-accuracy-window-168h-hit-rate").textContent,
    ).toBe("54.7%");

    // The endpoint we hit is the operator-console rollup.
    expect(apiFetchMock.mock.calls[0]?.[0]).toBe(
      "/api/dashboard/crypto-accuracy",
    );
  });

  it("renders an em-dash placeholder when a window has no scored rows", async () => {
    apiFetchMock.mockResolvedValueOnce({
      league: "CRYPTO",
      windows: [
        {
          window_hours: 24,
          n_settled: 0,
          n_scored: 0,
          n_void: 0,
          brier: null,
          hit_rate: null,
        },
        {
          // All four 7d-window rows voided → still scoreless. Brier/
          // hit_rate must render as "—" rather than 0.0000 / 0.0%, which
          // would mislead the operator into thinking the model nailed
          // every prediction.
          window_hours: 24 * 7,
          n_settled: 4,
          n_scored: 0,
          n_void: 4,
          brier: null,
          hit_rate: null,
        },
      ],
    });

    render(<CryptoAccuracyPanel token={vi.fn(async () => null)} />);

    await waitFor(() => {
      expect(
        screen.getByTestId("crypto-accuracy-window-24h-brier").textContent,
      ).toBe("—");
    });
    expect(
      screen.getByTestId("crypto-accuracy-window-24h-hit-rate").textContent,
    ).toBe("—");
    expect(
      screen.getByTestId("crypto-accuracy-window-168h-brier").textContent,
    ).toBe("—");
    expect(
      screen.getByTestId("crypto-accuracy-window-168h-hit-rate").textContent,
    ).toBe("—");
  });

  it("surfaces a rollup-fetch error without blanking the panel chrome", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("network down"));

    render(<CryptoAccuracyPanel token={vi.fn(async () => null)} />);

    await waitFor(() => {
      expect(screen.getByText(/network down/)).toBeTruthy();
    });
    // Panel chrome (heading + section) is still present so the
    // operator can see the panel is alive even when the rollup
    // fetch fails — they just see the error message instead of the
    // window cards.
    expect(screen.getByTestId("panel-crypto-accuracy")).toBeTruthy();
    expect(screen.queryByTestId("crypto-accuracy-window-24h")).toBeNull();
  });
});

describe("SurrogateBacktestPanel", () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("renders the per-league delta table with degraded-baseline disclaimer", async () => {
    apiFetchMock.mockResolvedValueOnce({
      available: true,
      reason: null,
      report_path: "/repo/scripts/reports/v1_backtest_sample_surrogate_8k_2026-04-29.json",
      report_filename: "v1_backtest_sample_surrogate_8k_2026-04-29.json",
      report_url: "/api/health/surrogate-backtest/report.json",
      generated_at: "2026-04-29T20:26:24+00:00",
      row_input_count: 8000,
      row_kept_count: 757,
      degraded_baseline: true,
      disclaimer:
        "DEGRADED baseline — surrogate features stand in for real V1 feature dumps until #731 lands.",
      summary: { league_count: 4 },
      leagues: [
        {
          league: "MLB",
          sample_count: 41,
          calibrator_applied_count: 41,
          surrogate_coverage_pct: 100.0,
          calibrator_type: "isotonic",
          delta_vs_v1: -0.1897,
          brier_v1_raw: 0.21,
          brier_calibrated: 0.02,
          verdict: "improved",
        },
        {
          league: "NBA",
          sample_count: 367,
          calibrator_applied_count: 360,
          surrogate_coverage_pct: 98.09,
          calibrator_type: "isotonic",
          delta_vs_v1: -0.0964,
          brier_v1_raw: 0.36,
          brier_calibrated: 0.27,
          verdict: "improved",
        },
        {
          league: "NCAAB",
          sample_count: 31,
          calibrator_applied_count: 31,
          surrogate_coverage_pct: 100.0,
          calibrator_type: "platt",
          delta_vs_v1: 0.05,
          brier_v1_raw: 0.40,
          brier_calibrated: 0.45,
          verdict: "regressed",
        },
      ],
    });

    render(<SurrogateBacktestPanel token={vi.fn(async () => null)} />);

    await waitFor(() => {
      expect(screen.getByTestId("surrogate-backtest-row-MLB")).toBeTruthy();
    });

    // Disclaimer banner is always present so an operator scanning the
    // page can't mistake the table for a green V1-parity sign-off.
    const disclaimer = screen.getByTestId("surrogate-backtest-disclaimer");
    expect(disclaimer.textContent).toMatch(/DEGRADED/);
    expect(disclaimer.textContent).toMatch(/#731/);

    // Delta cells render with explicit signs so improvement vs.
    // regression is unambiguous at a glance.
    expect(
      screen.getByTestId("surrogate-backtest-delta-MLB").textContent,
    ).toMatch(/−0\.1897/);
    expect(
      screen.getByTestId("surrogate-backtest-delta-NCAAB").textContent,
    ).toMatch(/\+0\.0500/);

    // Sample counts and surrogate coverage% are surfaced verbatim.
    expect(
      screen.getByTestId("surrogate-backtest-samples-NBA").textContent,
    ).toBe("367");
    expect(
      screen.getByTestId("surrogate-backtest-coverage-NBA").textContent,
    ).toBe("98.1%");

    // Freshness + report-path link give operators a single click to
    // pull the underlying JSON for triage.
    const link = screen.getByTestId("surrogate-backtest-report-link");
    expect(link.tagName.toLowerCase()).toBe("a");
    expect(link.getAttribute("href")).toBe(
      "/api/health/surrogate-backtest/report.json",
    );
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.textContent).toBe(
      "v1_backtest_sample_surrogate_8k_2026-04-29.json",
    );
    // The on-disk path stays visible alongside the link so operators
    // can still copy it for in-shell triage.
    expect(
      screen.getByTestId("surrogate-backtest-report-path-hint").textContent,
    ).toMatch(
      /\/repo\/scripts\/reports\/v1_backtest_sample_surrogate_8k_2026-04-29\.json/,
    );

    // The endpoint we hit is the dedicated surrogate-backtest one.
    expect(apiFetchMock.mock.calls[0]?.[0]).toBe(
      "/api/health/surrogate-backtest",
    );
  });

  it("renders the disclaimer and a placeholder when no report is on disk", async () => {
    apiFetchMock.mockResolvedValueOnce({
      available: false,
      reason: "no surrogate backtest report on disk",
      report_path: null,
      report_filename: null,
      generated_at: null,
      row_input_count: null,
      row_kept_count: null,
      degraded_baseline: true,
      disclaimer:
        "DEGRADED baseline — surrogate features stand in for real V1 feature dumps until #731 lands.",
      summary: null,
      leagues: [],
    });

    render(<SurrogateBacktestPanel token={vi.fn(async () => null)} />);

    await waitFor(() => {
      expect(screen.getByTestId("surrogate-backtest-empty")).toBeTruthy();
    });
    // Disclaimer must still render even with no report — operators
    // need the warning context regardless of whether numbers are
    // available.
    expect(screen.getByTestId("surrogate-backtest-disclaimer")).toBeTruthy();
    expect(
      screen.getByTestId("surrogate-backtest-empty").textContent,
    ).toMatch(/no surrogate backtest report/);
  });

  it("surfaces a fetch error without blanking the panel chrome", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("network down"));

    render(<SurrogateBacktestPanel token={vi.fn(async () => null)} />);

    await waitFor(() => {
      expect(screen.getByText(/network down/)).toBeTruthy();
    });
    // Panel + disclaimer chrome remain visible so the operator still
    // sees the section is alive.
    expect(screen.getByTestId("panel-surrogate-backtest")).toBeTruthy();
    expect(screen.getByTestId("surrogate-backtest-disclaimer")).toBeTruthy();
  });
});
