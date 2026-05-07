import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

import {
  EloUpdaterDisplay,
  computeEloNextRun,
  mergeOperatorAlertRows,
  type EloUpdaterStatus,
  type EloUpdaterCycle,
} from "./admin";

afterEach(() => cleanup());

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
    const onRunNow = vi.fn();
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
    expect(screen.getByTestId("elo-updater-state").textContent).toMatch(/ok/i);
    expect(screen.getByTestId("elo-updater-interval").textContent).toBe("5m");
    expect(screen.getByTestId("elo-updater-examined").textContent).toBe("12");
    expect(screen.getByTestId("elo-updater-updated").textContent).toBe("7");
    expect(screen.getByTestId("elo-updater-last-run").textContent).toMatch(
      /2026-04-24/,
    );
    // Empty-state and errors blocks should not be present.
    expect(screen.queryByTestId("elo-updater-empty")).toBeNull();
    expect(screen.queryByTestId("elo-updater-errors")).toBeNull();
  });

  it("renders the empty state when the scheduler has not completed a cycle yet", () => {
    const onRunNow = vi.fn();
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
    expect(screen.getByTestId("elo-updater-state").textContent).toMatch(
      /pending/i,
    );
    expect(screen.getByTestId("elo-updater-empty")).toBeTruthy();
    // The counters fall back to em-dashes — never NaN or 0.
    expect(screen.getByTestId("elo-updater-examined").textContent).toBe("—");
    expect(screen.getByTestId("elo-updater-updated").textContent).toBe("—");
    expect(screen.getByTestId("elo-updater-last-run").textContent).toBe("—");
  });

  it("renders the STOPPED badge when the scheduler is not running", () => {
    const onRunNow = vi.fn();
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
    expect(screen.getByTestId("elo-updater-state").textContent).toMatch(
      /stopped/i,
    );
  });

  it("surfaces last-cycle errors with an ERRORS badge", () => {
    const onRunNow = vi.fn();
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
    expect(screen.getByTestId("elo-updater-state").textContent).toMatch(
      /errors/i,
    );
    const errBox = screen.getByTestId("elo-updater-errors");
    expect(errBox.textContent).toMatch(/game-1: boom/);
    expect(errBox.textContent).toMatch(/game-2: oops/);
  });

  it("invokes the onRunNow callback when the button is clicked", () => {
    const onRunNow = vi.fn();
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
    expect(btn.textContent).toMatch(/run cycle now/i);
    fireEvent.click(btn);
    expect(onRunNow).toHaveBeenCalledTimes(1);
  });

  it("disables the button and switches its label while a manual run is in flight", () => {
    const onRunNow = vi.fn();
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay status={status} onRunNow={onRunNow} running={true} />,
    );
    const btn = screen.getByTestId("button-elo-run-now") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toMatch(/running/i);
  });

  it("renders the manual-run result summary on success", () => {
    const onRunNow = vi.fn();
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
    const summary = screen.getByTestId("elo-updater-run-result");
    expect(summary.textContent).toMatch(/examined 3/i);
    expect(summary.textContent).toMatch(/updated 2/i);
    expect(summary.textContent).toMatch(/skipped 1/i);
    expect(summary.textContent).toMatch(/0\.42s/);
  });

  it("shows the run error and hides the success summary when the manual run fails", () => {
    const onRunNow = vi.fn();
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
    expect(screen.getByTestId("elo-updater-run-error").textContent).toBe("boom");
    expect(screen.queryByTestId("elo-updater-run-result")).toBeNull();
  });
});

