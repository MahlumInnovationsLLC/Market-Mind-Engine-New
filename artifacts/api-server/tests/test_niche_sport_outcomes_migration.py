"""Static checks for migration 0053_niche_sport_outcomes (task #831)."""
from __future__ import annotations

from pathlib import Path


_MIG = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0053_niche_sport_outcomes.py"
)


def test_migration_file_exists() -> None:
    assert _MIG.exists(), f"missing migration: {_MIG}"


def test_migration_chains_onto_0052() -> None:
    txt = _MIG.read_text()
    assert 'down_revision = "0052_predictions_is_live"' in txt
    assert 'revision = "0053_niche_sport_outcomes"' in txt


def test_migration_creates_niche_sport_outcomes_table() -> None:
    txt = _MIG.read_text()
    assert "CREATE TABLE IF NOT EXISTS niche_sport_outcomes" in txt
    # core columns
    for col in (
        "sport", "league", "event_id", "entry_id",
        "outcome", "outcome_int", "event_date", "source",
    ):
        assert col in txt, f"missing column: {col}"


def test_migration_declares_unique_idempotent_key() -> None:
    txt = _MIG.read_text()
    assert "CREATE UNIQUE INDEX IF NOT EXISTS niche_sport_outcomes_uq_idx" in txt
    # the natural key includes (sport, league, event_id, entry_id, outcome)
    # so a single fighter can show up as both W and L on the same event
    assert "(sport, league, event_id, entry_id, outcome)" in txt


def test_migration_downgrade_drops_table() -> None:
    txt = _MIG.read_text()
    assert "DROP TABLE IF EXISTS niche_sport_outcomes" in txt
