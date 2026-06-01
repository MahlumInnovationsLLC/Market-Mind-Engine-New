"""Coverage for the migration-0030 outcome-column wiring.

Before migration 0030, ``_persist_odds_payload`` flattened all
outcomes for one (game, book, market, ts) tuple into the same
unique-index slot, and ``ON CONFLICT DO NOTHING`` silently dropped
every outcome after the first.  These tests pin down the new
behavior so a future refactor doesn't quietly regress it:

* Each outcome ends up as a SEPARATE row in the ``snapshot_rows``
  list with its own ``outcome`` field populated from
  ``outcome.name``.
* The INSERT statement actually includes ``outcome`` in both the
  column list and the ON CONFLICT tuple (string check on the SQL).
* Long outcome names (rare but possible for soccer team names that
  also include sponsor strings) are truncated to 255 chars rather
  than raising during the bind.
* ``odds_changes`` rows are unaffected (that table doesn't have an
  outcome column and never had this dedup bug).
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

import pytest

from app.feeds import odds_api


# Named positional indexes for the snapshot tuple inserted by
# ``_persist_odds_payload``.  Tracking these here (not magic ``-1``
# slot lookups) means a future producer-side column reorder will
# fail one well-named test instead of silently passing nonsense.
# Tuple shape:
#   ($1 game_id, $2 sport, $3 book, $4 market_type,
#    $5 line_value, $6 odds_value,
#    $7 home_team, $8 away_team, $9 is_opening, $10 outcome)
SNAP_GAME_ID, SNAP_SPORT, SNAP_BOOK, SNAP_MARKET = 0, 1, 2, 3
SNAP_LINE_VALUE, SNAP_ODDS_VALUE = 4, 5
SNAP_HOME, SNAP_AWAY, SNAP_IS_OPENING, SNAP_OUTCOME = 6, 7, 8, 9
SNAP_TUPLE_LEN = 10


class _FakeConn:
    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list[tuple]]] = []

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.executemany_calls.append((sql, list(rows)))

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *_a):
                return False

        return _Tx()


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_a) -> bool:
        return False


def _h2h_payload() -> list[dict[str, Any]]:
    """A single h2h game from one bookmaker with home + away outcomes."""
    return [
        {
            "id": "GAME_A",
            "home_team": "Detroit Pistons",
            "away_team": "Orlando Magic",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Detroit Pistons", "price": 1.95},
                                {"name": "Orlando Magic", "price": 1.92},
                            ],
                        },
                    ],
                },
            ],
        }
    ]


def _totals_payload() -> list[dict[str, Any]]:
    """A single totals game with Over + Under sharing one point."""
    return [
        {
            "id": "GAME_B",
            "home_team": "Bayern Munich",
            "away_team": "Heidenheim",
            "bookmakers": [
                {
                    "key": "williamhill_us",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 1.9, "point": 3.5},
                                {"name": "Under", "price": 1.95, "point": 3.5},
                            ],
                        },
                    ],
                },
            ],
        }
    ]


def _run(coro):
    """Run a coroutine in a fresh event loop.

    Earlier tests in the suite (e.g. ``test_model_health_alerts_seq_repair``)
    use ``asyncio.run()``, which on Python 3.10+ leaves the policy's
    current loop unset. The legacy ``asyncio.get_event_loop()``
    pattern then raises ``RuntimeError: There is no current event
    loop``. Always create our own fresh loop and tear it down cleanly
    so this test file works regardless of execution order.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_h2h_persists_one_row_per_outcome_with_outcome_name() -> None:
    conn = _FakeConn()
    with mock.patch.object(odds_api.db, "acquire", return_value=_FakeAcquireCM(conn)):
        written = _run(odds_api._persist_odds_payload("basketball_nba", _h2h_payload()))

    assert written == 2  # both outcomes counted
    snapshot_sql, snapshot_rows = conn.executemany_calls[0]
    # Two rows, one per outcome — this is the WHOLE POINT of mig 0030.
    assert len(snapshot_rows) == 2
    assert all(len(r) == SNAP_TUPLE_LEN for r in snapshot_rows)
    assert snapshot_rows[0][SNAP_OUTCOME] == "Detroit Pistons"
    assert snapshot_rows[1][SNAP_OUTCOME] == "Orlando Magic"
    # Same (game, book, market_type) — only outcome differentiates them.
    for r in snapshot_rows:
        assert r[SNAP_GAME_ID] == "GAME_A"
        assert r[SNAP_SPORT] == "basketball_nba"
        assert r[SNAP_BOOK] == "fanduel"
        assert r[SNAP_MARKET] == "h2h"
    # SQL must reference outcome in both the column list AND the conflict tuple.
    assert "outcome" in snapshot_sql
    assert "ON CONFLICT (game_id, book, market_type, outcome" in snapshot_sql


