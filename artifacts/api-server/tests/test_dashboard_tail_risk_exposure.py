"""Tests for the tail-risk exposure read-model + public route (task #744)."""
from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import dashboard
from app.main import app


def _stub_fetch(
    monkeypatch,
    rows: list[dict[str, Any]],
    tail_max_loss: float = 0.0,
) -> dict[str, Any]:
    """Stub db.fetch/fetchval; concatenates every query into cap['sql']."""
    captured: dict[str, Any] = {
        "sql": "",
        "args": None,
        "tail_sql": None,
        "all_sqls": [],
    }

    async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        captured["all_sqls"].append(query)
        captured["sql"] = "\n".join(captured["all_sqls"])
        captured["args"] = args
        return rows

    async def fake_fetchval(query: str, *args: Any) -> float:
        captured["tail_sql"] = query
        return float(tail_max_loss)

    monkeypatch.setattr(dashboard.db, "fetch", fake_fetch)
    monkeypatch.setattr(dashboard.db, "fetchval", fake_fetchval)
    return captured


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class TestEmptyAndZeroState:
    def test_empty_db_yields_seven_buckets_with_zeros(self, monkeypatch):
        _stub_fetch(monkeypatch, [])
        out = _run(dashboard.get_tail_risk_exposure(days=30))

        assert len(out["buckets"]) == 7
        labels = [b["label"] for b in out["buckets"]]
        assert labels == [
            "<$0.10",
            "$0.10–$0.25",
            "$0.25–$0.40",
            "$0.40–$0.60",
            "$0.60–$0.75",
            "$0.75–$0.90",
            "$0.90+",
        ]
        for b in out["buckets"]:
            assert b["rows"] == []
            assert b["totals"] == {
                "open_count": 0,
                "open_cost_usd": 0.0,
                "open_max_loss_usd": 0.0,
                "realized_count": 0,
                "realized_wins": 0,
                "realized_pnl_usd": 0.0,
                "ev_per_trade": None,
            }
            assert "is_tail" in b
        assert out["buckets"][0]["is_tail"] is True
        assert out["buckets"][1]["is_tail"] is False
        assert out["buckets"][6]["is_tail"] is False

        assert out["totals"]["open_count"] == 0
        assert out["totals"]["open_max_loss_usd"] == 0.0
        assert out["tail_concentration"]["pct"] is None
        assert out["tail_concentration"]["tone"] == "empty"
        assert out["tail_concentration"]["tail_max_loss_usd"] == 0.0
        assert out["tail_concentration"]["total_max_loss_usd"] == 0.0

    def test_db_failure_returns_empty_matrix_not_500(self, monkeypatch):
        async def boom(*_a, **_kw):
            raise RuntimeError("relation \"positions\" does not exist")

        monkeypatch.setattr(dashboard.db, "fetch", boom)
        out = _run(dashboard.get_tail_risk_exposure(days=30))

        assert len(out["buckets"]) == 7
        assert out["totals"]["open_count"] == 0
        assert out["tail_concentration"]["tone"] == "empty"

    def test_clamps_days_to_1_365(self, monkeypatch):
        _stub_fetch(monkeypatch, [])
        out_lo = _run(dashboard.get_tail_risk_exposure(days=0))
        out_hi = _run(dashboard.get_tail_risk_exposure(days=10_000))
        assert out_lo["days"] == 1
        assert out_hi["days"] == 365

    def test_buckets_omit_sentinel_upper_bound(self, monkeypatch):
        _stub_fetch(monkeypatch, [])
        out = _run(dashboard.get_tail_risk_exposure(days=30))

        last = out["buckets"][-1]
        assert last["label"] == "$0.90+"
        assert last["lo"] == 0.90
        assert last["hi"] == 1.0


