"""Tests for ``app.archive_helper`` (task #757).

Exercises the streaming archive-then-delete primitive end-to-end with
in-memory fakes for the row fetcher, deleter, and uploader so the
test never touches Postgres or Supabase Storage.

Verifies:
  * Rows are encoded as gzip JSONL with one row per line and the
    matching uncompressed size.
  * ``datetime``, ``Decimal``, and raw ``bytes`` columns survive a
    JSON round-trip via the ``_json_default`` shim.
  * The helper writes one ``part-<n>.jsonl.gz`` plus a sibling
    ``part-<n>.manifest.json`` per chunk under
    ``archives/<table>/dt=YYYY-MM-DD/``.
  * Each chunk's upload happens before the matching delete (no rows
    are lost if the upload fails).
  * ``max_rows`` and ``chunk_size`` interact correctly when the
    table has more rows than the budget.
  * Identifier quoting handles the reserved-word
    ``"timestamp"`` column.
"""
from __future__ import annotations

import asyncio
import gzip
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from app import archive_helper


# ---------------------------------------------------------------------------
# encode_chunk
# ---------------------------------------------------------------------------

def test_encode_chunk_round_trips_jsonl():
    rows = [
        {"id": 1, "label": "a"},
        {"id": 2, "label": "b"},
        {"id": 3, "label": "c"},
    ]
    compressed, uncompressed = archive_helper.encode_chunk(rows)
    decoded = gzip.decompress(compressed).decode("utf-8")
    lines = [json.loads(line) for line in decoded.splitlines()]
    assert lines == rows
    assert uncompressed == len(decoded.encode("utf-8"))


def test_encode_chunk_handles_datetime_decimal_bytes():
    rows = [
        {
            "id": 1,
            "ts": datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc),
            "d": date(2026, 4, 22),
            "amount": Decimal("3.14"),
            "blob": b"\x00\x01\x02",
        }
    ]
    compressed, _ = archive_helper.encode_chunk(rows)
    line = gzip.decompress(compressed).decode("utf-8").splitlines()[0]
    parsed = json.loads(line)
    assert parsed["ts"] == "2026-04-22T12:30:00+00:00"
    assert parsed["d"] == "2026-04-22"
    assert parsed["amount"] == "3.14"
    # Raw bytes are wrapped so the round-trip is lossless.
    assert parsed["blob"] == {"__bytes_b64__": "AAEC"}


# ---------------------------------------------------------------------------
# archive_table — happy path with fakes
# ---------------------------------------------------------------------------

class _FakeTable:
    """Minimal in-memory stand-in for one table."""

    def __init__(self, rows: list[dict[str, Any]]):
        # Sort by ts ASC so the fetcher returns oldest-first like
        # ``ORDER BY ts ASC`` would.
        self.rows = sorted(rows, key=lambda r: r["ts"])

    def make_fetcher(self, cutoff: datetime, ts_column: str):
        async def fetch(limit: int):
            eligible = [r for r in self.rows if r[ts_column] < cutoff]
            return eligible[:limit]

        return fetch

    def make_deleter(self, pk_columns):
        async def delete(pks):
            wanted = {tuple(pk) if isinstance(pk, tuple) else (pk,) for pk in pks}
            before = len(self.rows)
            self.rows = [
                r for r in self.rows
                if tuple(r[c] for c in pk_columns) not in wanted
            ]
            return before - len(self.rows)

        return delete


class _FakeBucket:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def make_uploader(self):
        async def upload(key: str, payload: bytes):
            self.objects[key] = payload
            return {"key": key, "size": len(payload)}
        return upload


