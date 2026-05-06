"""Tests for the WEATHER calibrator bootstrap (task #770).

Covers the chain that turns settled ``precip_emos_observations`` rows
into a fitted ``per_league:WEATHER`` champion snapshot:

* ``WEATHER`` is registered in ``SUPPORTED_LEAGUES`` so ``apply()``
  no longer bypasses with ``unsupported league``.
* ``apply()`` returns a calibrated probability once a WEATHER
  snapshot is loaded into state.
* The bootstrap script reconstructs YES-aligned probabilities by
  flipping ``predicted_p_above`` for ``direction='below'`` rows.
* The bootstrap writes a ``per_league:WEATHER`` snapshot whose
  ``calibrator_state`` survives a ``load_all()`` round-trip and is
  picked as champion.
* Once the champion is loaded, ``gate_uncalibrated_vertical`` waves
  WEATHER candidates through without needing the operator raw-mode
  override.
* ``--dry-run`` reports metrics but writes nothing.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from app import calibrators, db as _db
from app.risk.gates import RiskContext, gate_uncalibrated_vertical
from scripts import bootstrap_weather_calibrator as _bootstrap


# ---------------------------------------------------------------------------
# Registration sanity
# ---------------------------------------------------------------------------


def test_supported_leagues_contains_weather() -> None:
    assert "WEATHER" in calibrators.SUPPORTED_LEAGUES


def test_apply_no_longer_bypasses_weather_with_unsupported_league() -> None:
    calibrators.invalidate()
    out, info = calibrators.apply(0.42, "WEATHER")
    assert out == 0.42
    assert info["applied"] is False
    assert info["reason"] == "no fitted calibrator"


def test_apply_returns_calibrated_probability_for_weather() -> None:
    calibrators.invalidate()
    state = calibrators.get_state()
    state.calibrators["WEATHER"] = {
        calibrators.ROLE_CHAMPION: calibrators._Calibrator(
            league="WEATHER",
            type="isotonic",
            state={"x": [0.0, 0.7, 1.0], "y": [0.0, 0.3, 1.0]},
            fit_at=time.time(),
            sample_count=500,
        )
    }
    out, info = calibrators.apply(0.7, "weather")  # case-insensitive
    assert info["applied"] is True
    assert info["type"] == "isotonic"
    assert info["league"] == "WEATHER"
    assert out == pytest.approx(0.3, abs=0.01)


# ---------------------------------------------------------------------------
# Bootstrap script
# ---------------------------------------------------------------------------


class _BootstrapFakeDb:
    """Captures the bootstrap's INSERT and serves synthetic settled
    precip rows for ``_fetch_pairs``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.snapshots: list[dict[str, Any]] = []
        self._next_id = 1

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "to_regclass" in query and "precip_emos_observations" in query:
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
                "role": args[9],
                "created_at": time.time(),
            })
            return new_id
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "precip_emos_observations" in query:
            return list(self._rows)
        if "FROM calibration_snapshots" in query:
            by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for r in self.snapshots:
                key = (r["sub_category"], r["role"])
                cur = by_key.get(key)
                if cur is None or r["created_at"] > cur["created_at"]:
                    by_key[key] = r
            return [
                {
                    "sub_category": r["sub_category"],
                    "role": r["role"],
                    "calibrator_state": r["calibrator_state"],
                    "sample_count": r["sample_count"],
                    "fit_at": float(r["created_at"]),
                }
                for r in by_key.values()
            ]
        return []

    async def execute(self, query: str, *args: Any) -> str:
        return ""


def _miscalibrated_above_rows(n: int) -> list[dict[str, Any]]:
    """Above-direction rows where the model says p_above=0.7 but the
    realized YES rate is ~0.3 — a textbook miscalibration the
    isotonic fit must pull down."""
    return [
        {
            "predicted_p_above": 0.7,
            "direction": "above",
            "actual_outcome_yes": 1 if i % 10 < 3 else 0,
        }
        for i in range(n)
    ]


def test_bootstrap_persists_weather_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    rows = _miscalibrated_above_rows(200)
    fake = _BootstrapFakeDb(rows)
    monkeypatch.setattr(_db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_db, "fetch", fake.fetch)
    monkeypatch.setattr(_db, "execute", fake.execute)

    monkeypatch.setattr("sys.argv", ["bootstrap_weather_calibrator"])
    rc = asyncio.run(_bootstrap.main())
    assert rc == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is True
    assert summary["league"] == "WEATHER"
    assert summary["sample_count"] == 200
    assert summary["calibrator_type"] == "isotonic"
    assert summary["snapshot_id"] is not None
    assert summary["brier_delta"] is not None
    # Calibrator must materially reduce Brier on a clearly
    # miscalibrated population.
    assert summary["brier_delta"] < -0.05

    assert len(fake.snapshots) == 1
    row = fake.snapshots[0]
    assert row["sub_category"] == "per_league:WEATHER"
    assert row["role"] == calibrators.ROLE_CHAMPION
    cfg = json.loads(row["config"])
    assert cfg["source_breakdown"] == {
        "precip_emos_observations_bootstrap": 200
    }

    # Round-trip: a fresh load picks the snapshot up as champion.
    calibrators.invalidate()
    state = asyncio.run(calibrators.load_all())
    assert "WEATHER" in state.calibrators
    champ = state.champion("WEATHER")
    assert champ is not None
    assert champ.type == "isotonic"
    assert champ.role == calibrators.ROLE_CHAMPION


