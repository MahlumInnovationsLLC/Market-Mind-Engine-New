import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    data: undefined as unknown,
    isLoading: false,
    isError: false,
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetSupabaseGrowthRetentionStatusQueryKey: () => [
    "supabase-growth-retention",
  ],
  useGetSupabaseGrowthRetentionStatus: () => state,
  SupabaseGrowthRetentionLoopHealth: {
    ok: "ok",
    stale: "stale",
    error: "error",
    disabled: "disabled",
  },
}));

import SupabaseGrowthRetentionPanel from "./supabase-growth-retention-panel";

afterEach(() => {
  cleanup();
  state.data = undefined;
  state.isLoading = false;
  state.isError = false;
});

describe("SupabaseGrowthRetentionPanel", () => {
  it("renders the loading skeleton while the retention feed is in flight", () => {
    state.isLoading = true;
    render(<SupabaseGrowthRetentionPanel />);
    expect(
      screen.getByTestId("supabase-growth-retention-loading"),
    ).toBeTruthy();
  });

  it("renders the destructive error fallback when the fetch fails", () => {
    state.isError = true;
    render(<SupabaseGrowthRetentionPanel />);
    expect(
      screen.getByTestId("supabase-growth-retention-error").textContent,
    ).toMatch(/ERR_RETENTION_FETCH_FAIL/);
  });

  it("renders the destructive error fallback when data is missing", () => {
    state.data = undefined;
    render(<SupabaseGrowthRetentionPanel />);
    expect(
      screen.getByTestId("supabase-growth-retention-error"),
    ).toBeTruthy();
  });

  it("renders an empty-state row in each group when no loops are registered", () => {
    state.data = {
      archive: [],
      delete_only: [],
      counts: { ok: 0, stale: 0, error: 0, disabled: 0 },
      total: 0,
    };
    render(<SupabaseGrowthRetentionPanel />);
    const archiveTable = screen.getByTestId(
      "supabase-growth-retention-archive-table",
    );
    const deleteTable = screen.getByTestId(
      "supabase-growth-retention-delete-only-table",
    );
    expect(archiveTable.textContent).toMatch(/No loops registered/);
    expect(deleteTable.textContent).toMatch(/No loops registered/);
    expect(
      screen.getByTestId("supabase-growth-retention-summary").textContent,
    ).toMatch(/0 disabled \/ 0 ok \/ 0 stale \/ 0 error/);
  });

  it("renders archive vs delete-only loops, summary counts, health pills, archived summary, and last-error", () => {
    state.data = {
      archive: [
        {
          name: "ml_signals_archive",
          flavor: "archive",
          table: "ml_signals",
          health: "ok",
          last_tick_at: 1_700_000_000,
          seconds_since_last_tick: 12,
          last_deleted: 42,
          total_deleted: 4_242,
          last_archived_rows: 1_500,
          last_bytes_compressed: 2_048,
          last_error: null,
        },
        {
          name: "ml_features_archive",
          flavor: "archive",
          table: "ml_features",
          health: "error",
          last_tick_at: 1_700_000_000,
          seconds_since_last_tick: 90,
          last_deleted: 0,
          total_deleted: 17,
          last_archived_rows: 0,
          last_bytes_compressed: 0,
          last_error: "boom: storage put failed",
        },
      ],
      delete_only: [
        {
          name: "audit_log_prune",
          flavor: "delete-only",
          table: "audit_log",
          health: "stale",
          last_tick_at: 1_699_000_000,
          seconds_since_last_tick: 3_600,
          last_deleted: 7,
          total_deleted: 700,
          last_error: null,
        },
        {
          name: "snoozes_prune",
          flavor: "delete-only",
          table: "snoozes",
          health: "disabled",
          last_tick_at: null,
          seconds_since_last_tick: null,
          last_deleted: 0,
          total_deleted: 0,
          last_error: null,
        },
      ],
      counts: { ok: 1, stale: 1, error: 1, disabled: 1 },
      total: 4,
    };

    render(<SupabaseGrowthRetentionPanel />);

    // Summary badge reflects the counts block.
    expect(
      screen.getByTestId("supabase-growth-retention-summary").textContent,
    ).toMatch(/1 disabled \/ 1 ok \/ 1 stale \/ 1 error/);
    expect(
      screen.getByTestId("supabase-growth-retention-body").textContent,
    ).toMatch(/4 loops/);

    // Archive group renders its rows under the archive group testid.
    const archiveGroup = screen.getByTestId(
      "supabase-growth-retention-archive",
    );
    expect(archiveGroup.textContent).toMatch(/Archive-then-delete/);
    expect(
      screen.getByTestId(
        "supabase-growth-retention-archive-row-ml_signals_archive",
      ),
    ).toBeTruthy();
    expect(
      screen.getByTestId(
        "supabase-growth-retention-archive-row-ml_features_archive",
      ),
    ).toBeTruthy();

    // Delete-only group renders its rows under the delete-only group testid.
    const deleteGroup = screen.getByTestId(
      "supabase-growth-retention-delete-only",
    );
    expect(deleteGroup.textContent).toMatch(/Delete-only/);
    expect(
      screen.getByTestId(
        "supabase-growth-retention-delete-only-row-audit_log_prune",
      ),
    ).toBeTruthy();
    expect(
      screen.getByTestId(
        "supabase-growth-retention-delete-only-row-snoozes_prune",
      ),
    ).toBeTruthy();

    // Health pills carry the correct label per loop.
    expect(
      screen.getByTestId(
        "supabase-growth-retention-archive-health-ml_signals_archive",
      ).textContent,
    ).toMatch(/OK/);
    expect(
      screen.getByTestId(
        "supabase-growth-retention-archive-health-ml_features_archive",
      ).textContent,
    ).toMatch(/ERROR/);
    expect(
      screen.getByTestId(
        "supabase-growth-retention-delete-only-health-audit_log_prune",
      ).textContent,
    ).toMatch(/STALE/);
    expect(
      screen.getByTestId(
        "supabase-growth-retention-delete-only-health-snoozes_prune",
      ).textContent,
    ).toMatch(/DISABLED/);

    // Archive-flavor row renders archived rows + bytes summary.
    const archiveRow = screen.getByTestId(
      "supabase-growth-retention-archive-row-ml_signals_archive",
    );
    expect(archiveRow.textContent).toMatch(/1,500 rows/);
    expect(archiveRow.textContent).toMatch(/2\.0 KiB/);

    // Delete-only rows render "—" in the archived summary cell.
    const deleteRow = screen.getByTestId(
      "supabase-growth-retention-delete-only-row-audit_log_prune",
    );
    expect(deleteRow.textContent).toMatch(/—/);

    // Last-error renders only when present.
    expect(
      screen.getByTestId(
        "supabase-growth-retention-archive-error-ml_features_archive",
      ).textContent,
    ).toMatch(/boom: storage put failed/);
    expect(
      screen.queryByTestId(
        "supabase-growth-retention-archive-error-ml_signals_archive",
      ),
    ).toBeNull();
  });
});
