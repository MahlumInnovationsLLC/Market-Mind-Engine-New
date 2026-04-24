"""Tests for the AutoResearch mining pipeline + shadow feature wiring."""
from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.autoresearch import mining, stats, store
from app.features.registry import FeatureRegistry, FeatureSpec


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def test_pearson_perfect_positive_correlation():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0, 4.0, 6.0, 8.0, 10.0]
    assert stats.pearson(xs, ys) == pytest.approx(1.0)


def test_pearson_perfect_negative_correlation():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [10.0, 8.0, 6.0, 4.0, 2.0]
    assert stats.pearson(xs, ys) == pytest.approx(-1.0)


def test_pearson_zero_variance_returns_zero():
    # Constant series have undefined Pearson — must return 0, not NaN.
    assert stats.pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0
    assert stats.pearson([], []) == 0.0


def test_spearman_handles_ties():
    # Strictly monotone ranks despite tied values in xs.
    xs = [1.0, 1.0, 2.0, 3.0, 4.0]
    ys = [10.0, 11.0, 20.0, 30.0, 40.0]
    s = stats.spearman(xs, ys)
    assert 0.9 < s <= 1.0


def test_sharpe_signal_to_noise():
    # value*residual = [1, 1, 1, 1, 1] — std=0, so Sharpe is 0 by convention.
    assert stats.sharpe([1, 1, 1, 1, 1], [1, 1, 1, 1, 1]) == 0.0
    # Mean strongly positive vs small noise → Sharpe > 0.
    s = stats.sharpe([1, 2, 3, 4, 5], [1, 1, 1, 1, 1])
    assert s > 0


def test_monotonicity_increasing_buckets():
    # Values 0..29 paired with residuals = values/10 → strict monotone.
    n = 30
    values = [float(i) for i in range(n)]
    residuals = [v / 10.0 for v in values]
    assert stats.monotonicity(values, residuals, bins=5) == pytest.approx(1.0)


def test_monotonicity_returns_zero_for_too_few_samples():
    assert stats.monotonicity([1.0, 2.0], [1.0, 2.0], bins=5) == 0.0


