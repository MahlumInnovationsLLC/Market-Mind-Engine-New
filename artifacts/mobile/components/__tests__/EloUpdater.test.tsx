/**
 * Component tests for the mobile Elo updater panel.
 *
 * Mirrors `artifacts/web/src/pages/admin.test.tsx` so the operator-on-
 * the-road sees the same scheduler counters / empty state / error state
 * the web operator console renders. A regression that drops a counter,
 * forgets the empty-state copy, or stops disabling the manual-run
 * button while a cycle is in flight will turn one of these red.
 */
import { act, fireEvent, render, screen } from "@testing-library/react-native";
import React from "react";

jest.mock("@/lib/api", () => ({
  apiFetch: jest.fn(),
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

import {
  EloUpdaterDisplay,
  computeEloNextRun,
  type EloUpdaterCycle,
  type EloUpdaterStatus,
} from "../EloUpdater";

const baseCycle: EloUpdaterCycle = {
  started_at: "2026-04-24T12:00:00Z",
  completed_at: "2026-04-24T12:00:01Z",
  duration_seconds: 1.234,
  games_examined: 12,
  games_updated: 7,
  games_skipped: 5,
  teams_touched: 14,
  errors: [],
};

describe("EloUpdaterDisplay", () => {
  it("renders the OK badge and counters when the scheduler has a clean last cycle", () => {
    const onRunNow = jest.fn();
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay
        status={status}
        onRunNow={onRunNow}
        running={false}
      />,
    );
    expect(screen.getByTestId("elo-updater-state").props.children).toMatch(
      /ok/i,
    );
    expect(screen.getByTestId("elo-updater-interval").props.children).toBe(
      "5m",
    );
    expect(screen.getByTestId("elo-updater-examined").props.children).toBe(
      "12",
    );
    expect(screen.getByTestId("elo-updater-updated").props.children).toBe("7");
    expect(
      String(screen.getByTestId("elo-updater-last-run").props.children),
    ).toMatch(/2026-04-24/);
    // Empty-state and errors blocks should not be present.
    expect(screen.queryByTestId("elo-updater-empty")).toBeNull();
    expect(screen.queryByTestId("elo-updater-errors")).toBeNull();
  });

  it("renders the empty state when the scheduler has not completed a cycle yet", () => {
    const onRunNow = jest.fn();
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: null,
    };
    render(
      <EloUpdaterDisplay
        status={status}
        onRunNow={onRunNow}
        running={false}
      />,
    );
    expect(screen.getByTestId("elo-updater-state").props.children).toMatch(
      /pending/i,
    );
    expect(screen.getByTestId("elo-updater-empty")).toBeTruthy();
    // The counters fall back to em-dashes — never NaN or 0.
    expect(screen.getByTestId("elo-updater-examined").props.children).toBe(
      "—",
    );
    expect(screen.getByTestId("elo-updater-updated").props.children).toBe("—");
    expect(screen.getByTestId("elo-updater-last-run").props.children).toBe(
      "—",
    );
  });

  it("renders the STOPPED badge when the scheduler is not running", () => {
    const onRunNow = jest.fn();
    const status: EloUpdaterStatus = {
      running: false,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay
        status={status}
        onRunNow={onRunNow}
        running={false}
      />,
    );
    expect(screen.getByTestId("elo-updater-state").props.children).toMatch(
      /stopped/i,
    );
  });

  it("surfaces last-cycle errors with an ERRORS badge", () => {
    const onRunNow = jest.fn();
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: { ...baseCycle, errors: ["game-1: boom", "game-2: oops"] },
    };
    render(
      <EloUpdaterDisplay
        status={status}
        onRunNow={onRunNow}
        running={false}
      />,
    );
    expect(screen.getByTestId("elo-updater-state").props.children).toMatch(
      /errors/i,
    );
    expect(screen.getByTestId("elo-updater-errors")).toBeTruthy();
    expect(screen.getByTestId("elo-updater-error-0").props.children).toEqual(
      expect.arrayContaining(["game-1: boom"]),
    );
    expect(screen.getByTestId("elo-updater-error-1").props.children).toEqual(
      expect.arrayContaining(["game-2: oops"]),
    );
  });

  it("invokes the onRunNow callback when the button is pressed", () => {
    const onRunNow = jest.fn();
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay
        status={status}
        onRunNow={onRunNow}
        running={false}
      />,
    );
    const btn = screen.getByTestId("button-elo-run-now");
    fireEvent.press(btn);
    expect(onRunNow).toHaveBeenCalledTimes(1);
  });

  it("disables the button and switches its label while a manual run is in flight", () => {
    const onRunNow = jest.fn();
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay
        status={status}
        onRunNow={onRunNow}
        running={true}
      />,
    );
    const btn = screen.getByTestId("button-elo-run-now");
    // accessibilityState reflects the disabled prop on TouchableOpacity.
    expect(btn.props.accessibilityState?.disabled).toBe(true);
    fireEvent.press(btn);
    // Disabled press is a no-op.
    expect(onRunNow).not.toHaveBeenCalled();
  });

  it("renders the manual-run result summary on success", () => {
    const onRunNow = jest.fn();
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    const result: EloUpdaterCycle = {
      ...baseCycle,
      games_examined: 3,
      games_updated: 2,
      games_skipped: 1,
      duration_seconds: 0.42,
      errors: [],
    };
    render(
      <EloUpdaterDisplay
        status={status}
        onRunNow={onRunNow}
        running={false}
        result={result}
      />,
    );
    const text = String(
      screen.getByTestId("elo-updater-run-result").props.children.join(""),
    );
    expect(text).toMatch(/examined 3/i);
    expect(text).toMatch(/updated 2/i);
    expect(text).toMatch(/skipped 1/i);
    expect(text).toMatch(/0\.42s/);
  });

  it("shows the run error and hides the success summary when the manual run fails", () => {
    const onRunNow = jest.fn();
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay
        status={status}
        onRunNow={onRunNow}
        running={false}
        error="boom"
        result={null}
      />,
    );
    expect(
      screen.getByTestId("elo-updater-run-error").props.children,
    ).toBe("boom");
    expect(screen.queryByTestId("elo-updater-run-result")).toBeNull();
  });
});

