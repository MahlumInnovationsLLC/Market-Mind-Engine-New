import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

const { signalsState } = vi.hoisted(() => ({
  signalsState: {
    data: undefined as unknown,
    isLoading: false,
    error: null as unknown,
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetDashboardEdgeTerminalSignalsQueryKey: () => [
    "edge-terminal-recent-signals",
  ],
  useGetDashboardEdgeTerminalSignals: () => signalsState,
}));

import EdgeTerminalRecentSignalsTile from "./edge-terminal-recent-signals";

afterEach(() => {
  cleanup();
  signalsState.data = undefined;
  signalsState.isLoading = false;
  signalsState.error = null;
});

describe("EdgeTerminalRecentSignalsTile", () => {
  it("renders pick type, average odds, EV%, market id and source screenshot link", () => {
    signalsState.data = {
      limit: 25,
      generated_at: "2026-05-22T11:30:00Z",
      items: [
        {
          id: 42,
          signal_date: "2026-05-22",
          vertical: "nba",
          matchup: "Oklahoma City Thunder @ San Antonio Spurs",
          selection: "Thunder +2",
          ticker: "KXNBA-2026-OKC-SAS-SPREAD-THU2",
          disposition: "placed",
          scraped_at: "2026-05-22T11:29:30Z",
          pick_type: "spread",
          avg_odds: -103,
          ev_pct: 27.9,
          source_screenshot_url: "https://example.com/tile.png",
          kalshi_market_id: "00000000-0000-0000-0000-0000000000ab",
        },
      ],
    };

    render(<EdgeTerminalRecentSignalsTile />);

    const row = screen.getByTestId("edge-terminal-recent-signal-row-42");
    expect(
      within(row).getByTestId("edge-terminal-pick-type-42").textContent,
    ).toContain("SPREAD");
    expect(
      within(row).getByTestId("edge-terminal-avg-odds-42").textContent,
    ).toContain("-103");
    expect(
      within(row).getByTestId("edge-terminal-ev-pct-42").textContent,
    ).toContain("+27.9%");

    const link = within(row).getByTestId(
      "edge-terminal-screenshot-42",
    ) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("https://example.com/tile.png");
    expect(link.getAttribute("target")).toBe("_blank");

    const marketBadge = within(row).getByTestId("edge-terminal-market-id-42");
    expect(marketBadge.getAttribute("title")).toBe(
      "00000000-0000-0000-0000-0000000000ab",
    );

    const tickerLink = within(row).getByTestId(
      "edge-terminal-ticker-link-42",
    ) as HTMLAnchorElement;
    expect(tickerLink.getAttribute("href")).toBe(
      "https://kalshi.com/markets/kxnba/2026",
    );
    expect(tickerLink.getAttribute("target")).toBe("_blank");
    expect(tickerLink.getAttribute("rel")).toBe("noopener noreferrer");
    expect(tickerLink.textContent).toContain(
      "KXNBA-2026-OKC-SAS-SPREAD-THU2",
    );
  });

  it("does not render a ticker link when ticker and kalshi_market_id are missing", () => {
    signalsState.data = {
      limit: 25,
      generated_at: "2026-05-22T11:30:00Z",
      items: [
        {
          id: 13,
          signal_date: "2026-05-22",
          vertical: "nba",
          matchup: "OKC @ SAS",
          selection: "Thunder +2",
          ticker: null,
          disposition: "pending",
          scraped_at: null,
          pick_type: "spread",
          avg_odds: -110,
          ev_pct: 1.2,
          source_screenshot_url: null,
          kalshi_market_id: null,
        },
      ],
    };

    render(<EdgeTerminalRecentSignalsTile />);
    expect(screen.queryByTestId("edge-terminal-ticker-link-13")).toBeNull();
  });

  it("colors negative EV% red and shows em dash when fields are missing", () => {
    signalsState.data = {
      limit: 25,
      generated_at: "2026-05-22T11:30:00Z",
      items: [
        {
          id: 7,
          signal_date: "2026-05-22",
          vertical: "nhl",
          matchup: "VGK @ COL",
          selection: "Knights +1.5",
          ticker: null,
          disposition: "pending",
          scraped_at: null,
          pick_type: null,
          avg_odds: null,
          ev_pct: -12.4,
          source_screenshot_url: null,
          kalshi_market_id: null,
        },
      ],
    };

    render(<EdgeTerminalRecentSignalsTile />);
    const row = screen.getByTestId("edge-terminal-recent-signal-row-7");
    const ev = within(row).getByTestId("edge-terminal-ev-pct-7");
    expect(ev.textContent).toContain("-12.4%");
    expect(ev.className).toMatch(/text-rose-400/);
    expect(
      within(row).getByTestId("edge-terminal-pick-type-7").textContent,
    ).toContain("—");
    expect(
      within(row).getByTestId("edge-terminal-avg-odds-7").textContent,
    ).toContain("—");
  });

  it("does not render a clickable link for unsafe URL schemes", () => {
    signalsState.data = {
      limit: 25,
      generated_at: "2026-05-22T11:30:00Z",
      items: [
        {
          id: 99,
          signal_date: "2026-05-22",
          vertical: "nba",
          matchup: "OKC @ SAS",
          selection: "Thunder +2",
          ticker: null,
          disposition: "placed",
          scraped_at: null,
          pick_type: "spread",
          avg_odds: -110,
          ev_pct: 5.0,
          source_screenshot_url: "javascript:alert(1)",
          kalshi_market_id: null,
        },
      ],
    };

    render(<EdgeTerminalRecentSignalsTile />);
    expect(screen.queryByTestId("edge-terminal-screenshot-99")).toBeNull();
  });

  it("filters rows by status and shows a filtered empty-state message", () => {
    signalsState.data = {
      limit: 25,
      generated_at: "2026-05-22T11:30:00Z",
      items: [
        {
          id: 1,
          signal_date: "2026-05-22",
          vertical: "nba",
          matchup: "OKC @ SAS",
          selection: "Thunder +2",
          ticker: "KXNBA-1",
          disposition: "placed",
          scraped_at: null,
          pick_type: "spread",
          avg_odds: -110,
          ev_pct: 5,
          source_screenshot_url: null,
          kalshi_market_id: null,
        },
        {
          id: 2,
          signal_date: "2026-05-22",
          vertical: "nhl",
          matchup: "VGK @ COL",
          selection: "Knights ML",
          ticker: "KXNHL-2",
          disposition: "skipped_gate",
          scraped_at: null,
          pick_type: "moneyline",
          avg_odds: 120,
          ev_pct: -3,
          source_screenshot_url: null,
          kalshi_market_id: null,
        },
      ],
    };

    render(<EdgeTerminalRecentSignalsTile />);

    expect(screen.getByTestId("edge-terminal-recent-signal-row-1")).toBeTruthy();
    expect(screen.getByTestId("edge-terminal-recent-signal-row-2")).toBeTruthy();

    fireEvent.change(
      screen.getByTestId("edge-terminal-recent-signals-status-filter"),
      { target: { value: "placed" } },
    );

    expect(screen.getByTestId("edge-terminal-recent-signal-row-1")).toBeTruthy();
    expect(screen.queryByTestId("edge-terminal-recent-signal-row-2")).toBeNull();

    fireEvent.change(
      screen.getByTestId("edge-terminal-recent-signals-status-filter"),
      { target: { value: "errored" } },
    );

    expect(screen.queryByTestId("edge-terminal-recent-signal-row-1")).toBeNull();
    expect(screen.queryByTestId("edge-terminal-recent-signal-row-2")).toBeNull();
    expect(
      screen.getByTestId("edge-terminal-recent-signals-empty-filtered"),
    ).toBeTruthy();
  });

  it("treats both skipped_unresolved and skipped_gate as 'skipped'", () => {
    signalsState.data = {
      limit: 25,
      generated_at: "2026-05-22T11:30:00Z",
      items: [
        {
          id: 10,
          signal_date: "2026-05-22",
          vertical: "nba",
          matchup: "A @ B",
          selection: "A",
          ticker: null,
          disposition: "skipped_unresolved",
          scraped_at: null,
          pick_type: "spread",
          avg_odds: -110,
          ev_pct: 1,
          source_screenshot_url: null,
          kalshi_market_id: null,
        },
        {
          id: 11,
          signal_date: "2026-05-22",
          vertical: "nba",
          matchup: "C @ D",
          selection: "C",
          ticker: null,
          disposition: "placed",
          scraped_at: null,
          pick_type: "spread",
          avg_odds: -110,
          ev_pct: 1,
          source_screenshot_url: null,
          kalshi_market_id: null,
        },
      ],
    };
    render(<EdgeTerminalRecentSignalsTile />);
    fireEvent.change(
      screen.getByTestId("edge-terminal-recent-signals-status-filter"),
      { target: { value: "skipped" } },
    );
    expect(screen.getByTestId("edge-terminal-recent-signal-row-10")).toBeTruthy();
    expect(screen.queryByTestId("edge-terminal-recent-signal-row-11")).toBeNull();
  });

  it("combines search with dropdown filters using AND semantics", () => {
    signalsState.data = {
      limit: 25,
      generated_at: "2026-05-22T11:30:00Z",
      items: [
        {
          id: 20,
          signal_date: "2026-05-22",
          vertical: "nba",
          matchup: "Lakers @ Warriors",
          selection: "Lakers +4",
          ticker: "KXNBA-LAL-GSW",
          disposition: "placed",
          scraped_at: null,
          pick_type: "spread",
          avg_odds: -105,
          ev_pct: 4,
          source_screenshot_url: null,
          kalshi_market_id: null,
        },
        {
          id: 21,
          signal_date: "2026-05-22",
          vertical: "nba",
          matchup: "Lakers @ Warriors",
          selection: "Over 224.5",
          ticker: "KXNBA-LAL-GSW-TOT",
          disposition: "placed",
          pick_type: "total",
          scraped_at: null,
          avg_odds: -110,
          ev_pct: 2,
          source_screenshot_url: null,
          kalshi_market_id: null,
        },
      ],
    };
    render(<EdgeTerminalRecentSignalsTile />);

    fireEvent.change(
      screen.getByTestId("edge-terminal-recent-signals-search"),
      { target: { value: "lakers" } },
    );
    expect(screen.getByTestId("edge-terminal-recent-signal-row-20")).toBeTruthy();
    expect(screen.getByTestId("edge-terminal-recent-signal-row-21")).toBeTruthy();

    fireEvent.change(
      screen.getByTestId("edge-terminal-recent-signals-pick-type-filter"),
      { target: { value: "total" } },
    );
    expect(screen.queryByTestId("edge-terminal-recent-signal-row-20")).toBeNull();
    expect(screen.getByTestId("edge-terminal-recent-signal-row-21")).toBeTruthy();
  });

  it("shows the empty-state message when there are no items", () => {
    signalsState.data = {
      limit: 25,
      generated_at: "2026-05-22T11:30:00Z",
      items: [],
    };
    render(<EdgeTerminalRecentSignalsTile />);
    expect(screen.getByText("No signals yet.")).toBeTruthy();
  });
});