def test_totals_persists_over_and_under_separately() -> None:
    conn = _FakeConn()
    with mock.patch.object(odds_api.db, "acquire", return_value=_FakeAcquireCM(conn)):
        written = _run(odds_api._persist_odds_payload("soccer_germany_bundesliga", _totals_payload()))

    assert written == 2
    _, snapshot_rows = conn.executemany_calls[0]
    assert len(snapshot_rows) == 2
    outcomes = {r[-1] for r in snapshot_rows}
    assert outcomes == {"Over", "Under"}
    # line_value (5th positional) is identical for Over and Under — they share the line.
    assert snapshot_rows[0][4] == snapshot_rows[1][4] == 3.5


def test_long_outcome_name_is_truncated_not_raised() -> None:
    """A pathological 400-char outcome.name should not crash the batch."""
    payload = [
        {
            "id": "GAME_C",
            "home_team": "A",
            "away_team": "B",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "X" * 400, "price": 2.0},
                                {"name": "Y", "price": 2.0},
                            ],
                        },
                    ],
                },
            ],
        }
    ]
    conn = _FakeConn()
    with mock.patch.object(odds_api.db, "acquire", return_value=_FakeAcquireCM(conn)):
        written = _run(odds_api._persist_odds_payload("test_sport", payload))
    assert written == 2
    _, snapshot_rows = conn.executemany_calls[0]
    assert len(snapshot_rows[0][-1]) == 255
    assert snapshot_rows[0][-1] == "X" * 255
    assert snapshot_rows[1][-1] == "Y"


def test_odds_changes_rows_unaffected_by_outcome_column() -> None:
    """odds_changes still has the original tuple shape — no outcome col."""
    conn = _FakeConn()
    with mock.patch.object(odds_api.db, "acquire", return_value=_FakeAcquireCM(conn)):
        _run(odds_api._persist_odds_payload("basketball_nba", _h2h_payload()))

    changes_sql, changes_rows = conn.executemany_calls[1]
    assert "outcome" not in changes_sql.lower()
    assert len(changes_rows) == 2
    # 5-tuple: game_id, book, market_type, line, odds.  The timestamp column
    # is filled by SQL ``NOW()`` (see the INSERT), so it is NOT a bound param
    # — a real datetime in-DB rather than an ISO string asyncpg would reject.
    assert all(len(r) == 5 for r in changes_rows)
    assert "now()" in changes_sql.lower()


def test_missing_outcome_name_persists_as_null() -> None:
    """Defensive: if the API ever omits outcome.name we keep a NULL row."""
    payload = [
        {
            "id": "GAME_D",
            "home_team": "A",
            "away_team": "B",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [{"price": 2.0}],  # no name field
                        }
                    ],
                }
            ],
        }
    ]
    conn = _FakeConn()
    with mock.patch.object(odds_api.db, "acquire", return_value=_FakeAcquireCM(conn)):
        written = _run(odds_api._persist_odds_payload("test_sport", payload))
    assert written == 1
    _, snapshot_rows = conn.executemany_calls[0]
    assert snapshot_rows[0][-1] is None
