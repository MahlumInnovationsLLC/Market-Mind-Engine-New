"""Unit tests for ``scripts/backfill_v1_archive_leagues.py``.

Task #740. Covers the pure-function pieces of the backfill script that
don't require a live Supabase connection:

* ``_parse_analysis_json`` handles dicts, JSON-encoded strings, bytes,
  and malformed input without crashing.
* ``_classify_row`` runs the inference cascade, builds the audit-key
  string with the original ``orig=...`` suffix, and returns ``None``
  when no rule fires (so the row stays UNRESOLVED rather than getting
  silently re-bucketed).
* Resolved rows (already have a real league) are skipped defensively.

The DB-touching path (``_run_backfill``) is exercised end-to-end by
the manual ``--dry-run`` invocation against the live Supabase archive
documented in ``replit.md``; mocking asyncpg's cursor protocol here
would test the mock more than the script.
"""
from __future__ import annotations

import json

from scripts import backfill_v1_archive_leagues as bf


# --- _parse_analysis_json ---------------------------------------------------


def test_parse_analysis_json_passthrough_dict():
    assert bf._parse_analysis_json({"league": "NBA"}) == {"league": "NBA"}


def test_parse_analysis_json_passthrough_list():
    assert bf._parse_analysis_json(["NBA", "MLB"]) == ["NBA", "MLB"]


def test_parse_analysis_json_decodes_string():
    out = bf._parse_analysis_json('{"league": "NBA"}')
    assert out == {"league": "NBA"}


def test_parse_analysis_json_decodes_bytes():
    out = bf._parse_analysis_json(b'{"league": "EPL"}')
    assert out == {"league": "EPL"}


def test_parse_analysis_json_returns_none_for_malformed():
    # Must NOT raise — a single bad row should not abort the batch.
    assert bf._parse_analysis_json("not valid json") is None
    assert bf._parse_analysis_json(b"\xff\xfe garbage") is None


def test_parse_analysis_json_returns_none_for_none():
    assert bf._parse_analysis_json(None) is None


def test_parse_analysis_json_returns_none_for_unsupported_types():
    assert bf._parse_analysis_json(123) is None
    assert bf._parse_analysis_json(1.5) is None


# --- _classify_row ----------------------------------------------------------


def test_classify_row_skips_resolved_rows():
    # Defensive guard: even if the SELECT filter let a resolved row
    # through (e.g. concurrent writer), classification must not
    # overwrite a real league.
    out = bf._classify_row({
        "id": "abc", "league": "NBA",
        "market_id": "KXMLBGAME-foo", "vertical": "sports", "analysis_json": None,
    })
    assert out is None


def test_classify_row_kalshi_prefix_hit():
    out = bf._classify_row({
        "id": "row-1", "league": "unknown",
        "market_id": "KXNBAGAME-26APR29ORLDET-ORL",
        "vertical": "sports", "analysis_json": None,
    })
    assert out is not None
    row_id, new_league, audit_key = out
    assert row_id == "row-1"
    assert new_league == "NBA"
    assert audit_key == "market_id:kalshi_prefix orig='unknown'"


def test_classify_row_null_league_audit_string():
    out = bf._classify_row({
        "id": "row-2", "league": None,
        "market_id": "KXEPLGAME-foo", "vertical": "soccer",
        "analysis_json": None,
    })
    assert out is not None
    _, league, audit = out
    assert league == "EPL"
    assert audit == "market_id:kalshi_prefix orig=null"


def test_classify_row_via_analysis_json_string():
    # asyncpg may return jsonb as a JSON-encoded string in some setups;
    # the parser must decode it before the inference cascade runs.
    out = bf._classify_row({
        "id": "row-3", "league": "UNKNOWN",
        "market_id": None, "vertical": None,
        "analysis_json": json.dumps({"league": "ATP"}),
    })
    assert out is not None
    _, league, audit = out
    assert league == "ATP"
    assert audit.startswith("analysis_json.league orig=")


def test_classify_row_via_vertical_fallback():
    out = bf._classify_row({
        "id": "row-4", "league": "",
        "market_id": None, "vertical": "elections",
        "analysis_json": None,
    })
    assert out is not None
    _, league, audit = out
    assert league == "ELECTIONS"
    assert audit == "vertical:elections orig=''"


def test_classify_row_returns_none_when_no_rule_fires():
    out = bf._classify_row({
        "id": "row-5", "league": "unknown",
        "market_id": "OTHER-FOO", "vertical": "sports",
        "analysis_json": {"foo": "bar"},
    })
    assert out is None


def test_classify_row_audit_string_lowercases_orig():
    # The original V1 value is normalised to lowercase in the audit
    # string so a future reverse-query can rely on a stable key.
    out = bf._classify_row({
        "id": "row-6", "league": "  UNKNOWN  ",
        "market_id": "KXNFLGAME-foo", "vertical": None, "analysis_json": None,
    })
    assert out is not None
    assert out[2].endswith("orig='unknown'")


