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
  window.localStorage.clear();
});

const FILTERS_STORAGE_KEY = "axiom.edge-terminal-recent-signals-filters.v1";

function makeTwoSignalDataset() {
  return {
    limit: 25,
    generated_at: "2026-05-22T11:30:00Z",
    items: [
      {
        id: 100,
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
        id: 101,
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
}

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

  it("persists filter selections to localStorage and restores them on remount", () => {
    signalsState.data = makeTwoSignalDataset();

    const { unmount } = render(<EdgeTerminalRecentSignalsTile />);

    // Both rows visible by default.
    expect(screen.getByTestId("edge-terminal-recent-signal-row-100")).toBeTruthy();
    expect(screen.getByTestId("edge-terminal-recent-signal-row-101")).toBeTruthy();

    // Apply all four filter dimensions.
    fireEvent.change(
      screen.getByTestId("edge-terminal-recent-signals-status-filter"),
      { target: { value: "placed" } },
    );
    fireEvent.change(
      screen.getByTestId("edge-terminal-recent-signals-vertical-filter"),
      { target: { value: "nba" } },
    );
    fireEvent.change(
      screen.getByTestId("edge-terminal-recent-signals-pick-type-filter"),
      { target: { value: "spread" } },
    );
    fireEvent.change(
      screen.getByTestId("edge-terminal-recent-signals-search"),
      { target: { value: "thunder" } },
    );

    // Selections landed in localStorage under the documented key.
    const raw = window.localStorage.getItem(FILTERS_STORAGE_KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw as string)).toEqual({
      status: "placed",
      vertical: "nba",
      pickType: "spread",
      search: "thunder",
    });

    // Remount and confirm the inputs hydrate from localStorage.
    unmount();
    render(<EdgeTerminalRecentSignalsTile />);

    const statusSelect = screen.getByTestId(
      "edge-terminal-recent-signals-status-filter",
    ) as HTMLSelectElement;
    const verticalSelect = screen.getByTestId(
      "edge-terminal-recent-signals-vertical-filter",
    ) as HTMLSelectElement;
    const pickTypeSelect = screen.getByTestId(
      "edge-terminal-recent-signals-pick-type-filter",
    ) as HTMLSelectElement;
    const searchInput = screen.getByTestId(
      "edge-terminal-recent-signals-search",
    ) as HTMLInputElement;

    expect(statusSelect.value).toBe("placed");
    expect(verticalSelect.value).toBe("nba");
    expect(pickTypeSelect.value).toBe("spread");
    expect(searchInput.value).toBe("thunder");

    // Filtered view matches the restored selections.
    expect(screen.getByTestId("edge-terminal-recent-signal-row-100")).toBeTruthy();
    expect(screen.queryByTestId("edge-terminal-recent-signal-row-101")).toBeNull();

    // Reset filters clears localStorage and brings back every row.
    fireEvent.click(
      screen.getByTestId("edge-terminal-recent-signals-reset-filters"),
    );

    expect(statusSelect.value).toBe("all");
    expect(verticalSelect.value).toBe("all");
    expect(pickTypeSelect.value).toBe("all");
    expect(searchInput.value).toBe("");
    expect(
      screen.queryByTestId("edge-terminal-recent-signals-reset-filters"),
    ).toBeNull();
    expect(screen.getByTestId("edge-terminal-recent-signal-row-100")).toBeTruthy();
    expect(screen.getByTestId("edge-terminal-recent-signal-row-101")).toBeTruthy();

    const afterReset = window.localStorage.getItem(FILTERS_STORAGE_KEY);
    expect(JSON.parse(afterReset as string)).toEqual({
      status: "all",
      vertical: "all",
      pickType: "all",
      search: "",
    });
  });

  it("ignores corrupt persisted filter payloads and falls back to defaults", () => {
    window.localStorage.setItem(FILTERS_STORAGE_KEY, "{not-json");
    signalsState.data = makeTwoSignalDataset();

    render(<EdgeTerminalRecentSignalsTile />);

    const statusSelect = screen.getByTestId(
      "edge-terminal-recent-signals-status-filter",
    ) as HTMLSelectElement;
    expect(statusSelect.value).toBe("all");
    expect(screen.getByTestId("edge-terminal-recent-signal-row-100")).toBeTruthy();
    expect(screen.getByTestId("edge-terminal-recent-signal-row-101")).toBeTruthy();
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
