"""Unit tests for the admin "Early-access leads" endpoints (task #125).

Stubs the asyncpg layer at module scope and exercises the SQL-builder /
filter / paging code paths without touching a real Postgres.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import dashboard


class FakeDB:
    """Records every fetch/fetchval/execute and serves canned rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return list(self.rows)

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.fetchval_calls.append((query, args))
        return len(self.rows)

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "OK"


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    fake = FakeDB(
        rows=[
            {
                "id": 2,
                "email": "alice@example.com",
                "interest": "sports",
                "source": "landing",
                "created_at": "2026-04-23T12:00:00Z",
                "confirmation_sent_at": "2026-04-23T12:00:01Z",
            },
            {
                "id": 1,
                "email": "bob@example.com",
                "interest": "elections",
                "source": "landing",
                "created_at": "2026-04-22T10:00:00Z",
                "confirmation_sent_at": None,
            },
        ]
    )
    dashboard._LEADS_TABLE_READY = False
    monkeypatch.setattr(dashboard.db, "fetch", fake.fetch)
    monkeypatch.setattr(dashboard.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(dashboard.db, "execute", fake.execute)
    return fake


def test_filter_builder_no_filters() -> None:
    sql, params = dashboard._build_landing_leads_filter(
        interest=None, since=None, until=None, search=None
    )
    assert sql == ""
    assert params == []


def test_filter_builder_combines_filters() -> None:
    sql, params = dashboard._build_landing_leads_filter(
        interest="Sports",
        since="2026-04-01",
        until="2026-04-30",
        search="alice",
    )
    assert sql.startswith("WHERE")
    assert "lower(coalesce(interest, '')) = lower($1)" in sql
    assert "created_at >= $2::timestamptz" in sql
    assert "created_at < $3::timestamptz" in sql
    assert "lower(email) LIKE $4" in sql
    assert params == ["Sports", "2026-04-01", "2026-04-30", "%alice%"]


def test_filter_builder_none_sentinel() -> None:
    sql, params = dashboard._build_landing_leads_filter(
        interest="__none__", since=None, until=None, search=None
    )
    assert "interest IS NULL" in sql
    assert params == []


def test_list_landing_leads_returns_paged_envelope(fake_db: FakeDB) -> None:
    result = asyncio.run(dashboard.list_landing_leads(limit=10, offset=0))
    assert result["limit"] == 10
    assert result["offset"] == 0
    assert result["total"] == 2
    assert [r["email"] for r in result["items"]] == [
        "alice@example.com",
        "bob@example.com",
    ]
    list_query, args = fake_db.fetch_calls[0]
    assert "FROM landing_leads" in list_query
    assert args[-2:] == (10, 0)


def test_list_landing_leads_clamps_limit(fake_db: FakeDB) -> None:
    result = asyncio.run(dashboard.list_landing_leads(limit=10_000, offset=-5))
    assert result["limit"] == 500
    assert result["offset"] == 0


def test_list_landing_leads_passes_filters(fake_db: FakeDB) -> None:
    asyncio.run(
        dashboard.list_landing_leads(
            interest="sports",
            since="2026-04-01",
            until="2026-04-30",
            search="ali",
            limit=25,
            offset=50,
        )
    )
    list_query, args = fake_db.fetch_calls[0]
    assert args == ("sports", "2026-04-01", "2026-04-30", "%ali%", 25, 50)
    count_query, count_args = fake_db.fetchval_calls[0]
    assert "count(*)" in count_query
    assert count_args == ("sports", "2026-04-01", "2026-04-30", "%ali%")


def test_list_landing_lead_interests(fake_db: FakeDB) -> None:
    fake_db.rows = [{"interest": "elections"}, {"interest": "sports"}]
    out = asyncio.run(dashboard.list_landing_lead_interests())
    assert out == ["elections", "sports"]


def test_export_landing_leads_csv_quotes_correctly(fake_db: FakeDB) -> None:
    fake_db.rows = [
        {
            "id": 7,
            "email": 'weird,"name"@example.com',
            "interest": "sports, NFL",
            "source": "landing",
            "created_at": "2026-04-23T12:00:00Z",
            "confirmation_sent_at": "",
        }
    ]
    csv_text = asyncio.run(dashboard.export_landing_leads_csv(limit=10))
    lines = csv_text.strip().splitlines()
    assert lines[0] == "id,email,interest,source,created_at,confirmation_sent_at"
    assert lines[1].startswith("7,")
    assert '"weird,""name""@example.com"' in lines[1]
    assert '"sports, NFL"' in lines[1]


def test_list_landing_leads_swallows_db_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("db is on fire")

    dashboard._LEADS_TABLE_READY = True  # skip the CREATE TABLE call
    monkeypatch.setattr(dashboard.db, "fetch", boom)
    monkeypatch.setattr(dashboard.db, "fetchval", boom)
    result = asyncio.run(dashboard.list_landing_leads())
    assert result["items"] == []
    assert result["total"] == 0