# --- Integration test: fixture rows + dry-run path -------------------------
#
# Task #740 contract requires "an integration test that loads a small
# fixture into a temp ``v1_archive.trade_analyses``, runs the backfill
# in dry-run mode, asserts the per-rule counts."  We don't ship a
# disposable Supabase in CI, so we exercise the same code path with a
# minimal in-process asyncpg-shaped fake.  The fake honours: SELECT
# count(...), the cursor() async-iterator protocol with prefetch,
# executemany() over UPDATE statements (with the unresolved-predicate
# guard), the transaction() async-context-manager rollback contract,
# and connect+close.  This covers the same surface area as a real DB
# fixture for the backfill script: classify cascade, batch flushing,
# dry-run rollback, summary aggregation, and idempotence guard.

import asyncio
import re
from collections import OrderedDict
from typing import Any
from unittest.mock import patch


_UPDATE_PREDICATE_RE = re.compile(
    r"upper\(trim\(league\)\) IN", re.IGNORECASE
)


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def __aiter__(self):
        async def gen():
            for r in self._rows:
                yield r
        return gen()


class _FakeTransaction:
    def __init__(self, conn: "_FakeConn"):
        self._conn = conn

    async def __aenter__(self):
        self._conn._snapshot = OrderedDict(
            (k, dict(v)) for k, v in self._conn.rows.items()
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc is not None:
            # Mimic asyncpg behaviour: any exception inside the
            # transaction triggers a ROLLBACK.  We restore the
            # pre-transaction snapshot so the dry-run sentinel
            # exception leaves the table untouched.
            self._conn.rows = self._conn._snapshot
        self._conn._snapshot = None
        return False


class _FakeConn:
    """In-process stand-in for ``asyncpg.Connection``.

    Holds rows in an OrderedDict keyed by id.  Exercises the same
    fetchval/cursor/executemany/transaction surface ``_run_backfill``
    actually calls.
    """
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows: OrderedDict[str, dict[str, Any]] = OrderedDict(
            (r["id"], dict(r)) for r in rows
        )
        self._snapshot: OrderedDict[str, dict[str, Any]] | None = None
        self.executemany_calls: list[tuple[str, list[Any]]] = []

    def _is_unresolved(self, league: Any) -> bool:
        if league is None:
            return True
        if not isinstance(league, str):
            return False
        return league.strip().upper() in {"", "UNKNOWN", "NONE", "NULL"}

    async def fetchval(self, sql: str, *args):
        sql_norm = " ".join(sql.split()).upper()
        if "COUNT(*)" in sql_norm and "WHERE" not in sql_norm:
            return len(self.rows)
        if "COUNT(*)" in sql_norm and "UNKNOWN" in sql_norm:
            return sum(1 for r in self.rows.values()
                       if self._is_unresolved(r.get("league")))
        raise AssertionError(f"unexpected fetchval SQL: {sql!r}")

    def cursor(self, sql: str, *, prefetch: int = 0):
        sql_norm = " ".join(sql.split()).upper()
        assert "SELECT" in sql_norm
        assert "FROM V1_ARCHIVE.TRADE_ANALYSES" in sql_norm
        rows = [
            dict(r) for r in self.rows.values()
            if self._is_unresolved(r.get("league"))
        ]
        rows.sort(key=lambda r: r["id"])
        return _FakeCursor(rows)

    async def executemany(self, sql: str, args_list: list[Any]):
        # Race-safety guard: assert the unresolved predicate is still
        # in the UPDATE statement so a future regression (someone
        # drops the AND clause) is caught here, not in production.
        assert _UPDATE_PREDICATE_RE.search(sql), (
            "race-safety predicate missing from UPDATE WHERE clause"
        )
        self.executemany_calls.append((sql, list(args_list)))
        for row_id, new_league, audit_key in args_list:
            row = self.rows.get(row_id)
            if row is None:
                continue
            # Honour the WHERE-clause unresolved predicate.
            if not self._is_unresolved(row.get("league")):
                continue
            row["league"] = new_league
            row["league_inferred_from"] = audit_key

    def transaction(self):
        return _FakeTransaction(self)

    async def close(self):
        pass


def _make_fixture() -> list[dict[str, Any]]:
    # Hand-curated fixture exercising every rule + edge case.
    return [
        # Rule 1 — analysis_json.kalshi_ticker prefix
        {"id": "f01", "league": "unknown",
         "market_id": None, "vertical": "sports",
         "analysis_json": {"kalshi_ticker": "KXNBAGAME-foo-bar"}},
        # Rule 2 — single-league vertical
        {"id": "f02", "league": "UNKNOWN",
         "market_id": None, "vertical": "commodities",
         "analysis_json": None},
        {"id": "f03", "league": "",
         "market_id": None, "vertical": "elections",
         "analysis_json": None},
        # Rule 3 — analysis_json.league explicit field
        {"id": "f04", "league": None,
         "market_id": None, "vertical": "sports",
         "analysis_json": {"league": "MLB"}},
        # Rule 3 — analysis_json.sport explicit field
        {"id": "f05", "league": "unknown",
         "market_id": None, "vertical": "sports",
         "analysis_json": {"sport": "NHL"}},
        # Rule 4 — top-level market_id Kalshi prefix fallback
        {"id": "f06", "league": "unknown",
         "market_id": "KXEPLGAME-baz-qux", "vertical": "soccer",
         "analysis_json": None},
        {"id": "f07", "league": None,
         "market_id": "KXATPMATCH-xyz", "vertical": None,
         "analysis_json": None},
        # No rule fires — must stay UNRESOLVED
        {"id": "f08", "league": "unknown",
         "market_id": "OTHER-FOO", "vertical": "sports",
         "analysis_json": {"foo": "bar"}},
        {"id": "f09", "league": None,
         "market_id": None, "vertical": None, "analysis_json": None},
        # Already-resolved row — must NOT be touched even if SELECT
        # somehow let it through (defence in depth).
        {"id": "f10", "league": "PGA",
         "market_id": "KXNBAGAME-foo", "vertical": "sports",
         "analysis_json": None},
    ]


def test_integration_dry_run_per_rule_counts():
    """End-to-end: run _run_backfill against a fake DB w/ fixture rows.

    Asserts:
    * Per-rule counts match the hand-curated fixture (rule 1=1,
      rule 2=2, rule 3=2 [league + sport], rule 4=2, residual=2).
    * Per-league counts include every league that should fire.
    * Audit keys carry both the rule name and the original V1 value.
    * Dry-run rollback leaves the table BYTE-FOR-BYTE unchanged.
    * Idempotence guard fires on a second run after backfill.
    """
    fixture = _make_fixture()
    fake = _FakeConn(fixture)
    pre_state = {k: dict(v) for k, v in fake.rows.items()}

    async def _connect_stub(*args, **kwargs):
        return fake

    # ``_run_backfill`` does ``import asyncpg`` inside the function
    # body, so we patch the real module's ``connect`` attribute.
    import asyncpg
    with patch.object(asyncpg, "connect", _connect_stub):
        summary = asyncio.run(
            bf._run_backfill(
                "postgres://test/test",
                dry_run=True, batch_size=3, limit=None,
            )
        )

    # --- per-rule counts -----------------------------------------------
    per_rule = summary["per_rule"]
    assert per_rule["analysis_json.kalshi_ticker:kalshi_prefix"] == 1, per_rule
    assert per_rule["vertical:commodities"] == 1, per_rule
    assert per_rule["vertical:elections"] == 1, per_rule
    assert per_rule["analysis_json.league"] == 1, per_rule
    assert per_rule["analysis_json.sport"] == 1, per_rule
    assert per_rule["market_id:kalshi_prefix"] == 2, per_rule

    # --- per-league counts ---------------------------------------------
    per_league = summary["per_league"]
    assert per_league["NBA"] == 1
    assert per_league["COMMODITIES"] == 1
    assert per_league["ELECTIONS"] == 1
    assert per_league["MLB"] == 1
    assert per_league["NHL"] == 1
    assert per_league["EPL"] == 1
    assert per_league["ATP"] == 1

    # --- aggregate totals ----------------------------------------------
    # 9 unresolved fixture rows in pre-state (f10 is already PGA).
    assert summary["unresolved_before"] == 9
    assert summary["rows_seen"] == 9
    assert summary["rows_updated"] == 7    # f08+f09 stay unresolved
    assert summary["rows_left_unresolved"] == 2
    assert summary["dry_run"] is True

    # --- audit string format on a sampled call -------------------------
    sample_args = fake.executemany_calls[0][1]
    for row_id, new_league, audit_key in sample_args:
        assert " orig=" in audit_key, audit_key
        assert new_league in {"NBA", "COMMODITIES", "ELECTIONS",
                              "MLB", "NHL", "EPL", "ATP"}, new_league

    # --- DRY-RUN ROLLBACK CONTRACT -------------------------------------
    # The transaction must have rolled back, leaving the table
    # byte-for-byte identical to its pre-state.  This is the SINGLE
    # most important integration assertion: it proves the
    # _DryRunRollback sentinel is wired correctly.
    assert fake.rows == pre_state, (
        "dry-run did not roll back: rows mutated in-place"
    )

    # --- IDEMPOTENCE GUARD ---------------------------------------------
    # Force-resolve every fixture row, then re-run: the script must
    # refuse to walk the cursor when unresolved_before==0.
    for r in fake.rows.values():
        if fake._is_unresolved(r.get("league")):
            r["league"] = "NBA"  # arbitrary resolved value
            r["league_inferred_from"] = "manual:test_resolution"

    fake.executemany_calls.clear()
    with patch.object(asyncpg, "connect", _connect_stub):
        guard_summary = asyncio.run(
            bf._run_backfill(
                "postgres://test/test",
                dry_run=False, batch_size=10, limit=None,
            )
        )
    assert guard_summary.get("refused_no_unresolved") is True
    assert guard_summary["rows_seen"] == 0
    assert guard_summary["rows_updated"] == 0
    # Critical: NO updates were issued — we never even opened a cursor.
    assert fake.executemany_calls == []