def test_archive_table_streams_chunks_and_writes_manifests():
    table = _FakeTable(
        [{"id": i, "ts": datetime(2026, 1, 1, tzinfo=timezone.utc)}
         for i in range(7)]
    )
    bucket = _FakeBucket()
    cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)

    summary = asyncio.run(
        archive_helper.archive_table(
            table="t",
            ts_column="ts",
            cutoff=cutoff,
            pk_columns=("id",),
            chunk_size=3,
            max_rows=None,
            fetcher=table.make_fetcher(cutoff, "ts"),
            deleter=table.make_deleter(("id",)),
            uploader=bucket.make_uploader(),
            manifest_writer=bucket.make_uploader(),
            now=lambda: datetime(2026, 4, 22, tzinfo=timezone.utc),
        )
    )

    # 7 rows, chunk_size=3 -> 3 chunks (3 + 3 + 1).
    assert summary.error is None
    assert summary.rows == 7
    assert summary.chunks == 3
    # Three pairs (data + manifest) written to the bucket.
    keys = sorted(bucket.objects.keys())
    assert len(keys) == 6
    for i in range(3):
        assert f"archives/t/dt=2026-04-22/part-{i:04d}.jsonl.gz" in bucket.objects
        manifest_key = f"archives/t/dt=2026-04-22/part-{i:04d}.manifest.json"
        assert manifest_key in bucket.objects
        manifest = json.loads(bucket.objects[manifest_key])
        assert manifest["table"] == "t"
        assert manifest["ts_column"] == "ts"
        assert manifest["pk_columns"] == ["id"]
        assert manifest["chunk_index"] == i
        assert manifest["cutoff"] == cutoff.isoformat()
    # Source table is empty afterwards.
    assert table.rows == []


def test_archive_table_respects_max_rows_budget():
    table = _FakeTable(
        [{"id": i, "ts": datetime(2026, 1, 1, tzinfo=timezone.utc)}
         for i in range(50)]
    )
    bucket = _FakeBucket()
    cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)

    summary = asyncio.run(
        archive_helper.archive_table(
            table="t",
            ts_column="ts",
            cutoff=cutoff,
            pk_columns=("id",),
            chunk_size=10,
            max_rows=15,  # cap below table size
            fetcher=table.make_fetcher(cutoff, "ts"),
            deleter=table.make_deleter(("id",)),
            uploader=bucket.make_uploader(),
            manifest_writer=bucket.make_uploader(),
        )
    )
    # 15-row budget at chunk_size=10 -> first chunk 10, second chunk 5.
    assert summary.rows == 15
    assert summary.chunks == 2
    assert len(table.rows) == 50 - 15  # remainder still pending


def test_archive_table_upload_failure_aborts_without_data_loss():
    table = _FakeTable(
        [{"id": i, "ts": datetime(2026, 1, 1, tzinfo=timezone.utc)}
         for i in range(5)]
    )
    cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)

    async def boom_uploader(_key, _payload):
        raise RuntimeError("bucket down")

    summary = asyncio.run(
        archive_helper.archive_table(
            table="t",
            ts_column="ts",
            cutoff=cutoff,
            pk_columns=("id",),
            chunk_size=10,
            fetcher=table.make_fetcher(cutoff, "ts"),
            deleter=table.make_deleter(("id",)),
            uploader=boom_uploader,
            manifest_writer=boom_uploader,
        )
    )
    assert summary.error and "bucket down" in summary.error
    assert summary.rows == 0
    assert len(table.rows) == 5  # nothing deleted because upload failed


def test_archive_table_pk_extraction_uses_unquoted_name():
    """``"timestamp"`` must come back as ``timestamp`` for row lookups."""
    rows = [
        {"id": 1, "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"id": 2, "timestamp": datetime(2026, 1, 2, tzinfo=timezone.utc)},
    ]
    pks = [
        archive_helper._extract_pk(r, ("id", '"timestamp"')) for r in rows
    ]
    assert pks[0] == (1, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert pks[1] == (2, datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_quote_ident_handles_reserved_words_and_safe_idents():
    # Plain identifier: emitted as-is.
    assert archive_helper._quote_ident("created_at") == "created_at"
    # Reserved word: must be double-quoted to survive ORDER BY.
    assert archive_helper._quote_ident("timestamp") == '"timestamp"'
    # Already-quoted input is left as a single quoted form.
    assert archive_helper._quote_ident('"timestamp"') == '"""timestamp"""'
