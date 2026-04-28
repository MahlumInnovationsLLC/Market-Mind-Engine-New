import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within, act } from "@testing-library/react";

vi.mock("@/hooks/use-websocket", () => ({
  useWebSocket: () => ({ status: "open", reconnectIn: null, closeCode: null }),
}));

vi.mock("@/components/ws-status-indicator", () => ({
  default: () => null,
}));

import SwarmTranscript, { useSwarmTranscript } from "./swarm-transcript";

function Harness({ onReady }: { onReady: (api: ReturnType<typeof useSwarmTranscript>) => void }) {
  const api = useSwarmTranscript();
  onReady(api);
  return (
    <SwarmTranscript
      status="open"
      reconnectIn={null}
      closeCode={null}
      store={api.store}
    />
  );
}

afterEach(() => {
  cleanup();
});

describe("SwarmTranscript Plackett-Luce sketch", () => {
  it("renders the per-entrant probability table when a debate_n_way_sketch event arrives", () => {
    let api: ReturnType<typeof useSwarmTranscript> | null = null;
    render(<Harness onReady={(a) => { api = a; }} />);
    expect(api).not.toBeNull();
    const store = api!.store;

    act(() => {
      store.ingest({
        type: "debate_start",
        debate_id: "dbg-atp-1",
        matchup: "ATP — Roland Garros 2025",
        league: "ATP",
        market_id: "KX-ATP-RG-2025",
        market_type: "outright",
        side: "YES",
        ts: "2026-04-27T12:00:00Z",
        personas: ["matchup_historian"],
      });
    });
    act(() => {
      store.ingest({
        type: "debate_n_way_sketch",
        debate_id: "dbg-atp-1",
        sketch: {
          model: "plackett_luce",
          entrant: "alcaraz",
          entrant_rating: 2200,
          entrant_probability: 0.45,
          field_size: 3,
          ratings: { alcaraz: 2200, sinner: 2150, djokovic: 2100 },
          probabilities: { alcaraz: 0.45, sinner: 0.35, djokovic: 0.2 },
        },
      });
    });

    const table = screen.getByTestId("debate-pl-sketch-dbg-atp-1");
    expect(table).toBeTruthy();

    const alcaraz = within(table).getByTestId("debate-pl-row-dbg-atp-1-alcaraz");
    const sinner = within(table).getByTestId("debate-pl-row-dbg-atp-1-sinner");
    const djokovic = within(table).getByTestId("debate-pl-row-dbg-atp-1-djokovic");
    expect(alcaraz.getAttribute("data-traded")).toBe("true");
    expect(sinner.getAttribute("data-traded")).toBe("false");
    expect(djokovic.getAttribute("data-traded")).toBe("false");
    expect(within(alcaraz).getByTestId("debate-pl-traded-badge-dbg-atp-1")).toBeTruthy();

    const orderedRows = within(table).getAllByTestId(/^debate-pl-row-dbg-atp-1-/);
    expect(orderedRows.map((r) => r.getAttribute("data-testid"))).toEqual([
      "debate-pl-row-dbg-atp-1-alcaraz",
      "debate-pl-row-dbg-atp-1-sinner",
      "debate-pl-row-dbg-atp-1-djokovic",
    ]);
  });

  it("does not render the Plackett-Luce table for binary moneyline debates", () => {
    let api: ReturnType<typeof useSwarmTranscript> | null = null;
    render(<Harness onReady={(a) => { api = a; }} />);
    expect(api).not.toBeNull();
    const store = api!.store;

    act(() => {
      store.ingest({
        type: "debate_start",
        debate_id: "dbg-nba-1",
        matchup: "Team A vs Team B",
        league: "NBA",
        market_id: "KX-NBA-001",
        market_type: "h2h",
        side: "YES",
        ts: "2026-04-27T12:00:00Z",
        personas: ["matchup_historian"],
      });
    });

    expect(screen.queryByTestId("debate-pl-sketch-dbg-nba-1")).toBeNull();
  });

  it("ignores debate_n_way_sketch events with a malformed sketch payload", () => {
    let api: ReturnType<typeof useSwarmTranscript> | null = null;
    render(<Harness onReady={(a) => { api = a; }} />);
    const store = api!.store;

    act(() => {
      store.ingest({
        type: "debate_start",
        debate_id: "dbg-bad-1",
        matchup: "Bad Sketch",
        league: "ATP",
        ts: "2026-04-27T12:00:00Z",
        personas: ["matchup_historian"],
      });
    });
    act(() => {
      store.ingest({
        type: "debate_n_way_sketch",
        debate_id: "dbg-bad-1",
        sketch: { probabilities: null },
      });
    });

    expect(screen.queryByTestId("debate-pl-sketch-dbg-bad-1")).toBeNull();
  });
});
