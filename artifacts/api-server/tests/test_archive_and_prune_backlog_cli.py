"""Smoke tests for ``scripts/archive_and_prune_backlog.py`` (task #757).

The script's full apply path requires Postgres + Supabase Storage —
out of scope for unit tests.  These smoke tests verify the
plumbing: argparse surface, the ``_resolve_loops`` filter, and the
underlying-loop accessor.  The end-to-end apply path is exercised by
the operator dry-run before turning a loop on.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow importing the script as a module.
_API_SERVER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_API_SERVER_ROOT))

from scripts import archive_and_prune_backlog as cli  # noqa: E402

from app import retention_loops_v757  # noqa: E402
from app.retention_base import (  # noqa: E402
    ArchiveThenDeleteRetention,
    DeleteOnlyRetention,
)


def test_resolve_loops_default_returns_all_22():
    out = cli._resolve_loops(None)
    assert len(out) == 22


def test_resolve_loops_by_table_name_matches_archive_loop():
    out = cli._resolve_loops("predictions")
    assert len(out) == 1
    assert out[0].state()["table"] == "predictions"


def test_resolve_loops_by_table_name_matches_delete_only_loop():
    out = cli._resolve_loops("pipeline_events")
    assert len(out) == 1
    assert out[0].state()["table"] == "pipeline_events"


def test_resolve_loops_unknown_table_exits():
    with pytest.raises(SystemExit) as exc:
        cli._resolve_loops("nonexistent_table_xyz")
    assert "no retention loop" in str(exc.value)


def test_underlying_returns_correct_class():
    archive_module = retention_loops_v757.ARCHIVE_MODULES[0]
    delete_module = retention_loops_v757.DELETE_ONLY_MODULES[0]
    assert isinstance(cli._underlying(archive_module), ArchiveThenDeleteRetention)
    assert isinstance(cli._underlying(delete_module), DeleteOnlyRetention)


def test_argparse_dry_run_and_apply_are_mutually_exclusive():
    """``main()`` raises SystemExit when both flags are given."""
    old_argv = sys.argv
    sys.argv = [
        "archive_and_prune_backlog.py", "--dry-run", "--apply",
    ]
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert "mutually exclusive" in str(exc.value)
    finally:
        sys.argv = old_argv
