"""Tests for the CRYPTO calibrator + V1 archive bootstrap (task #754).

Covers the four moving parts that turn the V1 archive's ~595k labelled
crypto-binary outcomes into a working per-league CRYPTO calibrator:

* ``SUPPORTED_LEAGUES`` exposes ``CRYPTO`` so ``apply()`` no longer
  bypasses crypto candidates with ``unsupported league``.
* ``apply()`` returns a calibrated probability once a CRYPTO snapshot
  is loaded (sanity that the registration actually wires the lookup).
* ``_fetch_training_data`` composes the crypto-vertical UNION ALL
  branch into ``v1_settled``, with the synthetic ``league='CRYPTO'``
  literal and the ``vertical='crypto'`` filter present.
* ``scripts.bootstrap_crypto_calibrator`` writes a
  ``per_league:CRYPTO`` ``calibration_snapshots`` row whose
  ``calibrator_state`` survives a ``load_all()`` round-trip.
* ``crypto.backtest_scheduler.check_now`` triggers
  ``calibrators.refit_all()`` when ``CRYPTO_BACKTEST_REFIT_CALIBRATORS``
  is enabled (default) and skips the refit when the env switch is OFF.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from app import calibrators, db as _db
from app.crypto import backtest_scheduler as _bts
from scripts import bootstrap_crypto_calibrator as _bootstrap


# ---------------------------------------------------------------------------
# T001 — SUPPORTED_LEAGUES + apply() registration
# ---------------------------------------------------------------------------


def test_supported_leagues_contains_crypto() -> None:
    """``CRYPTO`` must be registered or every crypto candidate bypasses
    calibration with ``unsupported league``.
    """
    assert "CRYPTO" in calibrators.SUPPORTED_LEAGUES


def test_apply_no_longer_bypasses_crypto_with_unsupported_league() -> None:
    """Before #754 the bypass reason was ``unsupported league``; after
    registration it must instead report ``no fitted calibrator`` when
    nothing is loaded — confirming the lookup reaches the per-league
    table even with no snapshot.
    """
    calibrators.invalidate()
    out, info = calibrators.apply(0.42, "CRYPTO")
    assert out == 0.42
    assert info["applied"] is False
    assert info["reason"] == "no fitted calibrator"


def test_apply_returns_calibrated_probability_for_crypto() -> None:
    """A fitted CRYPTO isotonic snapshot in memory must be picked up by
    ``apply()`` — the wiring sanity for the bootstrap → loader →
    predictor chain.
    """
    calibrators.invalidate()
    state = calibrators.get_state()
    state.calibrators["CRYPTO"] = {calibrators.ROLE_CHAMPION: calibrators._Calibrator(
        league="CRYPTO",
        type="isotonic",
        # Aggressive miscalibration: model says 0.7 but actual rate is
        # ~0.3 — calibrator must pull the probability down materially.
        state={"x": [0.0, 0.7, 1.0], "y": [0.0, 0.3, 1.0]},
        fit_at=time.time(),
        sample_count=5000,
    )}
    out, info = calibrators.apply(0.7, "crypto")  # case-insensitive
    assert info["applied"] is True
    assert info["type"] == "isotonic"
    assert info["league"] == "CRYPTO"
    assert out == pytest.approx(0.3, abs=0.01)


# ---------------------------------------------------------------------------
# T002 — v1_settled CTE folds in vertical='crypto' rows
# ---------------------------------------------------------------------------


def test_v1_settled_cte_includes_crypto_vertical_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composed SQL must carry a UNION ALL branch that synthesises
    ``league='CRYPTO'`` from rows whose ``vertical='crypto'`` so the
    nightly refit can lift V1 crypto rows even though league inference
    intentionally left them unresolved.
    """
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION", "1")
    monkeypatch.setenv("AXIOM_USE_SHADOW_BUFFER_FOR_CALIBRATION", "0")

    captured_sql: list[str] = []

    async def fake_fetchval(query: str, *args: Any) -> Any:
        if "to_regclass" in query and "v1_archive" in query:
            return True
        return None

    async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        captured_sql.append(query)
        return []

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    asyncio.run(calibrators._fetch_training_data())

    assert len(captured_sql) == 1
    sql = captured_sql[0]
    # Crypto-vertical branch present.
    assert "lower(coalesce(vertical, '')) = 'crypto'" in sql
    # The synthetic league literal must be the all-caps token the
    # forward predictor uses (``apply(p, league='CRYPTO')``).
    assert "'CRYPTO'::text" in sql
    # Must still be UNION'd into v1_settled (not a separate CTE).
    assert "v1_settled" in sql
    assert sql.count("UNION ALL") >= 2  # live∪v1, v1-sports∪v1-crypto


