/**
 * Component tests for the mobile per-league calibration panel.
 *
 * Locks down the rendering branches that ship the same calibrator
 * health summary the web operator console renders. A regression that
 * hides a fitted league tile, drops the empty-state copy, or stops
 * surfacing the API error string would turn one of these red instead
 * of slipping out to the operator on the road.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react-native";
import React from "react";

const mockApiFetch = jest.fn();

jest.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
  ApiError: class ApiError extends Error {
    status: number;
    requestId: string;
    body: unknown;
    constructor(
      message: string,
      status: number,
      body: unknown,
      requestId: string,
    ) {
      super(message);
      this.status = status;
      this.body = body;
      this.requestId = requestId;
    }
  },
}));

import PerLeagueCalibration from "../PerLeagueCalibration";

interface CurvePoint {
  p_raw: number;
  p_cal: number;
}

interface LeagueItem {
  league: string;
  type: "isotonic" | "platt" | null;
  sample_count: number;
  fit_at: string | null;
  brier_before: number | null;
  brier_after: number | null;
  curve: CurvePoint[];
  knots: { x: number[]; y: number[] } | null;
}

function fittedItem(overrides: Partial<LeagueItem> = {}): LeagueItem {
  return {
    league: "NFL",
    type: "isotonic",
    sample_count: 42,
    fit_at: "2026-04-01T12:00:00Z",
    brier_before: 0.221,
    brier_after: 0.198,
    curve: [
      { p_raw: 0, p_cal: 0 },
      { p_raw: 0.5, p_cal: 0.55 },
      { p_raw: 1, p_cal: 1 },
    ],
    knots: { x: [0, 0.5, 1], y: [0, 0.55, 1] },
    ...overrides,
  };
}

function unfittedItem(league: string): LeagueItem {
  return {
    league,
    type: null,
    sample_count: 0,
    fit_at: null,
    brier_before: null,
    brier_after: null,
    curve: [],
    knots: null,
  };
}

function renderPanel(): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <PerLeagueCalibration />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe("PerLeagueCalibration (mobile)", () => {
  it("renders one tile per league, including unfitted ones", async () => {
    mockApiFetch.mockResolvedValueOnce({
      items: [fittedItem(), unfittedItem("NBA")],
      supported: ["NFL", "NBA"],
      fitted_count: 1,
    });

    renderPanel();

    expect(await screen.findByTestId("tile-calibration-NFL")).toBeTruthy();
    expect(screen.getByTestId("tile-calibration-NBA")).toBeTruthy();
    // Subtitle reflects the fitted_count over total leagues.
    expect(screen.getByText(/1\/2 leagues fitted/)).toBeTruthy();
    // Unfitted leagues advertise the threshold operators have to clear.
    expect(
      screen.getByText(/needs ≥10 settled trades/),
    ).toBeTruthy();
  });

  it("shows Brier delta with the right sign for an improving fit", async () => {
    mockApiFetch.mockResolvedValueOnce({
      items: [
        fittedItem({ brier_before: 0.221, brier_after: 0.198 }),
      ],
      supported: ["NFL"],
      fitted_count: 1,
    });

    renderPanel();

    const delta = await screen.findByTestId("tile-calibration-NFL-delta");
    // 0.198 - 0.221 = -0.023, formatted to 4 decimals.
    expect(delta).toHaveTextContent("Δ: -0.0230");
  });

  it("renders the empty state when the API returns no items", async () => {
    mockApiFetch.mockResolvedValueOnce({
      items: [],
      supported: [],
      fitted_count: 0,
    });

    renderPanel();

    await waitFor(() => {
      expect(
        screen.getByText(/No calibrator snapshots yet/),
      ).toBeTruthy();
    });
    // Subtitle still renders, just with 0/0.
    expect(screen.getByText(/0\/0 leagues fitted/)).toBeTruthy();
  });

  it("surfaces a server-side error message in the subtitle", async () => {
    mockApiFetch.mockResolvedValueOnce({
      items: [],
      supported: [],
      error: "calibration_snapshots query failed",
    });

    renderPanel();

    expect(
      await screen.findByText(/Failed to load: calibration_snapshots/),
    ).toBeTruthy();
  });

  it("shows a fetch failure message when the request itself throws", async () => {
    mockApiFetch.mockRejectedValueOnce(
      Object.assign(new Error("HTTP 500"), {
        status: 500,
        requestId: "abc123",
      }),
    );

    renderPanel();

    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load calibrators \(HTTP 500\)/),
      ).toBeTruthy();
    });
    expect(screen.getByText(/request_id=abc123/)).toBeTruthy();
  });
});
