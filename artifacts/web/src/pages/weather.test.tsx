import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { qstate } = vi.hoisted(() => ({
  qstate: {
    data: undefined as unknown,
    isLoading: false,
    isError: false,
    error: undefined as unknown,
  },
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => qstate,
}));

vi.mock("@workspace/api-client-react", () => ({
  customFetch: vi.fn(),
}));

import WeatherPage from "./weather";

afterEach(() => {
  cleanup();
  qstate.data = undefined;
  qstate.isLoading = false;
  qstate.isError = false;
  qstate.error = undefined;
});

describe("WeatherPage", () => {
  it("renders the loading affordance while the freshness query is in flight", () => {
    qstate.isLoading = true;
    render(<WeatherPage />);
    expect(screen.getByTestId("weather-page")).toBeTruthy();
    expect(screen.getByText(/Loading freshness/i)).toBeTruthy();
  });

  it("renders the error block with the underlying message when the query fails", () => {
    qstate.isError = true;
    qstate.error = new Error("502 bad gateway");
    render(<WeatherPage />);
    const err = screen.getByTestId("weather-error");
    expect(err.textContent).toMatch(/502 bad gateway/);
  });

  it("renders one section per city plus all five source badges per city", () => {
    qstate.data = {
      as_of: "2026-04-24T12:00:00Z",
      freshness_windows_s: { "open-meteo": 600 },
      cities: [
        {
          code: "NYC",
          name: "New York",
          sources: {
            "open-meteo": {
              observed_at: "2026-04-24T11:55:00Z",
              status: "fresh",
              max_age_s: 600,
            },
            nws: {
              observed_at: null,
              status: "missing",
              max_age_s: 1800,
            },
            "tomorrow.io": {
              observed_at: "2026-04-24T11:00:00Z",
              status: "stale",
              max_age_s: 1800,
            },
            accuweather: {
              observed_at: "2026-04-24T11:50:00Z",
              status: "fresh",
              max_age_s: 1800,
            },
            hrrr: {
              observed_at: "2026-04-24T11:30:00Z",
              status: "fresh",
              max_age_s: 3600,
              run_cycle: "12Z",
              run_time: "2026-04-24T12:00:00Z",
            },
          },
        },
      ],
    };
    render(<WeatherPage />);
    expect(screen.getByTestId("city-NYC")).toBeTruthy();
    expect(screen.getAllByText(/Open-Meteo/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/NWS/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Tomorrow\.io/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/AccuWeather/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/HRRR/i).length).toBeGreaterThan(0);
  });

  it("renders the empty-cities sentinel when the response has no cities", () => {
    qstate.data = {
      as_of: "2026-04-24T12:00:00Z",
      freshness_windows_s: {},
      cities: [],
    };
    render(<WeatherPage />);
    expect(
      screen.getByText(/No weather cities configured/i),
    ).toBeTruthy();
  });
});
