"""Migration 0052: predictions.is_live column (task #830).

Static checks — verifies the migration file declares the right
DDL without requiring a live Postgres connection:
  * adds ``is_live BOOLEAN NOT NULL DEFAULT FALSE`` (default-false
    so an existing row is automatically pre-game).
  * creates a partial index on ``(league) WHERE is_live = true`` so
    the live loop's per-league enumeration is index-backed.
  * chains correctly onto migration 0051.
"""
from __future__ import annotations

from pathlib import Path


_MIG = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0052_predictions_is_live.py"
)


def test_migration_file_exists() -> None:
    assert _MIG.exists(), f"missing migration: {_MIG}"


def test_migration_adds_is_live_column_default_false() -> None:
    txt = _MIG.read_text()
    assert "ADD COLUMN" in txt and "is_live" in txt
    # default must be false so existing rows are pre-game
    lowered = txt.lower()
    assert "default false" in lowered or "default 'false'" in lowered or \
        "default 0" in lowered or "default boolean 'false'" in lowered, (
        "is_live column must default to FALSE so existing predictions "
        "are pre-game until the in-game loop explicitly flips them"
    )


def test_migration_creates_partial_index_on_league_where_is_live() -> None:
    txt = _MIG.read_text().lower()
    assert "create" in txt and "index" in txt and "predictions" in txt
    assert "where" in txt and "is_live" in txt, (
        "partial index must be gated on is_live so the live loop's "
        "per-league fetch stays cheap on a mostly-pre-game table"
    )


def test_migration_revision_chains_onto_0051() -> None:
    txt = _MIG.read_text()
    assert "down_revision" in txt
    assert "0051_best_line_alert_actions" in txt, (
        "0052 must chain onto 0051_best_line_alert_actions"
    )
    assert "revision = \"0052_predictions_is_live\"" in txt or \
        "revision = '0052_predictions_is_live'" in txt
