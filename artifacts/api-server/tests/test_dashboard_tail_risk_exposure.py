"""Tests for the tail-risk exposure read-model + public route (task #744).

Covers two layers:

1. Pure-Python response shaping in :func:`dashboard.get_tail_risk_exposure`
   — bucket + total + concentration arithmetic against monkeypatched
   ``db.fetch`` rows.  These tests are hermetic, fast, and exhaustive
   for the empty-state, totals roll-up, and concentration-tone
   classification.

2. Route plumbing for ``GET /api/public/tail-risk-exposure`` — defaults,
   ``placed_by`` / ``vertical`` forwarding, response echo, HTTP 400 on
   unknown vertical, HTTP 422 on unknown placed_by — using the same
   stub-the-aggregator pattern as the equity-curve tests so the trust
   boundary is exercised without touching a live DB.

3. A real-Postgres end-to-end test that materializes the minimum
   ``positions`` + ``trades`` schema and exercises the SQL bucketing
   (including side-aware max-loss math and the Feb-20 NBA regression)
   end-to-end.  Skipped when local Postgres binaries are unavailable.
"""
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


# ---------------------------------------------------------------------------
# Pure-Python shape / totals / concentration tests (no DB)
# ---------------------------------------------------------------------------


def _stub_fetch(monkeypatch, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace ``dashboard.db.fetch`` with a coroutine returning ``rows``.

    Captures the SQL string and the trailing positional args so tests
    can assert filter splicing (placed_by / vertical fragments) without
    needing a real DB.
    """
    captured: dict[str, Any] = {"sql": None, "args": None}

    async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        captured["sql"] = query
        captured["args"] = args
        return rows

    monkeypatch.setattr(dashboard.db, "fetch", fake_fetch)
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

        # Always 7 bucket records — even with no data — so the UI doesn't
        # have to gracefully handle missing rows.
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
            }

        # Grand totals zero, concentration "empty" (no exposure to
        # measure — the headline must not fall to a misleading 0% green).
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

        # Tile is read-only analytics — a failed DB call must degrade
        # to an empty render, not a 500 that breaks the whole dashboard.
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

        # The internal `$0.90+` bucket uses `hi=1.01` so a `price=1.00`
        # row still bucket-joins, but the public response must clamp the
        # display upper bound back to 1.00 — leaking 1.01 would render
        # as "$0.90 to $1.01" which is nonsensical.
        last = out["buckets"][-1]
        assert last["label"] == "$0.90+"
        assert last["lo"] == 0.90
        assert last["hi"] == 1.0


class TestRollupArithmetic:
    """Bucket → totals → grand total → concentration arithmetic."""

    def test_rows_aggregate_into_per_bucket_and_grand_totals(self, monkeypatch):
        # Two cells in bucket 0 (<$0.10) — sports + crypto — and one in
        # bucket 5 ($0.75-$0.90) — sports.  Tests the per-bucket roll-up
        # AND the grand total across buckets.
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

        b5 = out["buckets"][5]
        assert b5["label"] == "$0.75–$0.90"
        assert len(b5["rows"]) == 1

        # Buckets that received no rows stay zeroed.
        assert out["buckets"][3]["totals"]["open_count"] == 0

        # Grand total across all rows.
        assert out["totals"]["open_count"] == 7
        assert out["totals"]["open_cost_usd"] == pytest.approx(1170.0)
        assert out["totals"]["realized_count"] == 17
        assert out["totals"]["realized_wins"] == 9
        assert out["totals"]["realized_pnl_usd"] == pytest.approx(-1325.0)

    def test_unknown_bucket_idx_silently_dropped(self, monkeypatch):
        # Defensive — a future schema change shouldn't blow up the
        # response if the SQL ever returns a bucket_idx outside [0..6].
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
        # Row dropped, all buckets stay at zero.
        assert out["totals"]["open_count"] == 0


class TestTailConcentrationHeadline:
    """Concentration tone (red/amber/green/empty) classification."""

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
                self._row(0, 300.0),  # tail bucket
                self._row(3, 700.0),  # mid bucket
            ],
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))
        c = out["tail_concentration"]
        assert c["tail_max_loss_usd"] == pytest.approx(300.0)
        assert c["total_max_loss_usd"] == pytest.approx(1000.0)
        assert c["pct"] == pytest.approx(0.30)
        assert c["tone"] == "red"

    def test_amber_in_the_15_to_30_band(self, monkeypatch):
        _stub_fetch(
            monkeypatch,
            [
                self._row(1, 150.0),  # tail bucket ($0.10-$0.25)
                self._row(4, 850.0),
            ],
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
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))
        c = out["tail_concentration"]
        assert c["pct"] == pytest.approx(0.05)
        assert c["tone"] == "green"

    def test_tail_buckets_are_first_two(self, monkeypatch):
        # The tail set is (`<$0.10`, `$0.10–$0.25`) — exposure in
        # bucket 2 ($0.25-$0.40) must NOT count toward the headline.
        _stub_fetch(
            monkeypatch,
            [
                self._row(2, 600.0),  # NOT tail
                self._row(4, 400.0),
            ],
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))
        assert out["tail_concentration"]["tail_max_loss_usd"] == 0.0
        assert out["tail_concentration"]["pct"] == pytest.approx(0.0)
        assert out["tail_concentration"]["tone"] == "green"


class TestFilterSplicing:
    """``placed_by`` and ``vertical`` are stitched into the SQL via the
    shared helpers — verify the fragments end up in the query string."""

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
        # Both aliases (positions p, trades t) get the predicate.
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
        # Same trust-boundary as `_vertical_predicate` — unknown verticals
        # raise so the route layer can turn them into HTTP 400 instead of
        # silently returning the unfiltered matrix.
        with pytest.raises(ValueError, match="unknown vertical"):
            _run(
                dashboard.get_tail_risk_exposure(
                    days=30, verticals="horseracing"
                )
            )


# ---------------------------------------------------------------------------
# Route plumbing — /api/public/tail-risk-exposure
# ---------------------------------------------------------------------------


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
            },
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
    assert body["verticals"] == []  # marketing-default echo


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
    """Unknown vertical → HTTP 400 with a clear error.

    Same stance as the equity-curve route: silently returning the
    unfiltered matrix on a typo would let an operator stare at all-
    verticals risk thinking they were narrowed.  We make it loud.
    """

    async def _fake(
        days: int = 30,
        placed_by: str | None = None,
        verticals: object = None,
    ) -> dict[str, Any]:
        # Re-runs the real registry-backed validation so the route's
        # ``except ValueError`` branch is exercised end-to-end.
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


# ---------------------------------------------------------------------------
# Live SQL bucketing + side-aware max-loss math + Feb-20 regression.
# Skipped when local Postgres binaries aren't available.
# ---------------------------------------------------------------------------

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
    """End-to-end SQL probe — covers the bucketing CASE expression, the
    side-aware max-loss math, and the Feb-20 NBA regression.

    Hand-seeded fixture:
      - sports / NO position at YES-price=0.04, qty=4,394 — the Feb-20
        NBA blow-up shape.  Lands in `<$0.10` bucket (price=0.04).
        max-loss = qty * max(0.04, 0.96) = 4394 * 0.96 = $4,218.24.
      - sports / YES position at price=0.55, qty=10 — mid bucket
        ($0.40-$0.60).  max-loss = 10 * max(0.55, 0.45) = $5.50.
      - crypto / YES position at price=0.92, qty=2 — top bucket ($0.90+).
        max-loss = 2 * max(0.92, 0.08) = $1.84.
      - sports / closed position at price=0.04 — must NOT show in open
        buckets.

    Realized fixture (last 7 days):
      - sports / pnl=-4218 at price=0.04 — lands in `<$0.10` realized.
      - sports / pnl=+50 at price=0.55 — wins+1.
      - sports / pnl=-3 at price=0.55 — losses+1.
      - sports / pnl=+12 at price=0.95 — top bucket, win.
    """
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

        # --- Open positions ----------------------------------------------
        # Feb-20 NBA shape: NO at YES-price=0.04, qty=4,394.
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, quantity, "
            "avg_price, status, placed_by) "
            "VALUES ('NBA-FEB20', 'no', 'sports', 4394, 0.04, 'open', "
            "'autotrader')"
        )
        # Mid-bucket sports YES.
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, quantity, "
            "avg_price, status, placed_by) "
            "VALUES ('MID', 'yes', 'sports', 10, 0.55, 'open', "
            "'autotrader')"
        )
        # Top-bucket crypto YES.
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, quantity, "
            "avg_price, status, placed_by) "
            "VALUES ('CRYPTO-FAV', 'yes', 'crypto', 2, 0.92, 'open', "
            "'orchestrator')"
        )
        # Closed position — must NOT count.
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, quantity, "
            "avg_price, status, placed_by) "
            "VALUES ('CLOSED', 'no', 'sports', 100, 0.04, 'closed', "
            "'autotrader')"
        )
        # Manual_ui position — must be filtered out by placed_by='system'.
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, quantity, "
            "avg_price, status, placed_by) "
            "VALUES ('MANUAL', 'yes', 'sports', 5, 0.30, 'open', "
            "'manual_ui')"
        )

        # --- Realized trades (within window) -----------------------------
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
        # Out-of-window trade — must NOT count.
        await _execute(
            "INSERT INTO trades (market_id, side, vertical, quantity, "
            "price, pnl, placed_by, executed_at) VALUES "
            "('OLD', 'yes', 'sports', 1, 0.55, -999, "
            "'autotrader', now() - interval '60 days')"
        )

        # System-only filter so the manual_ui row is dropped.
        out = await dashboard.get_tail_risk_exposure(
            days=7, placed_by="system"
        )
        await pool.close()
        return out

    out = _run(go())

    # --- <$0.10 bucket: Feb-20 NBA regression -----------------------------
    b0 = out["buckets"][0]
    assert b0["label"] == "<$0.10"
    # One open position: the Feb-20 NO shape.
    assert b0["totals"]["open_count"] == 1
    # Side-aware max-loss = 4394 * max(0.04, 0.96) = 4394 * 0.96 = 4218.24.
    assert b0["totals"]["open_max_loss_usd"] == pytest.approx(4218.24, abs=0.01)
    # Side-aware cost basis = 4394 * (1 - 0.04) = 4218.24 (NO side pays 1-p).
    assert b0["totals"]["open_cost_usd"] == pytest.approx(4218.24, abs=0.01)
    # One realized loss.
    assert b0["totals"]["realized_count"] == 1
    assert b0["totals"]["realized_wins"] == 0
    assert b0["totals"]["realized_pnl_usd"] == pytest.approx(-4218.0, abs=0.01)
    # Vertical breakdown lives under `rows`.
    assert any(r["vertical"] == "sports" for r in b0["rows"])

    # --- $0.40-$0.60 bucket: mid sports YES ------------------------------
    b3 = out["buckets"][3]
    assert b3["label"] == "$0.40–$0.60"
    assert b3["totals"]["open_count"] == 1
    # YES side, price=0.55, qty=10 → max-loss = 10 * max(0.55, 0.45) = 5.5.
    assert b3["totals"]["open_max_loss_usd"] == pytest.approx(5.5, abs=0.001)
    # Realized: one win (+50) and one loss (-3).
    assert b3["totals"]["realized_count"] == 2
    assert b3["totals"]["realized_wins"] == 1
    assert b3["totals"]["realized_pnl_usd"] == pytest.approx(47.0, abs=0.001)

    # --- $0.90+ bucket: crypto YES at 0.92 -------------------------------
    b6 = out["buckets"][6]
    assert b6["label"] == "$0.90+"
    assert b6["totals"]["open_count"] == 1
    # YES side, price=0.92, qty=2 → max-loss = 2 * max(0.92, 0.08) = 1.84.
    assert b6["totals"]["open_max_loss_usd"] == pytest.approx(1.84, abs=0.001)
    # Realized: one win (+12) at price 0.95.
    assert b6["totals"]["realized_count"] == 1
    assert b6["totals"]["realized_wins"] == 1
    assert b6["totals"]["realized_pnl_usd"] == pytest.approx(12.0, abs=0.001)

    # --- Concentration headline: tail dominates --------------------------
    c = out["tail_concentration"]
    # Tail max-loss = bucket 0 only (no exposure in bucket 1).
    assert c["tail_max_loss_usd"] == pytest.approx(4218.24, abs=0.01)
    # Total = 4218.24 + 5.5 + 1.84.
    assert c["total_max_loss_usd"] == pytest.approx(4225.58, abs=0.01)
    # Tail share ≈ 99.83% → red.
    assert c["pct"] > 0.99
    assert c["tone"] == "red"

    # --- placed_by filter actually dropped the manual_ui row -------------
    # Total open count across ALL buckets == 3 (NOT 4 — the manual_ui
    # position was filtered out by placed_by='system').
    assert out["totals"]["open_count"] == 3
