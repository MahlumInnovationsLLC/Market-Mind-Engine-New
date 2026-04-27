"""Tests for the one-shot crypto model-probability backfill (task #646).

Covers three layers:

  * The pure SQL handlers — exercises the rowcount parser, the
    idempotency guard, the persistent status snapshot, and the
    pre/post-run coverage tally — all against an in-memory fake DB.
  * ``app.orchestrator.cycle._persist_model_yes_prob`` — confirms the
    inline write is idempotent, validates inputs, and only ever fires
    a single UPDATE keyed on ``kalshi_market_id``.
  * The accuracy report's simplified SELECT — locks in that the
    lateral ``swarm_debates`` join is gone and the report now reads
    ``predictions.model_yes_prob`` directly.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.crypto import accuracy_report
from app.crypto import probability_backfill as backfill
from app.orchestrator import cycle as cycle_mod


# ---------------------------------------------------------------------------
# Tiny in-memory db substitute keyed on (kalshi_market_id) for predictions
# and (key) for system_settings.  Mirrors the shape used by
# ``test_entertainment_backfill`` so both backfills test against
# the same minimal fake.
# ---------------------------------------------------------------------------


class _FakeDB:
    """Records every SQL statement and routes the well-known queries
    used by the backfill onto an in-memory dict.

    The real backfill statement is a single UPDATE...FROM with a
    lateral join — too gnarly to model precisely without a real
    Postgres.  Instead the fake recognises the query by its leading
    keyword and applies a Python-side equivalent so the rest of the
    runner (rowcount parsing, status persistence, coverage tally) can
    still be exercised end-to-end.
    """

    def __init__(self) -> None:
        # Each prediction row: {
        #   "id", "league", "kalshi_market_id", "game_id", "settled_at",
        #   "model_yes_prob",
        # }
        self.predictions: list[dict[str, Any]] = []
        # Each debate: {"game_id", "kalshi_market_id",
        #               "consensus_probability", "created_at"}
        self.debates: list[dict[str, Any]] = []
        self.system_settings: dict[str, str] = {}
        self.executes: list[tuple[str, tuple[Any, ...]]] = []

    # --- helpers used by the tests -----------------------------------

    def add_prediction(self, **kw: Any) -> dict[str, Any]:
        kw.setdefault("id", len(self.predictions) + 1)
        kw.setdefault("model_yes_prob", None)
        kw.setdefault("settled_at", None)
        kw.setdefault("league", "CRYPTO")
        kw.setdefault("game_id", kw.get("kalshi_market_id"))
        self.predictions.append(kw)
        return kw

    def add_debate(self, **kw: Any) -> None:
        kw.setdefault("created_at", len(self.debates) + 1)
        self.debates.append(kw)

    # --- db driver surface -------------------------------------------

    async def execute(self, query: str, *args: Any) -> str:
        self.executes.append((query, args))
        q = " ".join(query.split()).lower()
        if q.startswith("update predictions p set model_yes_prob"):
            league = args[0]
            updated = 0
            # Mirror the lateral subquery: for each unscored prediction
            # in `league`, copy the latest matching debate's probability.
            for p in self.predictions:
                if p.get("model_yes_prob") is not None:
                    continue
                if p.get("league") != league:
                    continue
                match = self._latest_debate_for(p)
                if match is None:
                    continue
                p["model_yes_prob"] = match
                updated += 1
            return f"UPDATE {updated}"
        if q.startswith("update predictions set model_yes_prob"):
            # Inline cycle write: WHERE kalshi_market_id = $1 AND
            # model_yes_prob IS NULL.
            market_id, prob = args[0], args[1]
            updated = 0
            for p in self.predictions:
                if (
                    p.get("kalshi_market_id") == market_id
                    and p.get("model_yes_prob") is None
                ):
                    p["model_yes_prob"] = float(prob)
                    updated += 1
            return f"UPDATE {updated}"
        if q.startswith("insert into system_settings"):
            key, value = args[0], args[1]
            self.system_settings[key] = value
            return "INSERT 1"
        return "OK"

    async def fetchrow(
        self, query: str, *args: Any
    ) -> dict[str, Any] | None:
        q = " ".join(query.split()).lower()
        if q.startswith("select count(*) filter"):
            league = args[0]
            settled_total = 0
            settled_scored = 0
            settled_unscored = 0
            for p in self.predictions:
                if p.get("league") != league:
                    continue
                if p.get("settled_at") is None:
                    continue
                settled_total += 1
                if p.get("model_yes_prob") is None:
                    settled_unscored += 1
                else:
                    settled_scored += 1
            return {
                "settled_total": settled_total,
                "settled_scored": settled_scored,
                "settled_unscored": settled_unscored,
            }
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = " ".join(query.split()).lower()
        if q.startswith("select value from system_settings"):
            return self.system_settings.get(args[0])
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(query.split()).lower()
        if "from predictions p" in q and "p.league = $1" in q:
            league = args[0]
            window_hours = int(args[1])
            # Window filter is dropped in the fake — every settled row
            # is included; the tests pick a single window and verify
            # the SELECT returns the expected shape.
            del window_hours
            out: list[dict[str, Any]] = []
            for p in self.predictions:
                if p.get("league") != league:
                    continue
                if p.get("settled_at") is None:
                    continue
                ctx = p.get("game_context") or {}
                out.append({
                    "prediction_id": p["id"],
                    "market_id": p.get("kalshi_market_id"),
                    "outcome": p.get("outcome"),
                    "settle_price": p.get("settle_price"),
                    "settled_at": p.get("settled_at"),
                    "symbol": ctx.get("symbol", ""),
                    "timeframe": ctx.get("timeframe", ""),
                    "p_yes": p.get("model_yes_prob"),
                })
            return out
        return []

    def _latest_debate_for(self, pred: dict[str, Any]) -> float | None:
        candidates = [
            d for d in self.debates
            if (
                d.get("kalshi_market_id") == pred.get("kalshi_market_id")
                or d.get("game_id") == pred.get("game_id")
            ) and d.get("consensus_probability") is not None
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda d: d["created_at"], reverse=True)
        return float(candidates[0]["consensus_probability"])


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeDB:
    db = _FakeDB()
    monkeypatch.setattr(backfill.db, "execute", db.execute)
    monkeypatch.setattr(backfill.db, "fetchrow", db.fetchrow)
    monkeypatch.setattr(backfill.db, "fetchval", db.fetchval)
    monkeypatch.setattr(cycle_mod.db, "execute", db.execute)
    monkeypatch.setattr(accuracy_report.db, "fetch", db.fetch)
    backfill.reset_status_for_tests()
    return db


# ---------------------------------------------------------------------------
# Inline-write helper (cycle._persist_model_yes_prob)
# ---------------------------------------------------------------------------


def test_persist_model_yes_prob_writes_when_row_unscored(
    fake_db: _FakeDB,
) -> None:
    fake_db.add_prediction(
        kalshi_market_id="KXBTCD-1", model_yes_prob=None,
    )
    asyncio.run(cycle_mod._persist_model_yes_prob("KXBTCD-1", 0.62))
    assert fake_db.predictions[0]["model_yes_prob"] == pytest.approx(0.62)


def test_persist_model_yes_prob_is_idempotent(fake_db: _FakeDB) -> None:
    fake_db.add_prediction(
        kalshi_market_id="KXBTCD-1", model_yes_prob=0.4,
    )
    # Existing probability must NOT be overwritten — we record the
    # value from the *first* successful score, never a later cycle.
    asyncio.run(cycle_mod._persist_model_yes_prob("KXBTCD-1", 0.99))
    assert fake_db.predictions[0]["model_yes_prob"] == pytest.approx(0.4)


def test_persist_model_yes_prob_skips_invalid_inputs(
    fake_db: _FakeDB,
) -> None:
    fake_db.add_prediction(kalshi_market_id="KXBTCD-1", model_yes_prob=None)
    # Each invalid call must be a no-op (no UPDATE statement issued).
    for bad in (None, float("nan"), float("inf"), -0.1, 1.5, "not-a-number"):
        asyncio.run(cycle_mod._persist_model_yes_prob("KXBTCD-1", bad))
    # No UPDATE statements landed — only the intentional persists do.
    assert all(
        not q.lower().lstrip().startswith("update predictions")
        for q, _ in fake_db.executes
    )
    assert fake_db.predictions[0]["model_yes_prob"] is None


def test_persist_model_yes_prob_skips_when_market_id_missing(
    fake_db: _FakeDB,
) -> None:
    asyncio.run(cycle_mod._persist_model_yes_prob(None, 0.5))
    asyncio.run(cycle_mod._persist_model_yes_prob("", 0.5))
    assert fake_db.executes == []


# ---------------------------------------------------------------------------
# Backfill runner
# ---------------------------------------------------------------------------


def test_run_backfill_copies_latest_consensus_per_market(
    fake_db: _FakeDB,
) -> None:
    # Two settled predictions, both unscored.
    fake_db.add_prediction(
        kalshi_market_id="KXBTCD-1", settled_at="2026-04-01",
    )
    fake_db.add_prediction(
        kalshi_market_id="KXETHD-1", settled_at="2026-04-01",
    )
    # Stale + fresh debate per market — backfill takes the freshest.
    fake_db.add_debate(
        kalshi_market_id="KXBTCD-1", consensus_probability=0.4,
    )
    fake_db.add_debate(
        kalshi_market_id="KXBTCD-1", consensus_probability=0.7,
    )
    fake_db.add_debate(
        kalshi_market_id="KXETHD-1", consensus_probability=0.55,
    )
    snap = asyncio.run(backfill.run_backfill())
    assert snap["state"] == "completed"
    assert snap["rows_updated"] == 2
    by_id = {p["kalshi_market_id"]: p for p in fake_db.predictions}
    assert by_id["KXBTCD-1"]["model_yes_prob"] == pytest.approx(0.7)
    assert by_id["KXETHD-1"]["model_yes_prob"] == pytest.approx(0.55)


def test_run_backfill_resolves_via_game_id_when_no_market_id_match(
    fake_db: _FakeDB,
) -> None:
    """Legacy debates persisted before the
    ``game_context.kalshi_market_id`` linkage convention used the game_id
    heuristic instead — backfill must still resolve them."""
    fake_db.add_prediction(
        kalshi_market_id="KXBTCD-LEGACY",
        game_id="KXBTCD-LEGACY",
        settled_at="2026-04-01",
    )
    fake_db.add_debate(
        # No kalshi_market_id linkage — only the game_id match works.
        game_id="KXBTCD-LEGACY",
        consensus_probability=0.66,
    )
    snap = asyncio.run(backfill.run_backfill())
    assert snap["rows_updated"] == 1
    assert fake_db.predictions[0]["model_yes_prob"] == pytest.approx(0.66)


def test_run_backfill_skips_already_scored_rows(fake_db: _FakeDB) -> None:
    fake_db.add_prediction(
        kalshi_market_id="KXBTCD-1",
        settled_at="2026-04-01",
        model_yes_prob=0.42,  # already scored — keep as-is
    )
    fake_db.add_debate(
        kalshi_market_id="KXBTCD-1", consensus_probability=0.99,
    )
    snap = asyncio.run(backfill.run_backfill())
    assert snap["rows_updated"] == 0
    assert fake_db.predictions[0]["model_yes_prob"] == pytest.approx(0.42)


def test_run_backfill_records_pre_and_post_coverage(
    fake_db: _FakeDB,
) -> None:
    fake_db.add_prediction(
        kalshi_market_id="KXBTCD-1", settled_at="2026-04-01",
    )
    fake_db.add_prediction(
        kalshi_market_id="KXBTCD-2", settled_at="2026-04-01",
    )
    # Only one of them has a debate, so coverage_after still has a gap.
    fake_db.add_debate(
        kalshi_market_id="KXBTCD-1", consensus_probability=0.5,
    )
    snap = asyncio.run(backfill.run_backfill())
    assert snap["coverage_before"] == {
        "settled_total": 2, "settled_scored": 0, "settled_unscored": 2,
    }
    assert snap["coverage_after"] == {
        "settled_total": 2, "settled_scored": 1, "settled_unscored": 1,
    }


def test_run_backfill_persists_status_snapshot(fake_db: _FakeDB) -> None:
    fake_db.add_prediction(
        kalshi_market_id="KXBTCD-1", settled_at="2026-04-01",
    )
    fake_db.add_debate(
        kalshi_market_id="KXBTCD-1", consensus_probability=0.5,
    )
    asyncio.run(backfill.run_backfill())
    raw = fake_db.system_settings.get(backfill._STATUS_KEY)
    assert raw is not None
    snap = json.loads(raw)
    assert snap["state"] == "completed"
    assert snap["rows_updated"] == 1
    # The persisted snapshot survives a process restart — load it back
    # via the public API and confirm the round trip.
    loaded = asyncio.run(backfill.load_persisted_status())
    assert loaded == snap


def test_run_backfill_marks_state_failed_on_update_crash(
    fake_db: _FakeDB, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(query: str, *args: Any) -> str:
        if "update predictions p" in " ".join(query.split()).lower():
            raise RuntimeError("db gone")
        return await fake_db.execute(query, *args)

    monkeypatch.setattr(backfill.db, "execute", boom)
    snap = asyncio.run(backfill.run_backfill())
    assert snap["state"] == "failed"
    assert "db gone" in (snap["last_error"] or "")
    assert snap["rows_updated"] == 0


def test_run_backfill_normalises_league_input(fake_db: _FakeDB) -> None:
    fake_db.add_prediction(
        kalshi_market_id="KXBTCD-1", settled_at="2026-04-01",
    )
    fake_db.add_debate(
        kalshi_market_id="KXBTCD-1", consensus_probability=0.5,
    )
    snap = asyncio.run(backfill.run_backfill(league="crypto"))
    assert snap["league"] == "CRYPTO"
    assert snap["rows_updated"] == 1


def test_parse_rowcount_handles_int_and_command_tag() -> None:
    assert backfill._parse_rowcount("UPDATE 7") == 7
    assert backfill._parse_rowcount("UPDATE bogus") == 0
    assert backfill._parse_rowcount(7) == 7
    assert backfill._parse_rowcount(None) == 0


# ---------------------------------------------------------------------------
# Accuracy report SQL — locks in the join removal
# ---------------------------------------------------------------------------


def test_accuracy_report_sql_no_longer_joins_swarm_debates() -> None:
    """The whole point of task #646 was to drop the lateral join.  If
    a future refactor accidentally puts it back, this test fires
    immediately so the regression is caught at CI time instead of
    silently doubling DB cost on the report endpoint."""
    sql = accuracy_report._WINDOW_SQL.lower()
    assert "swarm_debates" not in sql
    assert "lateral" not in sql
    # The new SELECT must read the inline column directly.
    assert "p.model_yes_prob" in sql


def test_evaluate_window_reads_inline_model_yes_prob(
    fake_db: _FakeDB,
) -> None:
    """End-to-end: stamp the inline probability, settle the row, ask
    the report for it, confirm the row is *scored* (not unscored)."""
    fake_db.add_prediction(
        kalshi_market_id="KXBTCD-1",
        settled_at="2026-04-01",
        outcome="yes",
        settle_price=1.0,
        model_yes_prob=0.8,
        game_context={"symbol": "BTC", "timeframe": "1h"},
    )
    rep = asyncio.run(accuracy_report.evaluate_window(24))
    assert rep.n_settled == 1
    assert rep.n_unscored == 0
    assert rep.overall.n == 1
    assert rep.overall.accuracy == pytest.approx(1.0)
    assert rep.overall.brier == pytest.approx((0.8 - 1.0) ** 2)
