"""Tests for the shadow-prediction calibration loop (task #753).

Covers the four pieces of the loop end-to-end:

  * ``shadow_predictor.run_cycle`` writes one row per active prediction
    and is idempotent on a same-hour re-run.
  * ``shadow_settler.run_cycle`` copies the resolved outcome onto the
    snapshot and inserts a buffer row (idempotent).
  * ``calibrators._fetch_training_data`` UNIONs ``shadow_learning_buffer``
    when present and tags the rows ``source='shadow'`` so the
    per-league refit's ``source_breakdown`` reflects the contribution.
    The same query gates cleanly when the buffer table is missing
    (fresh DB without migration 0043) — i.e. live rows still come
    through, no UndefinedTable raises.
  * ``pipeline._calibration_and_drift_shadow`` writes a shadow snapshot
    and raises a brier_drift alert with a ``[shadow]`` prefix.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import calibrators, db as _db
from app.learning import shadow_predictor, shadow_settler
from app.settlement import pipeline as _pipeline


# ---------------------------------------------------------------------------
# In-memory DB shim
# ---------------------------------------------------------------------------


class _InMemoryDb:
    """Tiny query-pattern dispatcher that's just rich enough to drive
    the four code paths under test.  Each table is a list of dicts;
    inserts/updates mutate them in place.
    """

    def __init__(self) -> None:
        # predictions: rows with kalshi_market_id, model_yes_prob, etc.
        self.predictions: list[dict[str, Any]] = []
        # shadow_predictions: rows the predictor wrote.
        self.shadow_predictions: list[dict[str, Any]] = []
        # shadow_learning_buffer: rows the settler wrote.
        self.shadow_buffer: list[dict[str, Any]] = []
        # calibration_snapshots and model_health_alerts inserts (for
        # the drift parity test only).
        self.calibration_snapshots: list[tuple[Any, ...]] = []
        self.alerts: list[tuple[Any, ...]] = []
        # Toggle that gates `to_regclass('public.shadow_learning_buffer')`.
        self.shadow_table_present: bool = True
        # Toggle for v1_archive table (kept off — we don't exercise v1
        # in this suite).
        self.v1_table_present: bool = False
        self._next_shadow_id = 1
        self._next_buffer_id = 1
        self._next_snapshot_id = 1

    # ----- read paths ------------------------------------------------

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(query.split())

        # shadow_predictor: scan active predictions
        if "FROM predictions" in q and "kalshi_market_id IS NOT NULL" in q \
                and "model_yes_prob IS NOT NULL" in q \
                and "settled_at IS NULL" in q:
            limit = args[0] if args else 1000
            out = []
            for r in self.predictions:
                if r.get("kalshi_market_id") is None:
                    continue
                if r.get("model_yes_prob") is None:
                    continue
                if r.get("settled_at") is not None:
                    continue
                out.append({
                    "prediction_id": r.get("id"),
                    "kalshi_market_id": r.get("kalshi_market_id"),
                    "league": r.get("league"),
                    "p_yes": float(r["model_yes_prob"]),
                    "yes_price": r.get("kalshi_price"),
                    "confidence": r.get("confidence"),
                    "game_time": r.get("game_time"),
                })
                if len(out) >= limit:
                    break
            return out

        # shadow_settler: pending resolutions join
        if "WITH resolved AS" in q and "shadow_predictions sp" in q:
            limit = args[0] if args else 1000
            # Resolved = most recent settled prediction per kalshi_market_id
            resolved: dict[str, dict[str, Any]] = {}
            for p in self.predictions:
                if p.get("kalshi_market_id") is None:
                    continue
                if p.get("settled_at") is None or p.get("outcome") is None:
                    continue
                cur = resolved.get(p["kalshi_market_id"])
                if cur is None or p["settled_at"] > cur["settled_at"]:
                    resolved[p["kalshi_market_id"]] = p
            out = []
            for sp in self.shadow_predictions:
                if sp.get("settled_at") is not None:
                    continue
                r = resolved.get(sp["market_id"])
                if not r:
                    continue
                out.append({
                    "shadow_id": sp["id"],
                    "market_id": sp["market_id"],
                    "league": sp.get("league"),
                    "vertical": sp.get("vertical"),
                    "predicted_prob": float(sp["predicted_prob"]),
                    "uncalibrated_prob": sp.get("uncalibrated_prob"),
                    # Pass the raw outcome through unchanged so the
                    # settler's own _normalize_outcome path is exercised.
                    # In production this is a *text* column ('yes'/'no'/
                    # 'void') per migration 0022.
                    "outcome": r["outcome"],
                    "settled_at": r["settled_at"],
                })
                if len(out) >= limit:
                    break
            return out

        # calibrators._fetch_training_data: UNION of live + (optional) shadow
        if "live_settled" in q:
            out: list[dict[str, Any]] = []
            # No live rows in this suite — we only feed shadow.
            if self.shadow_table_present and "shadow_settled" in q:
                for b in self.shadow_buffer:
                    if b.get("league") is None:
                        continue
                    if b.get("predicted_prob") is None:
                        continue
                    if b.get("actual_outcome") is None:
                        continue
                    if b.get("settled_at") is None:
                        continue
                    out.append({
                        "league": str(b["league"]).upper(),
                        "p_side": float(b["predicted_prob"]),
                        "y_side": int(b["actual_outcome"]),
                        "source": "shadow",
                    })
            return out

        return []

    # ----- single-value paths ----------------------------------------

    async def fetchval(self, query: str, *args: Any):
        q = " ".join(query.split())

        if "to_regclass('public.shadow_learning_buffer')" in q:
            return self.shadow_table_present
        if "to_regclass('v1_archive.trade_analyses')" in q:
            return self.v1_table_present

        # shadow_predictor INSERT ... ON CONFLICT DO NOTHING RETURNING id
        if "INSERT INTO shadow_predictions" in q:
            (market_id, league, vertical, kalshi_yes_price,
             predicted_prob, confidence, model_name, model_version,
             predicted_at, metadata_json) = args
            for existing in self.shadow_predictions:
                if existing["market_id"] == market_id \
                        and existing["predicted_at"] == predicted_at:
                    return None  # ON CONFLICT DO NOTHING
            row_id = self._next_shadow_id
            self._next_shadow_id += 1
            self.shadow_predictions.append({
                "id": row_id,
                "market_id": market_id,
                "league": league,
                "vertical": vertical,
                "kalshi_yes_price": kalshi_yes_price,
                "predicted_prob": predicted_prob,
                "uncalibrated_prob": None,
                "confidence": confidence,
                "model_name": model_name,
                "model_version": model_version,
                "predicted_at": predicted_at,
                "settled_at": None,
                "outcome": None,
                "metadata": json.loads(metadata_json or "{}"),
            })
            return row_id

        # shadow_settler stamp UPDATE
        if "UPDATE shadow_predictions" in q and "SET settled_at" in q:
            shadow_id, settled_at, outcome = args
            for sp in self.shadow_predictions:
                if sp["id"] == shadow_id and sp.get("settled_at") is None:
                    sp["settled_at"] = settled_at
                    sp["outcome"] = int(outcome)
                    return shadow_id
            return None

        # shadow_settler buffer INSERT
        if "INSERT INTO shadow_learning_buffer" in q:
            (shadow_id, market_id, league, vertical,
             predicted_prob, uncalibrated_prob, actual_outcome,
             settled_at) = args
            for b in self.shadow_buffer:
                if b["shadow_prediction_id"] == shadow_id:
                    return None  # ON CONFLICT DO NOTHING
            row_id = self._next_buffer_id
            self._next_buffer_id += 1
            self.shadow_buffer.append({
                "id": row_id,
                "shadow_prediction_id": shadow_id,
                "market_id": market_id,
                "league": league,
                "vertical": vertical,
                "predicted_prob": predicted_prob,
                "uncalibrated_prob": uncalibrated_prob,
                "actual_outcome": int(actual_outcome),
                "source": "shadow",
                "has_full_features": True,
                "processed": False,
                "settled_at": settled_at,
            })
            return row_id

        if "INSERT INTO calibration_snapshots" in q and "RETURNING id" in q:
            new_id = self._next_snapshot_id
            self._next_snapshot_id += 1
            self.calibration_snapshots.append(args)
            return new_id

        # The pipeline drift path also issues a "recent unack'd alert" probe
        if "FROM model_health_alerts" in q and "brier_drift" in q:
            # No prior alerts in this suite — return None.
            return None

        return None

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        if "FROM shadow_learning_buffer" in q and "AS brier" in q:
            vertical = args[0]
            ps = []
            ys = []
            for b in self.shadow_buffer:
                if b.get("vertical") != vertical:
                    continue
                if b.get("settled_at") is None:
                    continue
                if b.get("predicted_prob") is None or b.get("actual_outcome") is None:
                    continue
                ps.append(float(b["predicted_prob"]))
                ys.append(int(b["actual_outcome"]))
            n = len(ps)
            if n == 0:
                return {"n": 0, "brier": None, "mae": None}
            brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / n
            mae = sum(abs(p - y) for p, y in zip(ps, ys)) / n
            return {"n": n, "brier": brier, "mae": mae}
        return None

    # ----- insert/update paths called via execute() -------------------

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split())
        if "INSERT INTO calibration_snapshots" in q:
            self.calibration_snapshots.append(args)
            return ""
        if "INSERT INTO model_health_alerts" in q:
            self.alerts.append(args)
            return ""
        return ""


@pytest.fixture
def fake_db(monkeypatch):
    fake = _InMemoryDb()
    # All three modules import db lazily via `from .. import db`,
    # so patching the underlying module attributes is enough.
    monkeypatch.setattr(_db, "fetch", fake.fetch)
    monkeypatch.setattr(_db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(_db, "execute", fake.execute)
    calibrators.invalidate()
    return fake


# ---------------------------------------------------------------------------
# T002 — predictor
# ---------------------------------------------------------------------------


def test_shadow_predictor_writes_one_row_per_active_prediction(fake_db):
    fake_db.predictions = [
        {
            "id": 1, "kalshi_market_id": "KX-NBA-LAL-1",
            "league": "NBA", "model_yes_prob": 0.62,
            "kalshi_price": 0.55, "confidence": 0.8,
            "settled_at": None, "game_time": None,
        },
        {
            "id": 2, "kalshi_market_id": "KX-MLB-LAD-1",
            "league": "MLB", "model_yes_prob": 0.41,
            "kalshi_price": 0.40, "confidence": 0.7,
            "settled_at": None, "game_time": None,
        },
        # Already settled — should NOT produce a snapshot.
        {
            "id": 3, "kalshi_market_id": "KX-NHL-STL-1",
            "league": "NHL", "model_yes_prob": 0.55,
            "settled_at": datetime.now(timezone.utc), "outcome": 1,
            "game_time": None,
        },
        # Missing prob — skipped.
        {
            "id": 4, "kalshi_market_id": "KX-NFL-GB-1",
            "league": "NFL", "model_yes_prob": None,
            "settled_at": None, "game_time": None,
        },
    ]
    cr = asyncio.run(shadow_predictor.run_cycle())
    assert cr.candidates_scanned == 2  # only the 2 active rows
    assert cr.snapshots_written == 2
    assert cr.skipped_duplicate == 0
    assert {r["market_id"] for r in fake_db.shadow_predictions} == {
        "KX-NBA-LAL-1", "KX-MLB-LAD-1",
    }


def test_shadow_predictor_is_idempotent_within_the_same_hour(fake_db):
    fake_db.predictions = [
        {
            "id": 1, "kalshi_market_id": "KX-NBA-LAL-1",
            "league": "NBA", "model_yes_prob": 0.62,
            "settled_at": None, "game_time": None,
        },
    ]
    asyncio.run(shadow_predictor.run_cycle())
    cr = asyncio.run(shadow_predictor.run_cycle())
    # The unique (market_id, predicted_at) constraint at the hour
    # bucket means the second call skips on conflict.
    assert cr.snapshots_written == 0
    assert cr.skipped_duplicate == 1
    assert len(fake_db.shadow_predictions) == 1


# ---------------------------------------------------------------------------
# T003 — settler
# ---------------------------------------------------------------------------


def test_shadow_settler_copies_outcome_and_inserts_buffer(fake_db):
    settled_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    # 1 active shadow snapshot whose underlying market just settled.
    fake_db.shadow_predictions.append({
        "id": 1, "market_id": "KX-NBA-LAL-1", "league": "NBA",
        "vertical": "sports", "predicted_prob": 0.62,
        "uncalibrated_prob": None, "predicted_at": settled_at,
        "settled_at": None, "outcome": None,
    })
    fake_db._next_shadow_id = 2
    # NOTE: predictions.outcome is a *text* column ('yes'/'no'/'void')
    # per migration 0022; the settler must normalise to int for the
    # shadow tables.  Use the realistic text label here so we exercise
    # the normalisation path (a regression here would silently break
    # buffer inserts in production).
    fake_db.predictions.append({
        "id": 99, "kalshi_market_id": "KX-NBA-LAL-1",
        "league": "NBA", "model_yes_prob": 0.62,
        "settled_at": settled_at, "outcome": "yes", "game_time": None,
    })

    cr = asyncio.run(shadow_settler.run_cycle())
    assert cr.candidates_scanned == 1
    assert cr.settled == 1
    assert cr.buffer_inserted == 1
    assert cr.voids_skipped == 0
    # Outcome stamped on the shadow row (normalised to int)
    assert fake_db.shadow_predictions[0]["settled_at"] == settled_at
    assert fake_db.shadow_predictions[0]["outcome"] == 1
    # Buffer row populated
    assert len(fake_db.shadow_buffer) == 1
    buf = fake_db.shadow_buffer[0]
    assert buf["shadow_prediction_id"] == 1
    assert buf["actual_outcome"] == 1
    assert buf["predicted_prob"] == pytest.approx(0.62)

    # Re-run is idempotent — buffer row is unique on shadow_prediction_id.
    cr2 = asyncio.run(shadow_settler.run_cycle())
    assert cr2.buffer_inserted == 0
    assert len(fake_db.shadow_buffer) == 1


def test_shadow_settler_normalizes_text_outcomes_and_skips_voids(fake_db):
    """Coverage for the predictions.outcome text vocabulary
    ('yes'/'no'/'void').  'no' should land as 0 in the buffer; 'void'
    should be counted in voids_skipped and never write a row (a void
    market carries no calibration signal)."""
    settled_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    fake_db.shadow_predictions.extend([
        {"id": 10, "market_id": "KX-MKT-A", "league": "NBA",
         "vertical": "sports", "predicted_prob": 0.40,
         "uncalibrated_prob": None, "predicted_at": settled_at,
         "settled_at": None, "outcome": None},
        {"id": 11, "market_id": "KX-MKT-B", "league": "NBA",
         "vertical": "sports", "predicted_prob": 0.55,
         "uncalibrated_prob": None, "predicted_at": settled_at,
         "settled_at": None, "outcome": None},
        {"id": 12, "market_id": "KX-MKT-VOID", "league": "NBA",
         "vertical": "sports", "predicted_prob": 0.50,
         "uncalibrated_prob": None, "predicted_at": settled_at,
         "settled_at": None, "outcome": None},
    ])
    fake_db._next_shadow_id = 13
    fake_db.predictions.extend([
        {"id": 100, "kalshi_market_id": "KX-MKT-A", "league": "NBA",
         "model_yes_prob": 0.40, "settled_at": settled_at,
         "outcome": "no", "game_time": None},
        {"id": 101, "kalshi_market_id": "KX-MKT-B", "league": "NBA",
         "model_yes_prob": 0.55, "settled_at": settled_at,
         "outcome": "YES", "game_time": None},  # case-insensitive
        {"id": 102, "kalshi_market_id": "KX-MKT-VOID", "league": "NBA",
         "model_yes_prob": 0.50, "settled_at": settled_at,
         "outcome": "void", "game_time": None},
    ])

    cr = asyncio.run(shadow_settler.run_cycle())
    assert cr.candidates_scanned == 3
    assert cr.settled == 2
    assert cr.buffer_inserted == 2
    assert cr.voids_skipped == 1
    # 'no' -> 0, 'YES' -> 1, void dropped entirely.
    outcomes = sorted(
        b["actual_outcome"] for b in fake_db.shadow_buffer
    )
    assert outcomes == [0, 1]
    market_ids = {b["market_id"] for b in fake_db.shadow_buffer}
    assert market_ids == {"KX-MKT-A", "KX-MKT-B"}
    # The void row is left un-stamped on shadow_predictions so a future
    # cycle re-inspects it (cheap; the void status is sticky upstream).
    void_row = next(
        sp for sp in fake_db.shadow_predictions if sp["id"] == 12
    )
    assert void_row["settled_at"] is None
    assert void_row["outcome"] is None


def test_shadow_settler_normalize_outcome_helper():
    """Direct contract test for the normaliser — the unit under test
    that closes the previous-round audit gap (predictions.outcome is
    text but shadow tables hold int)."""
    n = shadow_settler._normalize_outcome
    assert n("yes") == 1
    assert n("Yes") == 1
    assert n(" YES ") == 1
    assert n("no") == 0
    assert n("NO") == 0
    assert n(1) == 1
    assert n(0) == 0
    assert n(True) == 1
    assert n(False) == 0
    # void / cancel / unparseable / out-of-range numeric all map to None
    assert n("void") is None
    assert n("cancel") is None
    assert n("") is None
    assert n(None) is None
    assert n(2) is None
    assert n("maybe") is None


# ---------------------------------------------------------------------------
# T005 — calibrator UNION
# ---------------------------------------------------------------------------


def test_fetch_training_data_unions_shadow_rows_when_present(
    fake_db, monkeypatch,
):
    # Disable v1 archive entirely so we exercise the live + shadow
    # branches only.
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION", "0")
    settled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(60):
        fake_db.shadow_buffer.append({
            "shadow_prediction_id": i + 1,
            "market_id": f"KX-NBA-{i}",
            "league": "NBA",
            "vertical": "sports",
            "predicted_prob": 0.7,
            "uncalibrated_prob": None,
            "actual_outcome": 1 if i % 2 == 0 else 0,
            "settled_at": settled_at,
        })
    grouped = asyncio.run(calibrators._fetch_training_data())
    assert "NBA" in grouped
    assert len(grouped["NBA"]["probs"]) == 60
    # Every row originated from the shadow branch.
    assert set(grouped["NBA"]["sources"]) == {"shadow"}


def test_fetch_training_data_gates_off_when_buffer_table_missing(
    fake_db, monkeypatch,
):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION", "0")
    fake_db.shadow_table_present = False
    # Even with rows in the buffer, the missing-table preflight
    # gates the whole CTE OUT.
    fake_db.shadow_buffer.append({
        "shadow_prediction_id": 1, "market_id": "X", "league": "NBA",
        "vertical": "sports", "predicted_prob": 0.7,
        "uncalibrated_prob": None, "actual_outcome": 1,
        "settled_at": datetime.now(timezone.utc),
    })
    grouped = asyncio.run(calibrators._fetch_training_data())
    # No live rows in the in-memory DB either, so the result is empty
    # — but crucially it doesn't raise.
    assert grouped == {}


def test_fetch_training_data_kill_switch_disables_shadow_union(
    fake_db, monkeypatch,
):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION", "0")
    monkeypatch.setenv("AXIOM_USE_SHADOW_BUFFER_FOR_CALIBRATION", "0")
    fake_db.shadow_buffer.append({
        "shadow_prediction_id": 1, "market_id": "X", "league": "NBA",
        "vertical": "sports", "predicted_prob": 0.7,
        "uncalibrated_prob": None, "actual_outcome": 1,
        "settled_at": datetime.now(timezone.utc),
    })
    grouped = asyncio.run(calibrators._fetch_training_data())
    assert grouped == {}  # shadow CTE was not composed in


# ---------------------------------------------------------------------------
# T006 — drift parity
# ---------------------------------------------------------------------------


def test_shadow_drift_helper_raises_alert_with_shadow_prefix(fake_db):
    # 60 buffer rows in the SPORTS vertical, all with predicted_prob
    # 0.9 vs actual_outcome 0 — gives Brier ~ 0.81, well above the
    # critical threshold (0.32).
    settled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(60):
        fake_db.shadow_buffer.append({
            "shadow_prediction_id": i + 1, "market_id": f"M{i}",
            "league": "NBA", "vertical": "sports",
            "predicted_prob": 0.9, "uncalibrated_prob": None,
            "actual_outcome": 0, "settled_at": settled_at,
        })
    raised = asyncio.run(_pipeline._calibration_and_drift_shadow("sports"))
    assert raised == 1
    # Exactly one calibration snapshot row was inserted for this
    # vertical (positional arg 0 == vertical name in the INSERT).
    snaps = [a for a in fake_db.calibration_snapshots if a and a[0] == "sports"]
    assert len(snaps) == 1
    # Alert message carries the [shadow] prefix.  The INSERT binds
    # ($1=vertical, $2=severity, $3=brier, $4=threshold, $5=message),
    # so positional indices are 0..4 (alert_type and metric_name are
    # hard-coded literals in the SQL).
    assert fake_db.alerts, "expected one alert insert"
    args = fake_db.alerts[-1]
    assert args[4].startswith("[shadow]")
    # Severity is critical given Brier ~0.81 >> 0.32.
    assert args[1] == "critical"


def test_shadow_drift_helper_skips_when_buffer_table_missing(fake_db):
    fake_db.shadow_table_present = False
    raised = asyncio.run(_pipeline._calibration_and_drift_shadow("sports"))
    assert raised == 0
    assert fake_db.calibration_snapshots == []
    assert fake_db.alerts == []


def test_shadow_buffer_insert_targets_partial_unique_index_predicate():
    """Regression guard: migration 0043 builds the unique index as
    ``WHERE shadow_prediction_id IS NOT NULL``.  Postgres requires the
    same predicate on the ``ON CONFLICT`` clause for the inference to
    pick up a partial unique index — without it, the INSERT raises
    ``no unique or exclusion constraint matching the ON CONFLICT
    specification`` at runtime and the buffer write silently fails.

    This test reads the settler source and asserts the predicate is
    paired with the conflict target so a future drive-by edit can't
    silently break the idempotency contract (the in-memory shim used
    by the rest of this suite cannot detect this class of SQL bug)."""
    import inspect
    from app.learning import shadow_settler

    src = inspect.getsource(shadow_settler._insert_buffer)
    norm = " ".join(src.split())
    assert "ON CONFLICT (shadow_prediction_id)" in norm
    assert (
        "ON CONFLICT (shadow_prediction_id) WHERE shadow_prediction_id IS NOT NULL"
        in norm
    ), (
        "shadow_settler._insert_buffer must include the partial-index "
        "predicate on its ON CONFLICT clause to match the unique index "
        "created by migration 0043"
    )