def test_v1_settled_cte_crypto_branch_does_not_flip_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V1 crypto ``predicted_prob`` is YES-aligned by construction —
    the synthesised CRYPTO branch must NOT flip the probability the way
    the league-aligned branch does for ``side=no/sell`` rows.
    """
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION", "1")
    monkeypatch.setenv("AXIOM_USE_SHADOW_BUFFER_FOR_CALIBRATION", "0")

    captured_sql: list[str] = []

    async def fake_fetchval(query: str, *args: Any) -> Any:
        if "to_regclass" in query and "v1_archive" in query:
            return True
        return None

    async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        captured_sql.append(query)
        return []

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    asyncio.run(calibrators._fetch_training_data())
    sql = captured_sql[0]
    # Locate the crypto branch — text after the synthetic literal.
    crypto_branch_idx = sql.index("'CRYPTO'::text")
    crypto_branch = sql[crypto_branch_idx:]
    # Within the crypto branch, the only p_side expression is a clamp
    # of ``predicted_prob`` itself — there must NOT be a CASE that
    # flips by ``analysis_json->>'side'``.
    assert "analysis_json->>'side'" not in crypto_branch.split("UNION ALL")[0]


def test_v1_settled_branches_are_partitioned_by_vertical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two ``v1_settled`` UNION branches must be mutually exclusive
    so a hypothetical V1 crypto-vertical row that ALSO has a non-null
    league + recognised side can't get double-counted (once into its
    inferred league bucket and once into the synthetic CRYPTO bucket).
    The guard lives on the league-aligned branch:
    ``vertical <> 'crypto'``.
    """
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION", "1")
    monkeypatch.setenv("AXIOM_USE_SHADOW_BUFFER_FOR_CALIBRATION", "0")

    captured_sql: list[str] = []

    async def fake_fetchval(query: str, *args: Any) -> Any:
        if "to_regclass" in query and "v1_archive" in query:
            return True
        return None

    async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        captured_sql.append(query)
        return []

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    asyncio.run(calibrators._fetch_training_data())
    sql = captured_sql[0]
    # Slice the league-aligned (first) branch — everything before the
    # synthetic CRYPTO literal.
    crypto_branch_idx = sql.index("'CRYPTO'::text")
    league_branch = sql[:crypto_branch_idx]
    # The exclusion guard must be present inside the league-aligned
    # branch (so it filters that branch, not the CRYPTO one).
    assert "lower(coalesce(vertical, '')) <> 'crypto'" in league_branch


# ---------------------------------------------------------------------------
# T003 — bootstrap script writes a per_league:CRYPTO snapshot
# ---------------------------------------------------------------------------