describe("computeEloNextRun", () => {
  const status = (overrides: Partial<EloUpdaterStatus> = {}): EloUpdaterStatus => ({
    running: true,
    cycle_seconds: 300,
    last_cycle: baseCycle,
    ...overrides,
  });

  it("returns an in-future label when the next cycle has not yet elapsed", () => {
    // baseCycle.completed_at = 2026-04-24T12:00:01Z, +300s -> 12:05:01Z
    const now = Date.parse("2026-04-24T12:03:01Z"); // 2 minutes in
    const r = computeEloNextRun(status(), now);
    expect(r).not.toBeNull();
    expect(r!.overdue).toBe(false);
    expect(r!.label).toBe("in 2m");
  });

  it("returns an overdue label when the next cycle is past", () => {
    // 30 seconds past the next-run target.
    const now = Date.parse("2026-04-24T12:05:31Z");
    const r = computeEloNextRun(status(), now);
    expect(r).not.toBeNull();
    expect(r!.overdue).toBe(true);
    expect(r!.label).toBe("overdue 30s");
  });

  it("includes seconds when the gap is more than a minute but not whole minutes", () => {
    // 65s remaining → "in 1m 5s"
    const completedAt = Date.parse(baseCycle.completed_at!);
    const now = completedAt + (300 - 65) * 1000;
    const r = computeEloNextRun(status(), now);
    expect(r!.label).toBe("in 1m 5s");
  });

  it("returns null when the scheduler is stopped", () => {
    const now = Date.parse("2026-04-24T12:03:01Z");
    expect(computeEloNextRun(status({ running: false }), now)).toBeNull();
  });

  it("returns null when there is no completed cycle yet", () => {
    const now = Date.parse("2026-04-24T12:03:01Z");
    expect(
      computeEloNextRun(status({ last_cycle: null }), now),
    ).toBeNull();
  });

  it("returns null when the cycle interval is non-positive", () => {
    const now = Date.parse("2026-04-24T12:03:01Z");
    expect(computeEloNextRun(status({ cycle_seconds: 0 }), now)).toBeNull();
    expect(computeEloNextRun(status({ cycle_seconds: -10 }), now)).toBeNull();
  });
});

describe("EloUpdaterDisplay next-run countdown", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // Fix the wall clock to a known instant inside the cycle window.
    vi.setSystemTime(new Date("2026-04-24T12:03:01Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows a live 'in Xm/Xs' countdown derived from completed_at + cycle_seconds", () => {
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay status={status} onRunNow={vi.fn()} running={false} />,
    );
    expect(screen.getByTestId("elo-updater-next-run").textContent).toBe(
      "in 2m",
    );
    // After ticking 1 second the countdown should advance without a refetch.
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByTestId("elo-updater-next-run").textContent).toBe(
      "in 1m 59s",
    );
  });

  it("flips to 'overdue Xs' once the next-run target has elapsed", () => {
    vi.setSystemTime(new Date("2026-04-24T12:05:31Z"));
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay status={status} onRunNow={vi.fn()} running={false} />,
    );
    expect(screen.getByTestId("elo-updater-next-run").textContent).toBe(
      "overdue 30s",
    );
  });

  it("renders an em-dash for the next run when the scheduler is stopped", () => {
    const status: EloUpdaterStatus = {
      running: false,
      cycle_seconds: 300,
      last_cycle: baseCycle,
    };
    render(
      <EloUpdaterDisplay status={status} onRunNow={vi.fn()} running={false} />,
    );
    expect(screen.getByTestId("elo-updater-next-run").textContent).toBe("—");
  });

  it("renders an em-dash for the next run when there is no completed cycle", () => {
    const status: EloUpdaterStatus = {
      running: true,
      cycle_seconds: 300,
      last_cycle: null,
    };
    render(
      <EloUpdaterDisplay status={status} onRunNow={vi.fn()} running={false} />,
    );
    expect(screen.getByTestId("elo-updater-next-run").textContent).toBe("—");
  });
});

describe("mergeOperatorAlertRows", () => {
  it("renders a row for every configured recipient even when they have no override", () => {
    const rows = mergeOperatorAlertRows(
      ["Ops@example.com", "ceo@example.com"],
      [],
    );
    expect(rows).toHaveLength(2);
    const ops = rows.find((r) => r.email === "ops@example.com");
    expect(ops).toMatchObject({
      enabled: true,
      override: false,
      configured: true,
      updated_at: null,
    });
  });

  it("merges overrides into the configured roster and reflects effective state", () => {
    const rows = mergeOperatorAlertRows(
      ["ops@example.com", "ceo@example.com"],
      [
        {
          email: "ops@example.com",
          alert_type: "operator_trade_alert",
          enabled: false,
          updated_at: "2026-05-01T00:00:00Z",
        },
        {
          email: "ex-employee@example.com",
          alert_type: "operator_trade_alert",
          enabled: false,
          updated_at: "2026-04-01T00:00:00Z",
        },
      ],
    );
    expect(rows.map((r) => r.email)).toEqual([
      "ceo@example.com",
      "ops@example.com",
      "ex-employee@example.com",
    ]);
    const ops = rows.find((r) => r.email === "ops@example.com");
    expect(ops).toMatchObject({ enabled: false, override: true, configured: true });
    const ex = rows.find((r) => r.email === "ex-employee@example.com");
    expect(ex).toMatchObject({ enabled: false, override: true, configured: false });
  });

  it("handles empty input safely", () => {
    expect(mergeOperatorAlertRows([], [])).toEqual([]);
  });
});
