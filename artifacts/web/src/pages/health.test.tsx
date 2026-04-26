import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

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

// We render CardTile directly so the test only depends on the badge
// rendering logic, not the full /system page (auth, fetch, polling).
import { CardTile, CryptoFreshnessWidget } from "./health";

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

import { CryptoOutcomeBadge } from "./health";

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
