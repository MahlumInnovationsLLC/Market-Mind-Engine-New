import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import {
  EloUpdaterDisplay,
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
