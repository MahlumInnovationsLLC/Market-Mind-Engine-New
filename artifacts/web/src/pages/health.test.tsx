import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

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