class TestRollupArithmetic:
    """Bucket → totals → grand total → concentration arithmetic."""

    def test_rows_aggregate_into_per_bucket_and_grand_totals(self, monkeypatch):
        _stub_fetch(
            monkeypatch,
            [
                {
                    "bucket_idx": 0, "vertical": "sports",
                    "open_count": 2, "open_cost_usd": 800.0,
                    "open_max_loss_usd": 800.0,
                    "realized_count": 5, "realized_wins": 1,
                    "realized_pnl_usd": -1500.0,
                },
                {
                    "bucket_idx": 0, "vertical": "crypto",
                    "open_count": 1, "open_cost_usd": 50.0,
                    "open_max_loss_usd": 50.0,
                    "realized_count": 0, "realized_wins": 0,
                    "realized_pnl_usd": 0.0,
                },
                {
                    "bucket_idx": 5, "vertical": "sports",
                    "open_count": 4, "open_cost_usd": 320.0,
                    "open_max_loss_usd": 320.0,
                    "realized_count": 12, "realized_wins": 8,
                    "realized_pnl_usd": 175.0,
                },
            ],
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))

        b0 = out["buckets"][0]
        assert b0["label"] == "<$0.10"
        assert len(b0["rows"]) == 2
        assert b0["totals"]["open_count"] == 3
        assert b0["totals"]["open_cost_usd"] == pytest.approx(850.0)
        assert b0["totals"]["realized_count"] == 5
        assert b0["totals"]["realized_pnl_usd"] == pytest.approx(-1500.0)
        assert b0["totals"]["ev_per_trade"] == pytest.approx(-300.0)
        sports_cell = next(r for r in b0["rows"] if r["vertical"] == "sports")
        crypto_cell = next(r for r in b0["rows"] if r["vertical"] == "crypto")
        assert sports_cell["ev_per_trade"] == pytest.approx(-300.0)
        assert crypto_cell["ev_per_trade"] is None

        b5 = out["buckets"][5]
        assert b5["label"] == "$0.75–$0.90"
        assert len(b5["rows"]) == 1
        assert b5["totals"]["ev_per_trade"] == pytest.approx(175.0 / 12.0)

        assert out["buckets"][3]["totals"]["open_count"] == 0
        assert out["buckets"][3]["totals"]["ev_per_trade"] is None

        assert out["totals"]["open_count"] == 7
        assert out["totals"]["open_cost_usd"] == pytest.approx(1170.0)
        assert out["totals"]["realized_count"] == 17
        assert out["totals"]["realized_wins"] == 9
        assert out["totals"]["realized_pnl_usd"] == pytest.approx(-1325.0)
        assert out["totals"]["ev_per_trade"] == pytest.approx(-1325.0 / 17.0)

    def test_unknown_bucket_idx_silently_dropped(self, monkeypatch):
        _stub_fetch(
            monkeypatch,
            [
                {
                    "bucket_idx": 99, "vertical": "sports",
                    "open_count": 1, "open_cost_usd": 10.0,
                    "open_max_loss_usd": 10.0,
                    "realized_count": 0, "realized_wins": 0,
                    "realized_pnl_usd": 0.0,
                },
            ],
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))
        assert out["totals"]["open_count"] == 0


