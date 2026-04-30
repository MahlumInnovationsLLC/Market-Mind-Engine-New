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


def _stub_fetch(
    monkeypatch,
    rows: list[dict[str, Any]],
    tail_max_loss: float = 0.0,
) -> dict[str, Any]:
    """Replace ``dashboard.db.fetch`` and ``.fetchval`` with stubs.

    Captures the bucket-query SQL string and trailing positional args
    so tests can assert filter splicing (placed_by / vertical fragments)
    without needing a real DB.  ``fetchval`` is stubbed to return
    ``tail_max_loss`` so the dedicated row-level tail aggregate
    (introduced after the architect review) doesn't escape to a real DB
    in unit tests; tests that exercise the headline arithmetic pass an
    explicit value here.
    """
    # The aggregator may issue more than one fetch (positions primary +
    # trades fallback when positions returns empty/zero open exposure).
    # Concatenate every query string into ``cap["sql"]`` so existing
    # filter-splicing assertions ("p.placed_by ... in sql",
    # "t.placed_by ... in sql") still hold regardless of which path ran.
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
                # Derived field: None when no realized trades land in
                # the bucket.  Surfaced in the payload so the UI doesn't
                # have to compute it (and risk drift between web/mobile).
                "ev_per_trade": None,
            }
            # Per-bucket `is_tail` flag: only the `<$0.10` bucket is
            # entirely below the $0.20 threshold; the $0.10–$0.25 bucket
            # straddles it so its UI badge will rely on the row-level
            # tail headline rather than the bucket flag.
            assert "is_tail" in b
        assert out["buckets"][0]["is_tail"] is True
        assert out["buckets"][1]["is_tail"] is False
        assert out["buckets"][6]["is_tail"] is False

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
        # EV/trade per bucket: -1500 / 5 = -300 in bucket 0 (sports
        # cell drove the realized count; crypto cell had 0 trades).
        assert b0["totals"]["ev_per_trade"] == pytest.approx(-300.0)
        # Per-cell ev_per_trade also surfaced for the matrix view.
        sports_cell = next(r for r in b0["rows"] if r["vertical"] == "sports")
        crypto_cell = next(r for r in b0["rows"] if r["vertical"] == "crypto")
        assert sports_cell["ev_per_trade"] == pytest.approx(-300.0)
        assert crypto_cell["ev_per_trade"] is None  # zero realized

        b5 = out["buckets"][5]
        assert b5["label"] == "$0.75–$0.90"
        assert len(b5["rows"]) == 1
        # +175 across 12 trades → +14.58/trade — the favorite-band
        # signal the audit highlighted as masked by the tail blow-ups.
        assert b5["totals"]["ev_per_trade"] == pytest.approx(175.0 / 12.0)

        # Buckets that received no rows stay zeroed.
        assert out["buckets"][3]["totals"]["open_count"] == 0
        assert out["buckets"][3]["totals"]["ev_per_trade"] is None

        # Grand total across all rows.
        assert out["totals"]["open_count"] == 7
        assert out["totals"]["open_cost_usd"] == pytest.approx(1170.0)
        assert out["totals"]["realized_count"] == 17
        assert out["totals"]["realized_wins"] == 9
        assert out["totals"]["realized_pnl_usd"] == pytest.approx(-1325.0)
        # Grand EV/trade = -1325 / 17.
        assert out["totals"]["ev_per_trade"] == pytest.approx(-1325.0 / 17.0)

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
        # Tail value comes from the dedicated row-level fetchval
        # aggregate (price < $0.20 strictly), not from summing whole
        # buckets — the $0.10–$0.25 bucket straddles the threshold.
        _stub_fetch(
            monkeypatch,
            [
                self._row(0, 300.0),  # tail bucket
                self._row(3, 700.0),  # mid bucket
            ],
            tail_max_loss=300.0,
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))
        c = out["tail_concentration"]
        assert c["tail_max_loss_usd"] == pytest.approx(300.0)
        assert c["total_max_loss_usd"] == pytest.approx(1000.0)
        assert c["pct"] == pytest.approx(0.30)
        assert c["tone"] == "red"
        # Headline copy: surfaces the threshold the UI shows so every
        # consumer agrees on what "tail" means without hardcoding it.
        assert c["threshold_price"] == 0.20

    def test_amber_in_the_15_to_30_band(self, monkeypatch):
        # Row-level tail = 150 (the trades in the $0.10–$0.25 bucket
        # that landed below $0.20).
        _stub_fetch(
            monkeypatch,
            [
                self._row(1, 150.0),  # tail bucket ($0.10-$0.25)
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
        # Row-level tail is computed by the SQL aggregate against
        # `price < 0.20`.  Exposure that lands in bucket 2 or higher
        # must NOT count toward the headline.  We assert this by
        # passing a stub fetchval value of 0 — i.e. no rows below
        # threshold even though buckets 2 + 4 both have exposure.
        _stub_fetch(
            monkeypatch,
            [
                self._row(2, 600.0),  # NOT tail
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
        # Regression: prior behavior summed the whole `$0.10–$0.25`
        # bucket into the tail headline even though that bucket spans
        # prices up to $0.25 — over-counting any trade priced
        # $0.20–$0.25.  The dedicated fetchval aggregate fixes this:
        # the bucket totals show 200 in bucket 1 (mixed prices), but
        # only 80 of those rows are actually below $0.20.
        _stub_fetch(
            monkeypatch,
            [
                self._row(1, 200.0),  # bucket spans 0.10–0.25
                self._row(4, 800.0),
            ],
            tail_max_loss=80.0,  # only the < $0.20 portion
        )
        out = _run(dashboard.get_tail_risk_exposure(days=30))
        c = out["tail_concentration"]
        # Tail = 80 / (200 + 800) = 0.08, NOT 200 / 1000 = 0.20.
        assert c["tail_max_loss_usd"] == pytest.approx(80.0)
        assert c["pct"] == pytest.approx(0.08)
        assert c["tone"] == "green"


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


def test_empty_positions_falls_back_to_trades_reconstruction(
    pg_dsn, monkeypatch
):
    """When the `positions` table is present but empty (e.g. fresh
    install, ingestion stalled, or table truncated), the aggregator
    must fall back to reconstructing open exposure from the `trades`
    table rather than reporting zero open exposure.

    Hand-seeded fixture (positions: empty; trades only):
      - sports / NO at YES-price=0.04, qty=4,394, pnl IS NULL — the
        Feb-20 NBA tail shape, recorded only as a trade fill.  Expected
        max-loss = 4394 * max(0.04, 0.96) = $4,218.24, lands in
        `<$0.10` bucket.
      - One realized trade at 0.55 (pnl=+50) — should still surface in
        the realized slice via the same fallback path.

    Asserts:
      - `source == "trades_fallback"` in the payload meta.
      - Bucket 0 reports the reconstructed open exposure.
      - Tail headline reflects the reconstructed tail max-loss.
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
        # Module-scoped pg_dsn fixture means prior tests' rows persist.
        # Positions table left empty on purpose for THIS test.
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

    # Source flag flipped — the UI / observability hooks rely on this
    # to surface that the headline is approximate.
    assert out["source"] == "trades_fallback"

    # Bucket 0 reconstructed from the unsettled NO trade.
    b0 = out["buckets"][0]
    assert b0["totals"]["open_count"] == 1
    assert b0["totals"]["open_max_loss_usd"] == pytest.approx(4218.24, abs=0.01)

    # Realized slice still flows through the fallback path.
    b3 = out["buckets"][3]
    assert b3["totals"]["realized_count"] == 1
    assert b3["totals"]["realized_pnl_usd"] == pytest.approx(50.0, abs=0.01)

    # Tail headline = reconstructed tail max-loss (only the < $0.20 trade).
    c = out["tail_concentration"]
    assert c["tail_max_loss_usd"] == pytest.approx(4218.24, abs=0.01)
    assert c["tone"] == "red"


def test_trades_fallback_nets_multi_fill_market_via_vwap(
    pg_dsn, monkeypatch
):
    """Multiple opening fills for the same `(market_id, side)` should
    collapse into a single netted position priced at the VWAP — not be
    counted as N independent opens.

    Hand-seeded fixture (positions: empty; trades only):
      - Three fills for `('FAV-DROP', 'yes', 'sports')`, all pnl IS NULL:
          fill 1: qty=100 at price=0.10
          fill 2: qty=100 at price=0.20
          fill 3: qty=200 at price=0.05
        Expected net qty = 400, VWAP = (10 + 20 + 10) / 400 = $0.10.
        After bucketing on VWAP, this lands in `$0.10–$0.25` (lo=0.10
        inclusive).  Side=YES, so max-loss = 400 * max(0.10, 0.90) = 360.
      - The naive (non-netted) sum would be 3 separate opens with mixed
        prices — open_count=3, max-loss = 100*0.90 + 100*0.80 + 200*0.95
        = 90 + 80 + 190 = 360 (coincidentally same total here, but the
        bucket placement and per-position EV would differ).  The
        regression we care about is `open_count == 1`, not 3.

    Asserts:
      - One netted open position (not three).
      - VWAP-bucketed into $0.10–$0.25.
      - Max-loss = 400 * 0.90 = 360.
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
        # Module-scoped pg_dsn fixture means prior tests' rows persist.
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

    # Three fills should collapse into ONE netted position landing in
    # the $0.10–$0.25 VWAP bucket.
    b1 = out["buckets"][1]
    assert b1["label"] == "$0.10–$0.25"
    assert b1["totals"]["open_count"] == 1, (
        "multi-fill same-market trades must net into a single position, "
        f"got open_count={b1['totals']['open_count']}"
    )
    # YES side at VWAP=0.10, qty=400 → max-loss = 400 * max(0.10, 0.90) = 360.
    assert b1["totals"]["open_max_loss_usd"] == pytest.approx(360.0, abs=0.01)

    # Other buckets must be empty — the 3 fills did NOT leak into <$0.10
    # or any other bucket because we bucket on the netted VWAP, not the
    # individual fill prices.
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
    """Headline tail aggregate (`sql_tail_trades`) must group by the
    same `(vertical, market_key, side)` key as the matrix
    (`sql_trades_only.open_netted`) — otherwise a `market_id` shared
    across verticals would be silently merged in the headline while
    staying separate in the bucket totals, drifting the two readings
    apart.

    Hand-seeded fixture (positions: empty; trades only):
      Same ticker `'OVERLAP'`, same `'yes'` side, same sub-tail price
      (0.04), but two different verticals:
        sports : qty=1000 at 0.04  → max-loss = 1000 * 0.96 = 960
        crypto : qty=2000 at 0.04  → max-loss = 2000 * 0.96 = 1920
      Combined headline tail max-loss must therefore equal
      960 + 1920 = $2,880.  (If the tail SQL forgot the `vertical`
      partition, both rows would collapse into a single VWAP-merged
      group and the per-vertical headline math would still happen to
      match here only because the prices coincide — the bucket matrix,
      however, would still keep them apart.  We assert both directly
      so any future regression in either query trips this test.)
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

    # Bucket 0 ($0.10–$0.10 actually <$0.10) — both rows land here at
    # VWAP=0.04.  Because the matrix groups by (vertical, market_key,
    # side), each vertical contributes its own netted position row.
    b0 = out["buckets"][0]
    assert b0["label"] == "<$0.10"
    assert b0["totals"]["open_count"] == 2, (
        "two verticals sharing a market_id must NOT collapse into one "
        f"netted position; got open_count={b0['totals']['open_count']}"
    )
    # Per-vertical breakdown lives under `rows`.
    verticals = sorted(r["vertical"] for r in b0["rows"])
    assert verticals == ["crypto", "sports"]
    # Combined max-loss = 1000*0.96 + 2000*0.96 = 2880.
    assert b0["totals"]["open_max_loss_usd"] == pytest.approx(2880.0, abs=0.01)

    # Headline tail max-loss must equal the matrix sum — i.e. the tail
    # aggregate also partitioned by vertical.  Drift here would mean
    # `sql_tail_trades` silently merged the two verticals.
    c = out["tail_concentration"]
    assert c["tail_max_loss_usd"] == pytest.approx(
        b0["totals"]["open_max_loss_usd"], abs=0.01
    )
