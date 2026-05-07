"""Smoke tests for ``scripts/restore_archived_chunk.py`` (task #760).

End-to-end JSONL round-trip + on-conflict-do-nothing path are exercised
against in-memory fakes for the bucket downloader, column typer, and
row inserter — no Postgres or Supabase Storage required.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# Allow importing the script as a module.
_API_SERVER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_API_SERVER_ROOT))

from app import archive_helper  # noqa: E402
from scripts import restore_archived_chunk as cli  # noqa: E402


# ---------------------------------------------------------------------------
# Decode helpers
# ---------------------------------------------------------------------------

def test_decode_jsonl_gz_round_trips_with_archive_helper():
    rows = [
        {"id": 1, "label": "a"},
        {"id": 2, "label": "b"},
    ]
    compressed, _ = archive_helper.encode_chunk(rows)
    decoded = cli.decode_jsonl_gz(compressed)
    assert decoded == rows


def test_decode_row_unwraps_bytes_envelope():
    raw = {"id": 1, "blob": {"__bytes_b64__": "AAEC"}}
    decoded = cli.decode_row(raw)
    assert decoded == {"id": 1, "blob": b"\x00\x01\x02"}


def test_decode_row_parses_typed_columns():
    raw = {
        "id": 1,
        "ts": "2026-04-22T12:30:00+00:00",
        "d": "2026-04-22",
        "amount": "3.14",
        "blob": {"__bytes_b64__": "AAEC"},
    }
    types = {
        "id": "bigint",
        "ts": "timestamp with time zone",
        "d": "date",
        "amount": "numeric",
        "blob": "bytea",
    }
    decoded = cli.decode_row(raw, column_types=types)
    assert decoded["ts"] == datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    assert decoded["d"] == date(2026, 4, 22)
    assert decoded["amount"] == Decimal("3.14")
    assert decoded["blob"] == b"\x00\x01\x02"
    assert decoded["id"] == 1


def test_decode_round_trip_via_archive_helper_preserves_native_types():
    """The full encode → decode loop reproduces datetime/Decimal/bytes."""
    original = [
        {
            "id": 7,
            "ts": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            "amount": Decimal("9.875"),
            "blob": b"\xde\xad\xbe\xef",
        }
    ]
    compressed, _ = archive_helper.encode_chunk(original)
    raw_rows = cli.decode_jsonl_gz(compressed)
    types = {
        "id": "bigint",
        "ts": "timestamp with time zone",
        "amount": "numeric",
        "blob": "bytea",
    }
    decoded = [cli.decode_row(r, column_types=types) for r in raw_rows]
    assert decoded == original


# ---------------------------------------------------------------------------
# Object-key helpers
# ---------------------------------------------------------------------------

def test_infer_table_from_key_canonical_layout():
    key = "archives/predictions/dt=2026-04-22/part-0000.jsonl.gz"
    assert cli.infer_table_from_key(key) == "predictions"


def test_infer_table_from_key_returns_none_for_off_layout():
    assert cli.infer_table_from_key("path/to/part-0000.jsonl.gz") is None


def test_resolve_table_uses_explicit_arg_over_inference():
    args = type("A", (), {"table": "override"})()
    keys = ["archives/predictions/dt=2026-04-22/part-0000.jsonl.gz"]
    assert cli._resolve_table(args, keys) == "override"


def test_resolve_table_infers_when_arg_omitted():
    args = type("A", (), {"table": None})()
    keys = ["archives/predictions/dt=2026-04-22/part-0000.jsonl.gz"]
    assert cli._resolve_table(args, keys) == "predictions"


def test_resolve_table_errors_when_keys_span_tables():
    args = type("A", (), {"table": None})()
    keys = [
        "archives/predictions/dt=2026-04-22/part-0000.jsonl.gz",
        "archives/pipeline_events/dt=2026-04-22/part-0000.jsonl.gz",
    ]
    with pytest.raises(SystemExit) as exc:
        cli._resolve_table(args, keys)
    assert "multiple tables" in str(exc.value)


# ---------------------------------------------------------------------------
# Restore driver — happy path + on-conflict + dry-run
# ---------------------------------------------------------------------------

class _FakeBucket:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def make_downloader(self):
        async def download(key: str) -> bytes:
            if key not in self.objects:
                raise FileNotFoundError(key)
            return self.objects[key]
        return download


class _FakeTable:
    """In-memory destination table with a unique-key (id) constraint."""

    def __init__(self, columns: dict[str, str]):
        self.columns = columns
        self.rows: list[dict[str, Any]] = []
        self.calls: list[tuple[str, list[str], list[tuple[Any, ...]]]] = []

    def make_typer(self):
        async def typer(_table: str) -> dict[str, str]:
            return dict(self.columns)
        return typer

    def make_inserter(self):
        async def insert(table, columns, values):
            self.calls.append((table, list(columns), list(values)))
            inserted = 0
            for row_values in values:
                row = dict(zip(columns, row_values))
                # Treat ``id`` as the unique key — skip on conflict.
                if any(r.get("id") == row.get("id") for r in self.rows):
                    continue
                self.rows.append(row)
                inserted += 1
            return inserted
        return insert


def _make_chunk(rows):
    payload, _ = archive_helper.encode_chunk(rows)
    return payload


def test_restore_one_object_inserts_rows_when_apply():
    rows = [{"id": 1, "label": "a"}, {"id": 2, "label": "b"}]
    bucket = _FakeBucket({
        "archives/t/dt=2026-04-22/part-0000.jsonl.gz": _make_chunk(rows),
    })
    dest = _FakeTable({"id": "bigint", "label": "text"})

    result = asyncio.run(cli.restore_one_object(
        object_key="archives/t/dt=2026-04-22/part-0000.jsonl.gz",
        table="t",
        apply=True,
        column_typer=dest.make_typer(),
        downloader=bucket.make_downloader(),
        inserter=dest.make_inserter(),
    ))

    assert result.error is None
    assert result.rows_in_chunk == 2
    assert result.rows_inserted == 2
    assert result.rows_skipped == 0
    assert result.applied is True
    assert dest.rows == rows


def test_restore_one_object_dry_run_skips_inserts():
    rows = [{"id": 1, "label": "a"}]
    bucket = _FakeBucket({
        "archives/t/dt=2026-04-22/part-0000.jsonl.gz": _make_chunk(rows),
    })
    dest = _FakeTable({"id": "bigint", "label": "text"})

    result = asyncio.run(cli.restore_one_object(
        object_key="archives/t/dt=2026-04-22/part-0000.jsonl.gz",
        table="t",
        apply=False,
        column_typer=dest.make_typer(),
        downloader=bucket.make_downloader(),
        inserter=dest.make_inserter(),
    ))

    assert result.error is None
    assert result.rows_in_chunk == 1
    assert result.rows_inserted == 0
    assert result.rows_skipped == 0
    assert result.applied is False
    # No insert was issued at all.
    assert dest.calls == []
    assert dest.rows == []


def test_restore_one_object_on_conflict_do_nothing():
    rows = [{"id": 1, "label": "a"}, {"id": 2, "label": "b"}]
    bucket = _FakeBucket({
        "archives/t/dt=2026-04-22/part-0000.jsonl.gz": _make_chunk(rows),
    })
    dest = _FakeTable({"id": "bigint", "label": "text"})
    # Pre-seed id=1 so re-restoring should skip it.
    dest.rows.append({"id": 1, "label": "stale"})

    result = asyncio.run(cli.restore_one_object(
        object_key="archives/t/dt=2026-04-22/part-0000.jsonl.gz",
        table="t",
        apply=True,
        column_typer=dest.make_typer(),
        downloader=bucket.make_downloader(),
        inserter=dest.make_inserter(),
    ))

    assert result.error is None
    assert result.rows_in_chunk == 2
    assert result.rows_inserted == 1  # only id=2 was new
    assert result.rows_skipped == 1
    # Pre-existing row is NOT overwritten — that's the point of
    # ON CONFLICT DO NOTHING vs DO UPDATE.
    seeded = next(r for r in dest.rows if r["id"] == 1)
    assert seeded["label"] == "stale"


def test_restore_one_object_rejects_unknown_columns():
    rows = [{"id": 1, "ghost_col": "x"}]
    bucket = _FakeBucket({
        "archives/t/dt=2026-04-22/part-0000.jsonl.gz": _make_chunk(rows),
    })
    dest = _FakeTable({"id": "bigint"})

    result = asyncio.run(cli.restore_one_object(
        object_key="archives/t/dt=2026-04-22/part-0000.jsonl.gz",
        table="t",
        apply=True,
        column_typer=dest.make_typer(),
        downloader=bucket.make_downloader(),
        inserter=dest.make_inserter(),
    ))
    assert result.error and "ghost_col" in result.error
    assert dest.rows == []


def test_restore_objects_aggregates_summary_across_chunks():
    chunk_a = _make_chunk([{"id": 1, "label": "a"}])
    chunk_b = _make_chunk([{"id": 2, "label": "b"}, {"id": 3, "label": "c"}])
    bucket = _FakeBucket({
        "archives/t/dt=2026-04-22/part-0000.jsonl.gz": chunk_a,
        "archives/t/dt=2026-04-22/part-0001.jsonl.gz": chunk_b,
    })
    dest = _FakeTable({"id": "bigint", "label": "text"})

    summary = asyncio.run(cli.restore_objects(
        object_keys=sorted(bucket.objects.keys()),
        table="t",
        apply=True,
        column_typer=dest.make_typer(),
        downloader=bucket.make_downloader(),
        inserter=dest.make_inserter(),
    ))

    assert summary.total_rows == 3
    assert summary.total_inserted == 3
    assert summary.total_skipped == 0
    assert summary.n_failed == 0
    assert len(summary.objects) == 2


# ---------------------------------------------------------------------------
# Argparse plumbing
# ---------------------------------------------------------------------------

def test_argparse_dry_run_and_apply_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli._parse_args(["--object-key", "x", "--apply", "--dry-run"])


def test_resolve_object_keys_requires_one_source():
    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli._resolve_object_keys(object_key=None, prefix=None))
    assert "required" in str(exc.value)


def test_resolve_object_keys_rejects_both_sources():
    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli._resolve_object_keys(
            object_key="a", prefix="b",
        ))
    assert "mutually exclusive" in str(exc.value)


def test_resolve_object_keys_single_object():
    keys = asyncio.run(cli._resolve_object_keys(
        object_key="archives/t/dt=2026-04-22/part-0000.jsonl.gz",
        prefix=None,
    ))
    assert keys == ["archives/t/dt=2026-04-22/part-0000.jsonl.gz"]


def test_resolve_object_keys_prefix_uses_lister():
    async def fake_lister(prefix: str):
        assert prefix == "archives/t/dt=2026-04-22/"
        return [
            "archives/t/dt=2026-04-22/part-0001.jsonl.gz",
            "archives/t/dt=2026-04-22/part-0000.jsonl.gz",
        ]
    keys = asyncio.run(cli._resolve_object_keys(
        object_key=None,
        prefix="archives/t/dt=2026-04-22/",
        lister=fake_lister,
    ))
    assert keys == [
        "archives/t/dt=2026-04-22/part-0001.jsonl.gz",
        "archives/t/dt=2026-04-22/part-0000.jsonl.gz",
    ]