class TestTailConcentrationHeadline:

    def _row(self, idx: int, max_loss: float) -> dict[str, Any]:
        return {
            "bucket_idx": idx, "vertical": "sports",
            "open_count": 1, "open_cost_usd": max_loss,
            "open_max_loss_usd": max_loss,
            "realized_count": 0, "realized_wins": 0,
            "realized_pnl_usd": 0.0,
        }

    def test_red_when_tail_share_at_or_above_30_pct(self, monkeypatch):
        _stub_fetch(
            monkeypatch,
            [
                self._row(0, 300.0),
                self._row(3, 700.0),
            ],
            tail_max_loss=300.0,
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))
        c = out["tail_concentration"]
        assert c["tail_max_loss_usd"] == pytest.approx(300.0)
        assert c["total_max_loss_usd"] == pytest.approx(1000.0)
        assert c["pct"] == pytest.approx(0.30)
        assert c["tone"] == "red"
        assert c["threshold_price"] == 0.20

    def test_amber_in_the_15_to_30_band(self, monkeypatch):
        _stub_fetch(
            monkeypatch,
            [
                self._row(1, 150.0),
                self._row(4, 850.0),
            ],
            tail_max_loss=150.0,
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))
        c = out["tail_concentration"]
        assert c["pct"] == pytest.approx(0.15)
        assert c["tone"] == "amber"

    def test_green_below_15_pct(self, monkeypatch):
        _stub_fetch(
            monkeypatch,
            [
                self._row(0, 50.0),
                self._row(5, 950.0),
            ],
            tail_max_loss=50.0,
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))
        c = out["tail_concentration"]
        assert c["pct"] == pytest.approx(0.05)
        assert c["tone"] == "green"

    def test_tail_excludes_above_threshold_buckets(self, monkeypatch):
        _stub_fetch(
            monkeypatch,
            [
                self._row(2, 600.0),
                self._row(4, 400.0),
            ],
            tail_max_loss=0.0,
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))
        assert out["tail_concentration"]["tail_max_loss_usd"] == 0.0
        assert out["tail_concentration"]["pct"] == pytest.approx(0.0)
        assert out["tail_concentration"]["tone"] == "green"

    def test_tail_uses_row_level_threshold_not_bucket_boundaries(
        self, monkeypatch
    ):
        _stub_fetch(
            monkeypatch,
            [
                self._row(1, 200.0),
                self._row(4, 800.0),
            ],
            tail_max_loss=80.0,
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))
        c = out["tail_concentration"]
        assert c["tail_max_loss_usd"] == pytest.approx(80.0)
        assert c["pct"] == pytest.approx(0.08)
        assert c["tone"] == "green"


class TestFilterSplicing:

    def test_no_filter_passes_no_fragments(self, monkeypatch):
        cap = _stub_fetch(monkeypatch, [])
        _run(dashboard.get_tail_risk_exposure(days=30))
        sql = cap["sql"]
        assert "placed_by" not in sql
        assert "vertical IN" not in sql

    def test_placed_by_system_inserts_predicate(self, monkeypatch):
        cap = _stub_fetch(monkeypatch, [])
        _run(dashboard.get_tail_risk_exposure(days=30, placed_by="system"))
        sql = cap["sql"]
        assert "p.placed_by IN ('orchestrator','autotrader','swarm','operator_recovery')" in sql
        assert "t.placed_by IN ('orchestrator','autotrader','swarm','operator_recovery')" in sql

    def test_vertical_filter_inserts_predicate(self, monkeypatch):
        cap = _stub_fetch(monkeypatch, [])
        _run(
            dashboard.get_tail_risk_exposure(
                days=30, placed_by="all", verticals="sports"
            )
        )
        sql = cap["sql"]
        assert "p.vertical IN ('sports')" in sql
        assert "t.vertical IN ('sports')" in sql

    def test_unknown_vertical_raises_valueerror(self, monkeypatch):
        with pytest.raises(ValueError, match="unknown vertical"):
            _run(
                dashboard.get_tail_risk_exposure(
                    days=30, verticals="horseracing"
                )
            )


def _stub_aggregator(monkeypatch, captured: list[dict[str, Any]]) -> None:
    async def _fake(
        days: int = 30,
        placed_by: str | None = None,
        verticals: object = None,
    ) -> dict[str, Any]:
        captured.append(
            {
                "days": days,
                "placed_by": placed_by,
                "verticals": verticals,
            }
        )
        return {
            "days": days,
            "buckets": [],
            "totals": dashboard._zero_tail_totals(),
            "tail_concentration": {
                "tail_max_loss_usd": 0.0,
                "total_max_loss_usd": 0.0,
                "pct": None,
                "tone": "empty",
                "threshold_price": dashboard._TAIL_PRICE_THRESHOLD,
            },
            "source": "positions",
        }

    monkeypatch.setattr(dashboard, "get_tail_risk_exposure", _fake)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize(
    "qs,expected_pb",
    [
        ("?days=30", "all"),
        ("?days=30&placed_by=manual", "manual"),
        ("?days=30&placed_by=system", "system"),
        ("?days=30&placed_by=all", "all"),
    ],
)
def test_route_forwards_placed_by_and_echoes_in_response(
    client, monkeypatch, qs, expected_pb
):
    captured: list[dict[str, Any]] = []
    _stub_aggregator(monkeypatch, captured)

    resp = client.get(f"/api/public/tail-risk-exposure{qs}")
    assert resp.status_code == 200, resp.text
    assert captured[0]["placed_by"] == expected_pb
    assert captured[0]["days"] == 30
    body = resp.json()
    assert body["placed_by"] == expected_pb
    assert body["verticals"] == []