def test_bootstrap_dry_run_does_not_persist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    rows = _miscalibrated_above_rows(100)
    fake = _BootstrapFakeDb(rows)
    monkeypatch.setattr(_db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_db, "fetch", fake.fetch)
    monkeypatch.setattr(_db, "execute", fake.execute)

    monkeypatch.setattr(
        "sys.argv", ["bootstrap_weather_calibrator", "--dry-run"]
    )
    rc = asyncio.run(_bootstrap.main())
    assert rc == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is True
    assert summary["dry_run"] is True
    assert summary["snapshot_id"] is None
    assert fake.snapshots == []


def test_bootstrap_below_direction_flips_p_above(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """For ``direction='below'`` the YES side is "precip < threshold",
    so YES probability must be reconstructed as ``1 - predicted_p_above``.
    A row population where the *flipped* probability is well-calibrated
    must produce a near-zero Brier improvement; a bootstrap that
    forgot to flip would see a wildly miscalibrated population and a
    massive Brier drop instead.
    """
    # p_above=0.2 ⇒ p_yes(below)=0.8 ⇒ YES rate should be ~0.8.
    rows = [
        {
            "predicted_p_above": 0.2,
            "direction": "below",
            "actual_outcome_yes": 1 if i % 10 < 8 else 0,
        }
        for i in range(200)
    ]
    fake = _BootstrapFakeDb(rows)
    monkeypatch.setattr(_db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_db, "fetch", fake.fetch)
    monkeypatch.setattr(_db, "execute", fake.execute)

    monkeypatch.setattr(
        "sys.argv", ["bootstrap_weather_calibrator", "--dry-run"]
    )
    rc = asyncio.run(_bootstrap.main())
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    # Brier_before is computed against the (correctly flipped) p_yes
    # series; for a well-calibrated 0.8 prediction with 0.8 hit rate
    # the baseline Brier sits near 0.16 and the calibrator can only
    # marginally improve it.  If the flip were missing, brier_before
    # would explode (0.2 vs 0.8 hit rate ⇒ ~0.52) and the delta would
    # be enormous — guarding against that regression is the point.
    assert summary["brier_before"] == pytest.approx(0.16, abs=0.05)


def test_bootstrap_below_threshold_returns_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        {
            "predicted_p_above": 0.5,
            "direction": "above",
            "actual_outcome_yes": i % 2,
        }
        for i in range(calibrators.MIN_REFIT_SAMPLES - 1)
    ]
    fake = _BootstrapFakeDb(rows)
    monkeypatch.setattr(_db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_db, "fetch", fake.fetch)
    monkeypatch.setattr(_db, "execute", fake.execute)

    monkeypatch.setattr("sys.argv", ["bootstrap_weather_calibrator"])
    rc = asyncio.run(_bootstrap.main())
    assert rc == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is False
    assert "MIN_REFIT_SAMPLES" in summary["reason"]
    assert fake.snapshots == []


# ---------------------------------------------------------------------------
# Risk gate ungating
# ---------------------------------------------------------------------------


def _ctx(**kwargs: Any) -> RiskContext:
    base: dict[str, Any] = dict(
        market_id="KXHIGHNY-25MAY05-T70",
        side="YES",
        vertical="weather",
        league="WEATHER",
        market_type="weather:HIGH",
        matchup=None,
        current_price=0.5,
        model_probability=0.6,
        edge=0.1,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=10,
        intended_notional_usd=5.0,
        bankroll_usd=10000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
    )
    base.update(kwargs)
    return RiskContext(**base)


def test_gate_blocks_weather_when_no_calibrator() -> None:
    calibrators.invalidate()
    ctx = _ctx()
    result = asyncio.run(gate_uncalibrated_vertical(ctx))
    assert result.passed is False
    assert "WEATHER" in result.reason
    assert result.details["calibrator_loaded"] is False


def test_gate_passes_weather_once_champion_is_loaded() -> None:
    """The Done-criterion check: a fitted WEATHER champion in state
    must let ``gate_uncalibrated_vertical`` wave WEATHER candidates
    through on its own — no operator raw-mode override required.
    Daily honesty Section 5 stays GREEN as a direct consequence
    (it counts orders shipped against a raw-mode marker, and with
    the gate passing cleanly no marker is ever persisted).
    """
    calibrators.invalidate()
    state = calibrators.get_state()
    state.calibrators["WEATHER"] = {
        calibrators.ROLE_CHAMPION: calibrators._Calibrator(
            league="WEATHER",
            type="isotonic",
            state={"x": [0.0, 0.5, 1.0], "y": [0.0, 0.5, 1.0]},
            fit_at=time.time(),
            sample_count=500,
        )
    }
    ctx = _ctx()
    result = asyncio.run(gate_uncalibrated_vertical(ctx))
    assert result.passed is True
    assert result.reason == ""
    assert result.details["calibrator_loaded"] is True
    assert result.details["calibrator_type"] == "isotonic"
    # Specifically: no raw-mode marker fields populated, because no
    # raw-mode override was needed.
    assert "raw_mode" not in result.details