class _BootstrapFakeDb:
    """Captures the bootstrap script's INSERT and serves a synthetic
    crypto-archive row set for ``_fetch_pairs``.
    """

    def __init__(self, pairs: list[tuple[float, int]]) -> None:
        self._pairs = pairs
        self.snapshots: list[dict[str, Any]] = []
        self._next_id = 1

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "to_regclass" in query and "v1_archive" in query:
            return True
        if "INSERT INTO calibration_snapshots" in query:
            new_id = self._next_id
            self._next_id += 1
            self.snapshots.append({
                "id": new_id,
                "sub_category": args[0],
                "sample_count": int(args[4]),
                "calibrator_state": args[5],
                "config": args[8],
                "created_at": time.time(),
            })
            return new_id
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "v1_archive.trade_analyses" in query:
            return [
                {"predicted_prob": p, "actual_outcome": y}
                for p, y in self._pairs
            ]
        if "FROM calibration_snapshots" in query:
            # Service the loader's DISTINCT ON query so the round-trip
            # check below can read the freshly-written snapshot back.
            by_sub: dict[str, dict[str, Any]] = {}
            for r in self.snapshots:
                cur = by_sub.get(r["sub_category"])
                if cur is None or r["created_at"] > cur["created_at"]:
                    by_sub[r["sub_category"]] = r
            return [
                {
                    "sub_category": r["sub_category"],
                    "calibrator_state": r["calibrator_state"],
                    "sample_count": r["sample_count"],
                    "fit_at": float(r["created_at"]),
                }
                for r in by_sub.values()
            ]
        return []

    async def execute(self, query: str, *args: Any) -> str:
        return ""


def test_bootstrap_script_persists_crypto_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end exercise of the bootstrap script: synthesises a
    miscalibrated row population, runs ``main()``, and asserts:

    * a ``per_league:CRYPTO`` row landed in ``calibration_snapshots``
      with the expected ``v1_archive_bootstrap`` source breakdown;
    * the JSON summary printed to stdout reports a positive Brier
      reduction (the calibrator actually improved fit);
    * a fresh ``calibrators.load_all()`` picks the row back up so the
      CRYPTO entry shows up in the in-memory state.
    """
    # 200 rows, model uniformly says 0.7 but reality fires at ~0.3 —
    # well above ISOTONIC_MIN_SAMPLES, so the bootstrap fits isotonic.
    pairs: list[tuple[float, int]] = []
    for i in range(200):
        pairs.append((0.7, 1 if i % 10 < 3 else 0))

    fake = _BootstrapFakeDb(pairs)
    monkeypatch.setattr(_db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_db, "fetch", fake.fetch)
    monkeypatch.setattr(_db, "execute", fake.execute)

    # No CLI args — defaults to live persist.
    monkeypatch.setattr("sys.argv", ["bootstrap_crypto_calibrator"])
    rc = asyncio.run(_bootstrap.main())
    assert rc == 0

    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["ok"] is True
    assert summary["league"] == "CRYPTO"
    assert summary["sample_count"] == 200
    assert summary["calibrator_type"] == "isotonic"
    assert summary["snapshot_id"] is not None
    # Brier improved (delta is brier_after - brier_before, so negative).
    assert summary["brier_delta"] is not None
    assert summary["brier_delta"] < 0.0

    # Snapshot row landed with the right sub_category + bootstrap tag.
    assert len(fake.snapshots) == 1
    row = fake.snapshots[0]
    assert row["sub_category"] == "per_league:CRYPTO"
    cfg = json.loads(row["config"])
    assert cfg["source_breakdown"] == {"v1_archive_bootstrap": 200}

    # Loader picks the snapshot back up — this is the "next boot" path.
    calibrators.invalidate()
    state = asyncio.run(calibrators.load_all())
    assert "CRYPTO" in state.calibrators
    assert state.champion("CRYPTO").type == "isotonic"


def test_bootstrap_script_dry_run_does_not_persist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` must compute metrics without writing the snapshot —
    operators rely on this to A/B a fit before committing."""
    pairs = [(0.6, 1 if i % 3 == 0 else 0) for i in range(100)]
    fake = _BootstrapFakeDb(pairs)
    monkeypatch.setattr(_db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_db, "fetch", fake.fetch)
    monkeypatch.setattr(_db, "execute", fake.execute)

    monkeypatch.setattr("sys.argv", ["bootstrap_crypto_calibrator", "--dry-run"])
    rc = asyncio.run(_bootstrap.main())
    assert rc == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is True
    assert summary["dry_run"] is True
    assert summary["snapshot_id"] is None
    assert fake.snapshots == []


