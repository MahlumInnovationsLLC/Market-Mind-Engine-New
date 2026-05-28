"""Unit tests for the partitioning helpers (task #921).

Pure-string SQL builders + month math — no DB required.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import partitioning


# ----------------------------------------------------------------------
# month math
# ----------------------------------------------------------------------


def test_month_floor_truncates_to_first_instant():
    dt = datetime(2026, 5, 28, 14, 33, tzinfo=timezone.utc)
    assert partitioning.month_floor(dt) == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_month_floor_assumes_utc_for_naive():
    dt = datetime(2026, 5, 28, 14, 33)
    out = partitioning.month_floor(dt)
    assert out.tzinfo is timezone.utc
    assert out.year == 2026 and out.month == 5 and out.day == 1


def test_add_months_handles_year_rollover():
    nov = datetime(2026, 11, 15, tzinfo=timezone.utc)
    assert partitioning.add_months(nov, 1).month == 12
    assert partitioning.add_months(nov, 2) == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert partitioning.add_months(nov, -11) == datetime(2025, 12, 1, tzinfo=timezone.utc)


def test_month_bounds_end_is_exclusive_start_of_next_month():
    b = partitioning.month_bounds(datetime(2026, 5, 28, tzinfo=timezone.utc))
    assert b.start == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert b.end == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert b.suffix == "2026_05"


def test_month_bounds_december_rolls_to_january():
    b = partitioning.month_bounds(datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert b.end == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_months_between_inclusive():
    start = datetime(2026, 3, 20, tzinfo=timezone.utc)
    end = datetime(2026, 5, 10, tzinfo=timezone.utc)
    out = partitioning.months_between(start, end)
    assert [b.suffix for b in out] == ["2026_03", "2026_04", "2026_05"]


def test_months_between_empty_when_end_before_start():
    assert partitioning.months_between(
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        datetime(2026, 4, 1, tzinfo=timezone.utc),
    ) == []


# ----------------------------------------------------------------------
# identifier safety
# ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "", "line_snapshots; DROP TABLE x", "Line_Snapshots",
    "1_snapshots", "snap-shots", "snap shots", "snap\"shots",
])
def test_unsafe_identifiers_rejected(bad: str):
    b = partitioning.MonthBounds(2026, 5)
    with pytest.raises(ValueError):
        partitioning.create_partition_sql(bad, b)
    with pytest.raises(ValueError):
        partitioning.drop_partition_sql(bad, b)
    with pytest.raises(ValueError):
        partitioning.partition_name(bad, b)
    with pytest.raises(ValueError):
        partitioning.list_partitions_sql(bad)


def test_safe_identifiers_accepted():
    b = partitioning.MonthBounds(2026, 5)
    assert partitioning.partition_name("line_snapshots", b) == "line_snapshots_2026_05"


# ----------------------------------------------------------------------
# SQL shape
# ----------------------------------------------------------------------


def test_create_partition_sql_is_idempotent_and_uses_month_bounds():
    b = partitioning.MonthBounds(2026, 5)
    sql = partitioning.create_partition_sql("line_snapshots", b)
    assert "CREATE TABLE IF NOT EXISTS line_snapshots_2026_05" in sql
    assert "PARTITION OF line_snapshots" in sql
    assert "FOR VALUES FROM ('2026-05-01" in sql
    assert "TO ('2026-06-01" in sql


def test_drop_partition_sql_detaches_then_drops():
    b = partitioning.MonthBounds(2026, 3)
    sql = partitioning.drop_partition_sql("odds_changes", b)
    # DETACH must come before DROP so a long SELECT against the parent
    # doesn't block.
    detach_idx = sql.index("DETACH PARTITION odds_changes_2026_03")
    drop_idx = sql.index("DROP TABLE IF EXISTS odds_changes_2026_03")
    assert detach_idx < drop_idx


def test_list_partitions_sql_only_matches_monthly_children():
    sql = partitioning.list_partitions_sql("line_snapshots")
    assert "parent.relname = 'line_snapshots'" in sql
    # the regex must match exactly _YYYY_MM at end-of-string
    assert "'_\\d{4}_\\d{2}$'" in sql


# ----------------------------------------------------------------------
# retention planning
# ----------------------------------------------------------------------


def test_partitions_to_drop_uses_end_bound():
    # 30-day retention cutoff on 2026-05-28 = 2026-04-28; only the
    # partitions whose .end <= cutoff should be dropped.
    cutoff = datetime(2026, 4, 28, tzinfo=timezone.utc)
    months = [
        partitioning.MonthBounds(2026, 2),  # end = 2026-03-01  → drop
        partitioning.MonthBounds(2026, 3),  # end = 2026-04-01  → drop
        partitioning.MonthBounds(2026, 4),  # end = 2026-05-01  → keep (covers cutoff)
        partitioning.MonthBounds(2026, 5),  # end = 2026-06-01  → keep
    ]
    drop = partitioning.partitions_to_drop(months, cutoff=cutoff)
    assert [b.suffix for b in drop] == ["2026_02", "2026_03"]


def test_partitions_to_drop_empty_when_all_recent():
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    months = [partitioning.MonthBounds(2026, 5)]
    assert partitioning.partitions_to_drop(months, cutoff=cutoff) == []


def test_partitions_to_drop_accepts_naive_cutoff():
    # The runtime calls cutoff() which returns tz-aware, but defend
    # against operator scripts that pass a naive datetime.
    cutoff = datetime(2026, 4, 28)
    months = [partitioning.MonthBounds(2026, 2)]
    assert len(partitioning.partitions_to_drop(months, cutoff=cutoff)) == 1


def test_future_partitions_needed_skips_existing():
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    existing = {"2026_05", "2026_06"}
    needed = partitioning.future_partitions_needed(
        now=now, months_ahead=2, existing_suffixes=existing,
    )
    assert [b.suffix for b in needed] == ["2026_07"]


def test_future_partitions_needed_includes_current_month_when_missing():
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    needed = partitioning.future_partitions_needed(
        now=now, months_ahead=1, existing_suffixes=set(),
    )
    assert [b.suffix for b in needed] == ["2026_05", "2026_06"]


# ----------------------------------------------------------------------
# retention loop env knobs (no DB)
# ----------------------------------------------------------------------


def test_partition_retention_default_env_knobs(monkeypatch):
    from app import partitioned_table_retention as ptr

    # Clear any env from the test process so defaults assert cleanly.
    for k in (
        "LINE_SNAPSHOT_RETENTION_DAYS",
        "ODDS_CHANGES_RETENTION_DAYS",
        "AXIOM_LINE_SNAPSHOTS_PARTITIONED_RETENTION_ENABLED",
        "AXIOM_ODDS_CHANGES_PARTITIONED_RETENTION_ENABLED",
        "AXIOM_PARTITIONED_RETENTION_INTERVAL_SECONDS",
        "AXIOM_PARTITIONED_RETENTION_MONTHS_AHEAD",
    ):
        monkeypatch.delenv(k, raising=False)

    line, odds = ptr.INSTANCES
    assert line.parent_table == "line_snapshots"
    assert odds.parent_table == "odds_changes"
    assert line.retention_days() == 30
    assert odds.retention_days() == 30
    assert line.enabled() is False
    assert odds.enabled() is False
    assert line.interval_seconds() == 86400.0
    assert line.months_ahead() == 2


def test_partition_retention_respects_env(monkeypatch):
    from app import partitioned_table_retention as ptr
    monkeypatch.setenv("LINE_SNAPSHOT_RETENTION_DAYS", "14")
    monkeypatch.setenv("AXIOM_LINE_SNAPSHOTS_PARTITIONED_RETENTION_ENABLED", "1")
    monkeypatch.setenv("AXIOM_PARTITIONED_RETENTION_INTERVAL_SECONDS", "3600")

    line, _ = ptr.INSTANCES
    assert line.retention_days() == 14
    assert line.enabled() is True
    assert line.interval_seconds() == 3600.0


def test_partition_retention_refuses_zero_days(monkeypatch):
    from app import partitioned_table_retention as ptr
    monkeypatch.setenv("LINE_SNAPSHOT_RETENTION_DAYS", "0")
    line, _ = ptr.INSTANCES
    # 0 / negative would wipe the table — clamped to >= 1.
    assert line.retention_days() == 1


def test_partition_retention_state_shape(monkeypatch):
    from app import partitioned_table_retention as ptr
    for k in (
        "AXIOM_LINE_SNAPSHOTS_PARTITIONED_RETENTION_ENABLED",
        "AXIOM_ODDS_CHANGES_PARTITIONED_RETENTION_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)

    ptr._reset_state_for_tests()
    states = ptr.states()
    assert len(states) == 2
    keys_required = {
        "name", "flavor", "table", "running", "enabled",
        "retention_days", "interval_seconds", "months_ahead",
        "last_dropped", "last_created", "total_dropped", "total_created",
        "retention_env", "enabled_env",
    }
    for s in states:
        missing = keys_required - set(s.keys())
        assert not missing, f"state missing keys: {missing} (state={s})"
        assert s["flavor"] == "partitioned-drop"
        assert s["running"] is False
        assert s["enabled"] is False
