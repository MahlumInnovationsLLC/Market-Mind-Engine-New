"""Tests for the seed_team_elo CLI helpers (CSV loader, dedupe + sort).

These exercise the pure helpers without touching the network or DB —
the seeded replay path itself is already covered by `test_elo.py`'s
`test_seed_from_history_processes_and_dedupes`.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "seed_team_elo.py"


def _load_module():
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location("seed_team_elo", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def seed_mod():
    return _load_module()


def test_parse_played_at_normalizes_to_utc_aware(seed_mod) -> None:
    z = seed_mod._parse_played_at("2021-10-19T23:00:00Z")
    bare = seed_mod._parse_played_at("2021-10-19T23:00:00")
    offset = seed_mod._parse_played_at("2021-10-19T19:00:00-04:00")
    naive_dt = datetime(2021, 10, 19, 23, 0, 0)
    aware_dt = datetime(2021, 10, 19, 23, 0, 0, tzinfo=timezone.utc)
    for parsed in (z, bare, offset,
                   seed_mod._parse_played_at(naive_dt),
                   seed_mod._parse_played_at(aware_dt)):
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0
    assert z == bare == offset == aware_dt


def test_load_csv_round_trip(tmp_path, seed_mod) -> None:
    p = tmp_path / "games.csv"
    p.write_text(
        "league,home_team,away_team,home_score,away_score,game_id,played_at\n"
        "NBA,Lakers,Celtics,110,100,g-1,2021-10-19T23:00:00Z\n"
        "NFL,Chiefs,Bills,27,24,g-2,2021-10-10T20:00:00Z\n",
        encoding="utf-8",
    )
    rows = seed_mod.load_csv(p)
    assert len(rows) == 2
    assert {r["game_id"] for r in rows} == {"g-1", "g-2"}
    for r in rows:
        assert r["played_at"].tzinfo is not None
        assert isinstance(r["home_score"], float)


def test_load_csv_rejects_missing_columns(tmp_path, seed_mod) -> None:
    p = tmp_path / "bad.csv"
    p.write_text("league,home_team,away_team\nNBA,A,B\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        seed_mod.load_csv(p)


def test_merge_and_sort_dedupes_and_orders_chronologically(seed_mod) -> None:
    base = datetime(2021, 1, 1, tzinfo=timezone.utc)

    def g(i, league="NBA", gid=None, days=0):
        return {
            "league": league,
            "home_team": f"H{i}", "away_team": f"A{i}",
            "home_score": 1.0, "away_score": 0.0,
            "game_id": gid or f"id-{i}",
            "played_at": base.replace(day=1 + days),
        }

    src_a = [g(1, days=2), g(2, days=0)]
    src_b = [g(3, days=1), g(1, days=2)]      # duplicate (NBA, id-1)
    src_c = [g(1, league="NFL", gid="id-1", days=4)]  # different league

    merged = seed_mod.merge_and_sort(src_a, src_b, src_c)
    # Dedupe: 4 unique rows (NBA id-1/2/3 + NFL id-1)
    assert len(merged) == 4
    # Chronological by played_at
    assert [m["played_at"] for m in merged] == sorted(m["played_at"] for m in merged)
    # First-source-wins on duplicates
    nba_ids = [m["game_id"] for m in merged if m["league"] == "NBA"]
    assert sorted(nba_ids) == ["id-1", "id-2", "id-3"]


def test_merge_and_sort_handles_mixed_naive_aware_after_parse(seed_mod) -> None:
    """Regression: every input flows through `_parse_played_at` so naive
    + aware sources can still be sorted together without TypeError."""
    base_aware = datetime(2021, 6, 1, tzinfo=timezone.utc)
    base_naive = datetime(2021, 5, 1)
    rows = [
        {"league": "NBA", "home_team": "A", "away_team": "B",
         "home_score": 1, "away_score": 0, "game_id": "x1",
         "played_at": seed_mod._parse_played_at(base_aware)},
        {"league": "NBA", "home_team": "C", "away_team": "D",
         "home_score": 1, "away_score": 0, "game_id": "x2",
         "played_at": seed_mod._parse_played_at(base_naive)},
    ]
    merged = seed_mod.merge_and_sort(rows)
    assert merged[0]["game_id"] == "x2"
    assert merged[1]["game_id"] == "x1"