@pytest.mark.parametrize(
    "qs,expected_arg,expected_echo",
    [
        ("?days=30", None, []),
        ("?days=30&vertical=sports", "sports", ["sports"]),
        ("?days=30&vertical=sports,crypto", "sports,crypto", ["sports", "crypto"]),
        ("?days=30&vertical=SPORTS,%20crypto%20,all",
         "SPORTS, crypto ,all", ["sports", "crypto"]),
    ],
)
def test_route_forwards_vertical_csv_and_echoes_normalized(
    client, monkeypatch, qs, expected_arg, expected_echo
):
    captured: list[dict[str, Any]] = []
    _stub_aggregator(monkeypatch, captured)

    resp = client.get(f"/api/public/tail-risk-exposure{qs}")
    assert resp.status_code == 200, resp.text
    assert captured[0]["verticals"] == expected_arg
    assert resp.json()["verticals"] == expected_echo


def test_route_rejects_unknown_placed_by_with_422(client, monkeypatch):
    captured: list[dict[str, Any]] = []
    _stub_aggregator(monkeypatch, captured)

    resp = client.get("/api/public/tail-risk-exposure?days=30&placed_by=evil")
    assert resp.status_code == 422
    assert captured == []


def test_route_rejects_unknown_vertical_with_400(client, monkeypatch):
    """Unknown vertical → HTTP 400."""

    async def _fake(
        days: int = 30,
        placed_by: str | None = None,
        verticals: object = None,
    ) -> dict[str, Any]:
        dashboard._vertical_predicate(verticals)
        return {
            "days": days,
            "buckets": [],
            "totals": dashboard._zero_tail_totals(),
            "tail_concentration": {
                "tail_max_loss_usd": 0.0,
                "total_max_loss_usd": 0.0,
                "pct": None,
                "tone": "empty",
            },
        }

    monkeypatch.setattr(dashboard, "get_tail_risk_exposure", _fake)

    resp = client.get(
        "/api/public/tail-risk-exposure?days=30&vertical=horseracing"
    )
    assert resp.status_code == 400
    assert "unknown vertical" in resp.json().get("detail", "")


def test_route_clamps_days(client, monkeypatch):
    captured: list[dict[str, Any]] = []
    _stub_aggregator(monkeypatch, captured)

    client.get("/api/public/tail-risk-exposure?days=-5")
    client.get("/api/public/tail-risk-exposure?days=999999")
    assert captured[0]["days"] == 1
    assert captured[1]["days"] == 365