describe("computeEloNextRun (mobile)", () => {
  const status = (
    overrides: Partial<EloUpdaterStatus> = {},
  ): EloUpdaterStatus => ({
    running: true,
    cycle_seconds: 300,
    last_cycle: baseCycle,
    ...overrides,
  });

  it("returns 'in Xm' when the next cycle is still pending", () => {
    const now = Date.parse("2026-04-24T12:03:01Z");
    const r = computeEloNextRun(status(), now);
    expect(r).not.toBeNull();
    expect(r!.overdue).toBe(false);
    expect(r!.label).toBe("in 2m");
  });

  it("returns 'overdue Xs' once the next cycle has elapsed", () => {
    const now = Date.parse("2026-04-24T12:05:31Z");
    const r = computeEloNextRun(status(), now);
    expect(r!.overdue).toBe(true);
    expect(r!.label).toBe("overdue 30s");
  });

  it("returns null when the scheduler is stopped or has no completed cycle", () => {
    const now = Date.parse("2026-04-24T12:03:01Z");
    expect(computeEloNextRun(status({ running: false }), now)).toBeNull();
    expect(
      computeEloNextRun(status({ last_cycle: null }), now),
    ).toBeNull();
    expect(computeEloNextRun(status({ cycle_seconds: 0 }), now)).toBeNull();
  });
});

describe("EloUpdaterDisplay next-run countdown (mobile)", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date("2026-04-24T12:03:01Z"));
  });
  afterEach(() => {
    jest.useRealTimers();
  });

  it("shows a live next-run label derived from completed_at + cycle_seconds", () => {
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay
        status={status}
        onRunNow={jest.fn()}
        running={false}
      />,
    );
    expect(screen.getByTestId("elo-updater-next-run").props.children).toBe(
      "in 2m",
    );
    // Advance the in-component setInterval; the label should re-render
    // without any data refetch.
    act(() => {
      jest.advanceTimersByTime(1000);
    });
    expect(screen.getByTestId("elo-updater-next-run").props.children).toBe(
      "in 1m 59s",
    );
  });

  it("renders an em-dash for the next run when the scheduler is stopped", () => {
    const status: EloUpdaterStatus = {
      running: false,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay
        status={status}
        onRunNow={jest.fn()}
        running={false}
      />,
    );
    expect(screen.getByTestId("elo-updater-next-run").props.children).toBe(
      "—",
    );
  });

  it("flips to 'overdue Xs' once the next-run target is past", () => {
    jest.setSystemTime(new Date("2026-04-24T12:05:31Z"));
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay
        status={status}
        onRunNow={jest.fn()}
        running={false}
      />,
    );
    expect(screen.getByTestId("elo-updater-next-run").props.children).toBe(
      "overdue 30s",
    );
  });
});
