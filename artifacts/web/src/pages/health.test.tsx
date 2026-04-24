import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

// We render CardTile directly so the test only depends on the badge
// rendering logic, not the full /system page (auth, fetch, polling).
import { CardTile } from "./health";

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