asyncpg = pytest.importorskip("asyncpg")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def pg_dsn():
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if not (initdb and pg_ctl):
        pytest.skip("no local Postgres binaries on PATH")

    tmp = Path(tempfile.mkdtemp(prefix="axiom_pg_tre_"))
    data = tmp / "data"
    sock = tmp / "sock"
    sock.mkdir()
    log = tmp / "pg.log"
    port = _free_port()

    subprocess.check_call(
        [initdb, "-D", str(data), "-A", "trust", "-U", "postgres"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    opts = f"-p {port} -k {sock} -h 127.0.0.1"
    subprocess.check_call(
        [pg_ctl, "-D", str(data), "-l", str(log), "-o", opts, "-w", "start"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)

    try:
        yield dsn
    finally:
        subprocess.call(
            [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(tmp, ignore_errors=True)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS positions (
    id            bigserial PRIMARY KEY,
    market_id     text,
    side          text,
    vertical      text,
    quantity      integer,
    avg_price     double precision,
    status        text,
    placed_by     text
);
CREATE TABLE IF NOT EXISTS trades (
    id            bigserial PRIMARY KEY,
    market_id     text,
    side          text,
    vertical      text,
    quantity      integer,
    price         double precision,
    pnl           double precision,
    placed_by     text,
    executed_at   timestamptz NOT NULL DEFAULT now()
);
"""


def test_sql_bucketing_side_aware_maxloss_and_feb20_regression(
    pg_dsn, monkeypatch
):
    """E2E SQL probe: bucketing, side-aware max-loss, Feb-20 regression."""
    from app import db as app_db

    async def go():
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)

        async def _fetchval(q, *a):
            async with pool.acquire() as c:
                return await c.fetchval(q, *a)

        async def _fetch(q, *a):
            async with pool.acquire() as c:
                return await c.fetch(q, *a)

        async def _execute(q, *a):
            async with pool.acquire() as c:
                return await c.execute(q, *a)

        monkeypatch.setattr(app_db, "fetchval", _fetchval)
        monkeypatch.setattr(app_db, "fetch", _fetch)
        monkeypatch.setattr(app_db, "execute", _execute)

        await _execute(_SCHEMA_SQL)

        await _execute(
            "INSERT INTO positions (market_id, side, vertical, quantity, "
            "avg_price, status, placed_by) "
            "VALUES ('NBA-FEB20', 'no', 'sports', 4394, 0.04, 'open', "
            "'autotrader')"
        )
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, quantity, "
            "avg_price, status, placed_by) "
            "VALUES ('MID', 'yes', 'sports', 10, 0.55, 'open', "
            "'autotrader')"
        )
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, quantity, "
            "avg_price, status, placed_by) "
            "VALUES ('CRYPTO-FAV', 'yes', 'crypto', 2, 0.92, 'open', "
            "'orchestrator')"
        )
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, quantity, "
            "avg_price, status, placed_by) "
            "VALUES ('CLOSED', 'no', 'sports', 100, 0.04, 'closed', "
            "'autotrader')"
        )
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, quantity, "
            "avg_price, status, placed_by) "
            "VALUES ('MANUAL', 'yes', 'sports', 5, 0.30, 'open', "
            "'manual_ui')"
        )

        await _execute(
            "INSERT INTO trades (market_id, side, vertical, quantity, "
            "price, pnl, placed_by, executed_at) VALUES "
            "('NBA-FEB20', 'no', 'sports', 4394, 0.04, -4218, "
            "'autotrader', now() - interval '2 days'),"
            "('MID', 'yes', 'sports', 10, 0.55, 50, "
            "'autotrader', now() - interval '1 day'),"
            "('MID2', 'yes', 'sports', 5, 0.55, -3, "
            "'autotrader', now() - interval '1 day'),"
            "('TOP', 'yes', 'sports', 1, 0.95, 12, "
            "'autotrader', now() - interval '3 days')"
        )
        await _execute(
            "INSERT INTO trades (market_id, side, vertical, quantity, "
            "price, pnl, placed_by, executed_at) VALUES "
            "('OLD', 'yes', 'sports', 1, 0.55, -999, "
            "'autotrader', now() - interval '60 days')"
        )

        out = await dashboard.get_tail_risk_exposure(
            days=7, placed_by="system"
        )
        await pool.close()
        return out

    out = _run(go())

    b0 = out["buckets"][0]
    assert b0["label"] == "<$0.10"
    assert b0["totals"]["open_count"] == 1
    assert b0["totals"]["open_max_loss_usd"] == pytest.approx(4218.24, abs=0.01)
    assert b0["totals"]["open_cost_usd"] == pytest.approx(4218.24, abs=0.01)
    assert b0["totals"]["realized_count"] == 1
    assert b0["totals"]["realized_wins"] == 0
    assert b0["totals"]["realized_pnl_usd"] == pytest.approx(-4218.0, abs=0.01)
    assert any(r["vertical"] == "sports" for r in b0["rows"])

    b3 = out["buckets"][3]
    assert b3["label"] == "$0.40–$0.60"
    assert b3["totals"]["open_count"] == 1
    assert b3["totals"]["open_max_loss_usd"] == pytest.approx(5.5, abs=0.001)
    assert b3["totals"]["realized_count"] == 2
    assert b3["totals"]["realized_wins"] == 1
    assert b3["totals"]["realized_pnl_usd"] == pytest.approx(47.0, abs=0.001)

    b6 = out["buckets"][6]
    assert b6["label"] == "$0.90+"
    assert b6["totals"]["open_count"] == 1
    assert b6["totals"]["open_max_loss_usd"] == pytest.approx(1.84, abs=0.001)
    assert b6["totals"]["realized_count"] == 1
    assert b6["totals"]["realized_wins"] == 1
    assert b6["totals"]["realized_pnl_usd"] == pytest.approx(12.0, abs=0.001)

    c = out["tail_concentration"]
    assert c["tail_max_loss_usd"] == pytest.approx(4218.24, abs=0.01)
    assert c["total_max_loss_usd"] == pytest.approx(4225.58, abs=0.01)
    assert c["pct"] > 0.99
    assert c["tone"] == "red"

    assert out["totals"]["open_count"] == 3


def test_empty_positions_falls_back_to_trades_reconstruction(
    pg_dsn, monkeypatch
):
    """Empty positions → trades-fallback reconstructs open exposure."""
    from app import db as app_db

    async def go():
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)

        async def _fetchval(q, *a):
            async with pool.acquire() as c:
                return await c.fetchval(q, *a)

        async def _fetch(q, *a):
            async with pool.acquire() as c:
                return await c.fetch(q, *a)

        async def _execute(q, *a):
            async with pool.acquire() as c:
                return await c.execute(q, *a)

        monkeypatch.setattr(app_db, "fetchval", _fetchval)
        monkeypatch.setattr(app_db, "fetch", _fetch)
        monkeypatch.setattr(app_db, "execute", _execute)

        await _execute(_SCHEMA_SQL)
        await _execute("TRUNCATE positions; TRUNCATE trades;")
        await _execute(
            "INSERT INTO trades (market_id, side, vertical, quantity, "
            "price, pnl, placed_by, executed_at) VALUES "
            "('NBA-FEB20', 'no', 'sports', 4394, 0.04, NULL, "
            "'autotrader', now() - interval '1 day'),"
            "('MID', 'yes', 'sports', 10, 0.55, 50, "
            "'autotrader', now() - interval '1 day')"
        )

        out = await dashboard.get_tail_risk_exposure(
            days=7, placed_by="system"
        )
        await pool.close()
        return out

    out = _run(go())

    assert out["source"] == "trades_fallback"

    b0 = out["buckets"][0]
    assert b0["totals"]["open_count"] == 1
    assert b0["totals"]["open_max_loss_usd"] == pytest.approx(4218.24, abs=0.01)

    b3 = out["buckets"][3]
    assert b3["totals"]["realized_count"] == 1
    assert b3["totals"]["realized_pnl_usd"] == pytest.approx(50.0, abs=0.01)

    c = out["tail_concentration"]
    assert c["tail_max_loss_usd"] == pytest.approx(4218.24, abs=0.01)
    assert c["tone"] == "red"


def test_trades_fallback_nets_multi_fill_market_via_vwap(
    pg_dsn, monkeypatch
):
    """Multi-fill (market, side) collapses into one VWAP-priced netted position."""
    from app import db as app_db

    async def go():
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)

        async def _fetchval(q, *a):
            async with pool.acquire() as c:
                return await c.fetchval(q, *a)

        async def _fetch(q, *a):
            async with pool.acquire() as c:
                return await c.fetch(q, *a)

        async def _execute(q, *a):
            async with pool.acquire() as c:
                return await c.execute(q, *a)

        monkeypatch.setattr(app_db, "fetchval", _fetchval)
        monkeypatch.setattr(app_db, "fetch", _fetch)
        monkeypatch.setattr(app_db, "execute", _execute)

        await _execute(_SCHEMA_SQL)
        await _execute("TRUNCATE positions; TRUNCATE trades;")
        await _execute(
            "INSERT INTO trades (market_id, side, vertical, quantity, "
            "price, pnl, placed_by, executed_at) VALUES "
            "('FAV-DROP', 'yes', 'sports', 100, 0.10, NULL, "
            "'autotrader', now() - interval '1 day'),"
            "('FAV-DROP', 'yes', 'sports', 100, 0.20, NULL, "
            "'autotrader', now() - interval '1 day'),"
            "('FAV-DROP', 'yes', 'sports', 200, 0.05, NULL, "
            "'autotrader', now() - interval '1 day')"
        )

        out = await dashboard.get_tail_risk_exposure(
            days=7, placed_by="system"
        )
        await pool.close()
        return out

    out = _run(go())

    assert out["source"] == "trades_fallback"

    b1 = out["buckets"][1]
    assert b1["label"] == "$0.10–$0.25"
    assert b1["totals"]["open_count"] == 1, (
        "multi-fill same-market trades must net into a single position, "
        f"got open_count={b1['totals']['open_count']}"
    )
    assert b1["totals"]["open_max_loss_usd"] == pytest.approx(360.0, abs=0.01)

    for i, b in enumerate(out["buckets"]):
        if i == 1:
            continue
        assert b["totals"]["open_count"] == 0, (
            f"bucket {i} ({b['label']}) leaked open positions: "
            f"{b['totals']}"
        )


def test_trades_fallback_tail_aggregate_partitions_by_vertical(
    pg_dsn, monkeypatch
):
    """Tail aggregate must partition by vertical, not collapse on market_id."""
    from app import db as app_db

    async def go():
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)

        async def _fetchval(q, *a):
            async with pool.acquire() as c:
                return await c.fetchval(q, *a)

        async def _fetch(q, *a):
            async with pool.acquire() as c:
                return await c.fetch(q, *a)

        async def _execute(q, *a):
            async with pool.acquire() as c:
                return await c.execute(q, *a)

        monkeypatch.setattr(app_db, "fetchval", _fetchval)
        monkeypatch.setattr(app_db, "fetch", _fetch)
        monkeypatch.setattr(app_db, "execute", _execute)

        await _execute(_SCHEMA_SQL)
        await _execute("TRUNCATE positions; TRUNCATE trades;")
        await _execute(
            "INSERT INTO trades (market_id, side, vertical, quantity, "
            "price, pnl, placed_by, executed_at) VALUES "
            "('OVERLAP', 'yes', 'sports', 1000, 0.04, NULL, "
            "'autotrader', now() - interval '1 day'),"
            "('OVERLAP', 'yes', 'crypto', 2000, 0.04, NULL, "
            "'autotrader', now() - interval '1 day')"
        )

        out = await dashboard.get_tail_risk_exposure(
            days=7, placed_by="system"
        )
        await pool.close()
        return out

    out = _run(go())

    assert out["source"] == "trades_fallback"

    b0 = out["buckets"][0]
    assert b0["label"] == "<$0.10"
    assert b0["totals"]["open_count"] == 2, (
        "two verticals sharing a market_id must NOT collapse into one "
        f"netted position; got open_count={b0['totals']['open_count']}"
    )
    verticals = sorted(r["vertical"] for r in b0["rows"])
    assert verticals == ["crypto", "sports"]
    assert b0["totals"]["open_max_loss_usd"] == pytest.approx(2880.0, abs=0.01)

    c = out["tail_concentration"]
    assert c["tail_max_loss_usd"] == pytest.approx(
        b0["totals"]["open_max_loss_usd"], abs=0.01
    )
