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
    assert audit_key == "kalshi_prefix:market_id orig='unknown'"


def test_classify_row_null_league_audit_string():
    out = bf._classify_row({
        "id": "row-2", "league": None,
        "market_id": "KXEPLGAME-foo", "vertical": "soccer",
        "analysis_json": None,
    })
    assert out is not None
    _, league, audit = out
    assert league == "EPL"
    assert audit == "kalshi_prefix:market_id orig=null"


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