def test_evaluate_summary_keys():
    summary = stats.evaluate([1.0, 2.0, 3.0, 4.0], [0.1, 0.2, 0.3, 0.4])
    assert set(summary.keys()) == {"n", "ic", "sharpe", "monotonicity"}
    assert summary["n"] == 4
    assert summary["ic"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Feature registry shadow flag
# ---------------------------------------------------------------------------


def test_registry_shadow_specs_excluded_from_production_view():
    r = FeatureRegistry()
    r.register(FeatureSpec("prod_a", "src", "t", "raw", 0.0, lambda _: 1.0))
    r.register(FeatureSpec(
        "shadow_a", "src", "t", "raw", 0.0, lambda _: 9.0, shadow=True,
    ))
    r.register(FeatureSpec("prod_b", "src", "t", "raw", 0.0, lambda _: 2.0))

    # Production-facing API ignores shadow specs entirely.
    assert r.names() == ("prod_a", "prod_b")
    assert len(r) == 2
    assert r.metadata() == [
        {"index": 0, "name": "prod_a", "source": "src", "transform": "t",
         "dtype": "raw", "default": 0.0, "shadow": False},
        {"index": 1, "name": "prod_b", "source": "src", "transform": "t",
         "dtype": "raw", "default": 0.0, "shadow": False},
    ]
    # Shadow-aware enumeration is opt-in.
    assert {s.name for s in r.shadow_specs()} == {"shadow_a"}
    assert {s.name for s in r.all_specs()} == {"prod_a", "shadow_a", "prod_b"}


def test_registry_build_skips_shadow_from_vector_but_computes_them():
    """build() must compute shadow specs (so derived features and
    AutoResearch telemetry see them) while keeping them out of the
    positional vector that aligns to the trained model."""
    from datetime import datetime, timezone

    r = FeatureRegistry()
    r.register(FeatureSpec("prod_a", "src", "t", "raw", 0.0, lambda _: 1.0))
    captured: dict[str, float] = {}

    def _shadow_compute(bc):
        captured["seen"] = 1.0
        return 7.0

    r.register(FeatureSpec(
        "shadow_x", "src", "t", "raw", 0.0, _shadow_compute, shadow=True,
    ))
    r.register(FeatureSpec("prod_b", "src", "t", "raw", 0.0, lambda _: 3.0))

    values, _present = r.build(
        candidate={}, ctx={}, now=datetime(2025, 1, 1, tzinfo=timezone.utc),
        overrides={},
    )
    # Vector contains only production specs in declared order.
    assert values == [1.0, 3.0]
    # Shadow compute still ran (so we collect telemetry / training rows).
    assert captured == {"seen": 1.0}


def test_registry_metadata_can_include_shadow_for_admin_view():
    r = FeatureRegistry()
    r.register(FeatureSpec("prod", "src", "t", "raw", 0.0, lambda _: 0.0))
    r.register(FeatureSpec(
        "shadow", "src", "t", "raw", 0.0, lambda _: 0.0, shadow=True,
    ))
    md = r.metadata(include_shadow=True)
    assert [row["name"] for row in md] == ["prod", "shadow"]
    assert {row["name"]: row["shadow"] for row in md} == {
        "prod": False, "shadow": True,
    }


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


def test_feature_name_is_stable_and_lowercase():
    assert store.feature_name("Sports", "FLOW_ALERT", "Count_24h") == (
        "uw__flow_alert__count_24h__sports"
    )


def test_feature_name_components_separated():
    n = store.feature_name("crypto", "darkpool_print", "sum_premium_24h")
    assert n.startswith("uw__")
    assert "darkpool_print" in n
    assert "sum_premium_24h" in n
    assert n.endswith("crypto")


def test_feature_name_with_ticker_appends_suffix():
    assert store.feature_name(
        "crypto", "flow_alert", "count_24h", ticker="BTC"
    ) == "uw__flow_alert__count_24h__crypto__btc"
    # None / empty ticker preserves the legacy global name so we don't
    # silently re-key already-promoted candidates.
    assert store.feature_name(
        "crypto", "flow_alert", "count_24h", ticker=None
    ) == "uw__flow_alert__count_24h__crypto"
    assert store.feature_name(
        "crypto", "flow_alert", "count_24h", ticker=""
    ) == "uw__flow_alert__count_24h__crypto"


# ---------------------------------------------------------------------------
# Per-ticker mining helpers
# ---------------------------------------------------------------------------


def test_is_ticker_linked_covers_documented_verticals():
    # Verticals where the Kalshi ticker carries a real underlying.
    assert mining.is_ticker_linked("crypto")
    assert mining.is_ticker_linked("crypto-binary")
    assert mining.is_ticker_linked("commodities")
    assert mining.is_ticker_linked("equities")
    # Sports markets have no UW ticker counterpart — global only.
    assert not mining.is_ticker_linked("sports")
    assert not mining.is_ticker_linked("soccer")
    assert not mining.is_ticker_linked("weather")
    # Case + whitespace tolerant.
    assert mining.is_ticker_linked("  CRYPTO  ")


def test_extract_underlying_ticker_strips_kx_prefix_and_segments():
    assert mining.extract_underlying_ticker("KXBTC-25APR2310-T100000") == "BTC"
    assert mining.extract_underlying_ticker("KXAAPL_2025_Q2") == "AAPL"
    assert mining.extract_underlying_ticker("kxeth-25dec01-t5000") == "ETH"
    # No KX prefix is fine — first segment wins.
    assert mining.extract_underlying_ticker("AAPL-ABC") == "AAPL"
    # Bare KX-only / empty inputs return None so the caller can fall back.
    assert mining.extract_underlying_ticker("") is None
    assert mining.extract_underlying_ticker(None) is None
    assert mining.extract_underlying_ticker("KX") == "KX"  # not stripped: len<=2


def test_group_by_ticker_buckets_and_collects_unparseable_under_none():
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    trades = [
        {"market_id": "KXBTC-1", "created_at": base, "p_side": 0.5, "y_side": 1},
        {"market_id": "KXBTC-2", "created_at": base, "p_side": 0.5, "y_side": 0},
        {"market_id": "KXETH-1", "created_at": base, "p_side": 0.5, "y_side": 1},
        {"market_id": None,      "created_at": base, "p_side": 0.5, "y_side": 0},
        {"market_id": "",        "created_at": base, "p_side": 0.5, "y_side": 1},
    ]
    buckets = mining._group_by_ticker(trades)
    # Parseable rows split per ticker; the two unparseable rows fall into
    # the global (None) bucket so the caller can still score them.
    assert set(buckets.keys()) == {"BTC", "ETH", None}
    assert len(buckets["BTC"]) == 2
    assert len(buckets["ETH"]) == 1
    assert len(buckets[None]) == 2


# ---------------------------------------------------------------------------
# run_mining: global vs per-ticker code paths
# ---------------------------------------------------------------------------


class _FakeMiningConn:
    """Minimal asyncpg-style stub backing the mining job.

    Records every aggregate query so the test can assert that
    ticker-linked verticals issued per-ticker SQL while sports stuck
    to the global path.
    """

    def __init__(self, *, trades: dict[str, list[dict[str, Any]]],
                 uw_counts: dict[tuple[str, str | None], int]) -> None:
        self.trades = trades
        self.uw_counts = uw_counts
        self.upserts: list[dict[str, Any]] = []
        self.aggregate_calls: list[dict[str, Any]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        if q.startswith("SELECT DISTINCT vertical"):
            return [{"vertical": v} for v in self.trades.keys()]
        if "FROM trades t" in q and "trade_postmortems" in q:
            vertical = args[0]
            return list(self.trades.get(vertical, []))
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split())
        if "FROM uw_signals" in q and "count(*)" in q:
            signal_type = args[0]
            ticker = args[3] if "upper(ticker)" in q else None
            self.aggregate_calls.append(
                {"signal": signal_type, "ticker": ticker, "op": "count"}
            )
            return {"v": int(self.uw_counts.get((signal_type, ticker), 0))}
        if "FROM uw_signals" in q and "sum" in q:
            signal_type = args[0]
            ticker = args[3] if "upper(ticker)" in q else None
            self.aggregate_calls.append(
                {"signal": signal_type, "ticker": ticker, "op": "sum_premium"}
            )
            return {"v": float(self.uw_counts.get((signal_type, ticker), 0))}
        if q.startswith("INSERT INTO autoresearch_features"):
            row = {
                "name": args[0],
                "vertical": args[1],
                "signal_type": args[2],
                "aggregator": args[3],
                "ic": args[4],
                "sharpe": args[5],
                "monotonicity": args[6],
                "n_samples": args[7],
                "metadata": args[8],
                "status": "shadow",
            }
            self.upserts.append(row)
            return row
        return None


def _synth_trades(n: int, *, market_id: str,
                  start: datetime,
                  outcome_pattern: list[int]) -> list[dict[str, Any]]:
    """Build n settled-trade dicts mimicking the rows _settled_trades emits."""
    rows: list[dict[str, Any]] = []
    for i in range(n):
        rows.append({
            "trade_id": f"{market_id}-{i}",
            "created_at": start + timedelta(minutes=i),
            "side_lc": "yes",
            "model_prob": 0.5,
            "market_id": market_id,
            "actual_outcome": outcome_pattern[i % len(outcome_pattern)],
        })
    return rows


def test_run_mining_global_path_for_sports(monkeypatch: pytest.MonkeyPatch):
    """Sports verticals must aggregate UW globally (no ticker filter)."""
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    trades = {
        "sports": _synth_trades(
            40, market_id="KXNFL-25SEP04SEAARI-SEA",
            start=base, outcome_pattern=[1, 0],
        ),
    }
    # Global UW count is non-zero for the global key (signal, None).
    uw = {
        ("flow_alert", None): 5,
        ("darkpool_print", None): 3,
    }
    fake = _FakeMiningConn(trades=trades, uw_counts=uw)

    @asynccontextmanager
    async def fake_acquire():
        yield fake

    monkeypatch.setattr(mining.db, "acquire", fake_acquire)
    monkeypatch.setattr(store.db, "acquire", fake_acquire)
    monkeypatch.setenv("AXIOM_AUTORESEARCH_MIN_SAMPLES", "20")

    res = asyncio.run(mining.run_mining(verticals=["sports"]))
    assert res.candidates_evaluated > 0
    assert res.candidates_persisted > 0
    # Every UW aggregate call for sports must be ticker-less (global).
    assert fake.aggregate_calls, "expected at least one UW aggregate call"
    assert all(c["ticker"] is None for c in fake.aggregate_calls)
    # Persisted names must NOT carry a ticker suffix.
    for row in fake.upserts:
        assert row["vertical"] == "sports"
        assert row["name"].count("__") == 3  # uw__sig__agg__vertical
        assert "ticker" not in (row["metadata"] or {})


def test_run_mining_per_ticker_path_for_crypto(monkeypatch: pytest.MonkeyPatch):
    """Crypto trades must split into one (signal × agg × ticker) row each."""
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    trades = {
        "crypto": (
            _synth_trades(40, market_id="KXBTC-25APR2310-T1",
                          start=base, outcome_pattern=[1, 0])
            + _synth_trades(40, market_id="KXETH-25APR2310-T1",
                            start=base, outcome_pattern=[0, 1])
        ),
    }
    uw = {
        ("flow_alert", "BTC"): 7,
        ("flow_alert", "ETH"): 2,
        ("darkpool_print", "BTC"): 4,
        ("darkpool_print", "ETH"): 1,
    }
    fake = _FakeMiningConn(trades=trades, uw_counts=uw)

    @asynccontextmanager
    async def fake_acquire():
        yield fake

    monkeypatch.setattr(mining.db, "acquire", fake_acquire)
    monkeypatch.setattr(store.db, "acquire", fake_acquire)
    monkeypatch.setenv("AXIOM_AUTORESEARCH_MIN_SAMPLES", "20")

    res = asyncio.run(mining.run_mining(verticals=["crypto"]))

    # Every UW aggregate call must carry one of the two underlyings.
    assert fake.aggregate_calls
    seen_tickers = {c["ticker"] for c in fake.aggregate_calls}
    assert seen_tickers == {"BTC", "ETH"}

    # Persisted names embed the ticker; both BTC and ETH show up for
    # each (signal, aggregator) combo.
    names = {row["name"] for row in fake.upserts}
    assert any(n.endswith("__btc") for n in names)
    assert any(n.endswith("__eth") for n in names)
    # And every persisted row stores the ticker in its metadata so
    # downstream consumers don't need to reparse the name.
    btc_rows = [r for r in fake.upserts if r["name"].endswith("__btc")]
    assert btc_rows
    import json
    md = json.loads(btc_rows[0]["metadata"])
    assert md.get("ticker") == "BTC"
    # Counts: 2 signals × 3 aggregators × 2 tickers = 12 candidates.
    assert res.candidates_evaluated == 12
    assert res.candidates_persisted == 12


def test_run_mining_per_ticker_falls_back_when_no_ticker(
    monkeypatch: pytest.MonkeyPatch,
):
    """A ticker-linked vertical whose trades all lack a parseable
    market_id should fall back to the global aggregation rather than
    silently dropping the whole vertical."""
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    trades = {
        "crypto": [
            {**row, "market_id": None}
            for row in _synth_trades(
                40, market_id="ignored",
                start=base, outcome_pattern=[1, 0],
            )
        ],
    }
    uw = {("flow_alert", None): 5, ("darkpool_print", None): 3}
    fake = _FakeMiningConn(trades=trades, uw_counts=uw)

    @asynccontextmanager
    async def fake_acquire():
        yield fake

    monkeypatch.setattr(mining.db, "acquire", fake_acquire)
    monkeypatch.setattr(store.db, "acquire", fake_acquire)
    monkeypatch.setenv("AXIOM_AUTORESEARCH_MIN_SAMPLES", "20")

    res = asyncio.run(mining.run_mining(verticals=["crypto"]))
    assert res.candidates_persisted > 0
    # Fall-back path issued ticker-less queries.
    assert all(c["ticker"] is None for c in fake.aggregate_calls)
    # And no name carries a ticker suffix.
    for row in fake.upserts:
        assert not row["name"].endswith("__btc")
        assert not row["name"].endswith("__eth")


# ---------------------------------------------------------------------------
# Regression: the SQL in _settled_trades must reference real columns on
# the ``trades`` table. Previously the query said ``t.created_at`` — a
# column that does not exist on ``trades`` (only ``executed_at`` /
# ``settled_at`` are defined) — so every vertical fell through the bare
# ``except Exception`` and the panel reported NO_CANDIDATES_DISCOVERED.
# ---------------------------------------------------------------------------


def test_settled_trades_sql_references_real_trade_columns(
    monkeypatch: pytest.MonkeyPatch,
):
    """Capture the exact SQL the mining job sends to asyncpg and assert
    it does not reference ``t.created_at`` (not a column on ``trades``)
    and does reference ``t.executed_at`` (the actual trade-entry
    timestamp). Catches the column-name regression that silently
    emptied ``autoresearch_features``."""
    captured: list[str] = []

    class _CapturingConn:
        async def fetch(self, query: str, *_args: Any) -> list[dict[str, Any]]:
            captured.append(query)
            return []

    @asynccontextmanager
    async def fake_acquire():
        yield _CapturingConn()

    monkeypatch.setattr(mining.db, "acquire", fake_acquire)
    asyncio.run(mining._settled_trades("crypto", 30))

    assert captured, "expected _settled_trades to issue a SQL query"
    sql = captured[0]
    # The historical bug: SELECT t.created_at FROM trades — ``trades``
    # has no ``created_at`` column, only ``executed_at``.
    assert "t.created_at" not in sql, (
        "mining query references t.created_at which does not exist on "
        "the trades table; use t.executed_at instead"
    )
    assert "t.executed_at" in sql, (
        "mining query must read the trade-entry timestamp from "
        "t.executed_at (aliased to created_at for the in-memory dict)"
    )


# ---------------------------------------------------------------------------
# Sanity: live registry production count is unchanged by adding the shadow
# capability (no rows registered as shadow yet — keeps trained model aligned).
# ---------------------------------------------------------------------------


def test_live_registry_unchanged_count():
    from app.features import FEATURE_COUNT, registry as live_registry

    assert len(live_registry) == FEATURE_COUNT == 32
    # And no production spec is accidentally flagged as shadow.
    assert all(not s.shadow for s in live_registry.specs())
