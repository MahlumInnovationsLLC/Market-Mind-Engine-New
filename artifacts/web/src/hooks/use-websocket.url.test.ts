import { afterEach, describe, expect, it, vi } from "vitest";

import { buildWsUrl } from "./use-websocket";

describe("buildWsUrl host knob (VITE_API_URL)", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("builds a same-origin ws URL when VITE_API_URL is empty", () => {
    vi.stubEnv("VITE_API_URL", "");
    // BASE_URL stays "/" in vitest's default config so the same-origin
    // branch should produce window.location-derived URLs.
    const url = buildWsUrl("/ws", null);
    expect(url.startsWith("ws://") || url.startsWith("wss://")).toBe(true);
    expect(url).toContain(window.location.host);
    expect(url.endsWith("/ws")).toBe(true);
  });

  it("upgrades https → wss against the configured backend", () => {
    vi.stubEnv("VITE_API_URL", "https://api.example.com");
    expect(buildWsUrl("/ws", null)).toBe("wss://api.example.com/ws");
  });

  it("downgrades http → ws against the configured backend", () => {
    vi.stubEnv("VITE_API_URL", "http://mac-studio.local:8000");
    expect(buildWsUrl("/ws", null)).toBe("ws://mac-studio.local:8000/ws");
  });

  it("strips the trailing slash on VITE_API_URL", () => {
    vi.stubEnv("VITE_API_URL", "https://api.example.com/");
    expect(buildWsUrl("/ws/llm-logs", null)).toBe(
      "wss://api.example.com/ws/llm-logs",
    );
  });

  it("appends the auth token query parameter when supplied", () => {
    vi.stubEnv("VITE_API_URL", "https://api.example.com");
    expect(buildWsUrl("/ws", "abc.def")).toBe(
      "wss://api.example.com/ws?token=abc.def",
    );
  });

  it("preserves a path prefix on the configured backend URL", () => {
    vi.stubEnv("VITE_API_URL", "https://example.com/axiom");
    expect(buildWsUrl("/ws", null)).toBe("wss://example.com/axiom/ws");
  });

  it("falls back to same-origin on a malformed VITE_API_URL", () => {
    vi.stubEnv("VITE_API_URL", "not a url");
    const url = buildWsUrl("/ws", null);
    expect(url).toContain(window.location.host);
    expect(url.endsWith("/ws")).toBe(true);
  });

  it("falls back to same-origin on an unsupported protocol", () => {
    // Mirrors `resolveApiUrl` — only http/https are valid backend
    // origins. ftp:, file:, javascript: must NEVER produce a real
    // WebSocket URL because the WS upgrade inherits the same auth
    // boundary as the REST surface.
    vi.stubEnv("VITE_API_URL", "ftp://example.com");
    const url = buildWsUrl("/ws", null);
    expect(url).toContain(window.location.host);
    expect(url.startsWith("ws://") || url.startsWith("wss://")).toBe(true);
    expect(url.endsWith("/ws")).toBe(true);
  });
});
