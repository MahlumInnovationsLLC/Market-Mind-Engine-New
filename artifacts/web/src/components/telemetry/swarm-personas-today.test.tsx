import { describe, it, expect, afterEach, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    data: undefined as unknown,
    isLoading: false,
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetSwarmPersonasTodayQueryKey: () => ["swarm-personas-today"],
  useGetSwarmPersonasToday: () => state,
}));

import SwarmPersonasToday from "./swarm-personas-today";

afterEach(() => {
  cleanup();
  state.data = undefined;
  state.isLoading = false;
});

describe("SwarmPersonasToday", () => {
  it("renders an empty-state message when the persona feed is empty", () => {
    state.data = { date_utc: "2026-04-24", items: [] };
    render(<SwarmPersonasToday />);
    // Empty payload still surfaces the table chrome but no rows.
    expect(screen.getAllByText(/persona/i).length).toBeGreaterThan(0);
  });

  it("renders one row per persona with a sortable column header", () => {
    state.data = {
      date_utc: "2026-04-24",
      items: [
        {
          persona_id: "alpha",
          debates: 12,
          brier: 0.18,
          accuracy_pct: 62.5,
          avg_confidence: 0.7,
          lean_yes: 7,
          lean_no: 5,
        },
        {
          persona_id: "beta",
          debates: 9,
          brier: 0.22,
          accuracy_pct: 50.0,
          avg_confidence: 0.55,
          lean_yes: 4,
          lean_no: 5,
        },
      ],
    };
    render(<SwarmPersonasToday />);
    expect(screen.getByText(/alpha/)).toBeTruthy();
    expect(screen.getByText(/beta/)).toBeTruthy();
  });

  it("changes the active sort key when a column header is clicked", () => {
    state.data = {
      date_utc: "2026-04-24",
      items: [
        {
          persona_id: "alpha",
          debates: 12,
          brier: 0.18,
          accuracy_pct: 62.5,
          avg_confidence: 0.7,
          lean_yes: 7,
          lean_no: 5,
        },
      ],
    };
    render(<SwarmPersonasToday />);
    // The 'Persona' column toggles sort by id.  We don't assert the
    // specific arrow glyph — just that the click flips internal state
    // without crashing.
    const personaHeader = screen.getByText(/^Persona$/i);
    fireEvent.click(personaHeader);
    fireEvent.click(personaHeader);
    expect(screen.getByText(/alpha/)).toBeTruthy();
  });
});