# ---------------------------------------------------------------------------
# T004 — backtest_scheduler triggers calibrator refit
# ---------------------------------------------------------------------------


def test_check_now_triggers_calibrator_refit_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``CRYPTO_BACKTEST_REFIT_CALIBRATORS`` ON (default) the
    scheduler sweep must call ``calibrators.refit_all()`` exactly once
    after the per-pair loop and surface the summary on the sweep
    payload.
    """
    monkeypatch.delenv("CRYPTO_BACKTEST_REFIT_CALIBRATORS", raising=False)

    refit_calls: list[float] = []

    async def fake_refit_all() -> dict[str, Any]:
        refit_calls.append(time.time())
        return {
            "fitted": 3, "skipped": 1, "errors": 0,
            "duration_seconds": 0.42,
            "leagues": [],
        }

    async def fake_run_pair(*, symbol: str, timeframe: str) -> dict[str, Any]:
        return {
            "symbol": symbol, "timeframe": timeframe,
            "ok": True, "alerts_raised": 0,
        }

    monkeypatch.setattr(calibrators, "refit_all", fake_refit_all)
    monkeypatch.setattr(_bts, "run_pair", fake_run_pair)

    out = asyncio.run(_bts.check_now(pairs=[("BTC", "1h"), ("ETH", "15m")]))

    assert len(refit_calls) == 1
    refit = out["calibrator_refit"]
    assert refit["ok"] is True
    assert refit["fitted"] == 3
    assert refit["skipped"] == 1
    assert refit["errors"] == 0
    assert refit["duration_seconds"] == pytest.approx(0.42)


def test_check_now_skips_calibrator_refit_when_env_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kill-switch — operators can revert to nightly-only refit if
    the scheduler proves too aggressive — and the sweep payload must
    advertise the skip so the dashboard can show why.
    """
    monkeypatch.setenv("CRYPTO_BACKTEST_REFIT_CALIBRATORS", "0")

    refit_calls: list[float] = []

    async def fake_refit_all() -> dict[str, Any]:
        refit_calls.append(time.time())
        return {"fitted": 0, "skipped": 0, "errors": 0}

    async def fake_run_pair(*, symbol: str, timeframe: str) -> dict[str, Any]:
        return {
            "symbol": symbol, "timeframe": timeframe,
            "ok": True, "alerts_raised": 0,
        }

    monkeypatch.setattr(calibrators, "refit_all", fake_refit_all)
    monkeypatch.setattr(_bts, "run_pair", fake_run_pair)

    out = asyncio.run(_bts.check_now(pairs=[("BTC", "1h")]))
    assert refit_calls == []
    assert out["calibrator_refit"] == {
        "ok": True, "skipped": "disabled_via_env",
    }


def test_check_now_records_calibrator_refit_failure_without_aborting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sick calibrator fit for one league must not break the next
    crypto sweep — the refit error is logged on the payload but the
    sweep itself reports ``ok``.
    """
    monkeypatch.delenv("CRYPTO_BACKTEST_REFIT_CALIBRATORS", raising=False)

    async def boom_refit() -> dict[str, Any]:
        raise RuntimeError("simulated calibrator fit failure")

    async def fake_run_pair(*, symbol: str, timeframe: str) -> dict[str, Any]:
        return {
            "symbol": symbol, "timeframe": timeframe,
            "ok": True, "alerts_raised": 0,
        }

    monkeypatch.setattr(calibrators, "refit_all", boom_refit)
    monkeypatch.setattr(_bts, "run_pair", fake_run_pair)

    out = asyncio.run(_bts.check_now(pairs=[("BTC", "1h")]))
    refit = out["calibrator_refit"]
    assert refit["ok"] is False
    assert "RuntimeError" in refit["error"]
    # The sweep itself still reports success — the per-pair loop ran.
    assert out["errors"] == 0
    assert len(out["pairs"]) == 1
