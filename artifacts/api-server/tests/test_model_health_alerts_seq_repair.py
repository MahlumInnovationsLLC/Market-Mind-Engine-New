"""Regression coverage for task #576.

When ``model_health_alerts.id`` (a regular ``serial``) drifts from
``model_health_alerts_id_seq.last_value`` — typically because rows
were bulk-loaded with explicit ids without a follow-up ``setval()`` —
every subsequent default-id INSERT collides on the primary key and
the reconciler / settlement / persona-brier alerters spam the log.

Two layers of defence are tested here:

1. The Alembic migration ``0023_model_health_alerts_seq_repair``
   issues the canonical ``setval()`` recipe so the next ``nextval()``
   returns ``MAX(id)+1``.
2. Every ``INSERT INTO model_health_alerts`` call site appends
   ``ON CONFLICT (id) DO NOTHING`` so a *future* drift event (e.g. a
   manual operator INSERT during an incident) degrades to a silent
   skip instead of an aborted transaction.

Both layers are unit-tested without opening a real Postgres pool —
the migration's SQL is parsed for the expected ``setval`` shape, and
each call site is grepped for the ``ON CONFLICT`` clause attached to
the ``INSERT INTO model_health_alerts`` statement.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Layer 1 — the Alembic migration itself.
# ---------------------------------------------------------------------------


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "_mig_0023",
        _REPO_ROOT / "migrations" / "versions"
        / "0023_model_health_alerts_seq_repair.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_revision_chain() -> None:
    """The new migration must merge both 0022 heads (uw signals + crypto
    settlement) back into a single chain — listing only one would leave
    the other as a dangling head and break ``alembic upgrade head``."""
    mod = _load_migration_module()
    assert mod.revision == "0023_model_health_alerts_seq_repair"
    assert mod.branch_labels is None
    # Tuple form is required for a merge; both parents must be present.
    assert isinstance(mod.down_revision, tuple), (
        f"down_revision must be a tuple to merge the two 0022 heads, "
        f"got {mod.down_revision!r}"
    )
    assert set(mod.down_revision) == {
        "0022_uw_signals_ticker_backfill",
        "0022_predictions_settlement",
    }


def test_migration_uses_canonical_setval_recipe() -> None:
    """The migration must run
    ``setval(..., GREATEST(MAX(id)+1, 1), false)``.

    The ``+1`` / ``is_called=false`` form is the one specified by the
    task plan: it makes ``last_value > MAX(id)`` true immediately
    after the migration (so operators can verify the repair on the
    spot) while still arranging for the next ``nextval()`` to return
    exactly ``MAX(id)+1``.
    """
    mod = _load_migration_module()
    sql = mod._SQL_UP
    # The exact sequence name the table's serial default points at.
    assert "model_health_alerts_id_seq" in sql
    # Use GREATEST so an empty table (MAX(id) IS NULL → coalesce 0,
    # then +1 = 1) safely sets last_value to 1 without erroring.
    assert "GREATEST" in sql
    # ``+ 1`` is the post-condition guarantee: last_value > MAX(id).
    # Allow for a ``FROM model_health_alerts`` subquery between the
    # COALESCE and the ``+ 1`` (the actual SQL form), but require both
    # pieces to be present.
    assert re.search(
        r"COALESCE\(MAX\(id\),\s*0\).*?\+\s*1",
        sql,
        re.DOTALL,
    ), sql
    # is_called=false so the *next* nextval returns last_value itself
    # (which is MAX(id)+1) instead of last_value+1.  Combined with the
    # +1 above, the first post-migration insert lands at MAX(id)+1.
    assert re.search(r"setval\(.*?,\s*false\s*\)", sql, re.DOTALL), sql
    # Idempotent: guarded by both table and sequence existence checks
    # so a fresh DB is a no-op.
    assert "information_schema.tables" in sql
    assert "information_schema.sequences" in sql


def test_migration_downgrade_is_noop() -> None:
    """Per the task plan, downgrade is a no-op — rolling the sequence
    backwards would re-introduce the very collision this fixed.
    The migration must *not* raise; alembic stamps the downgrade as
    successful and leaves the sequence ahead.
    """
    mod = _load_migration_module()
    # Should not raise.  Returns None.
    assert mod.downgrade() is None


# ---------------------------------------------------------------------------
# Layer 2 — every call site is hardened with ON CONFLICT (id) DO NOTHING.
# ---------------------------------------------------------------------------


# (file relative to api-server root, human label).  Keep this list in
# sync with ``rg 'INSERT INTO model_health_alerts' app/`` — any new
# alerter that lands without the conflict clause will fail this test.
_CALL_SITES: tuple[tuple[str, str], ...] = (
    ("app/kalshi/reconciler.py",                 "kalshi position_drift"),
    ("app/settlement/retrain.py",                "retrain rollback"),
    ("app/settlement/pipeline.py",               "settlement brier_drift"),
    ("app/notifications/persona_brier_alerts.py", "persona daily brier"),
    ("app/crypto/backtest_scheduler.py",         "crypto backtest drift"),
)


@pytest.mark.parametrize("rel_path,label", _CALL_SITES)
def test_call_site_uses_on_conflict_do_nothing(rel_path: str, label: str) -> None:
    text = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    # Every INSERT block in the file must carry the conflict clause.
    # We split on the literal "INSERT INTO model_health_alerts" and
    # examine each suffix up to the closing triple-quote / next
    # statement boundary.
    chunks = text.split("INSERT INTO model_health_alerts")
    assert len(chunks) >= 2, (
        f"{rel_path} no longer references model_health_alerts — update the "
        f"call-site list in this test."
    )
    for i, suffix in enumerate(chunks[1:], start=1):
        # Look at the next ~400 chars after the INSERT marker — that
        # spans the column list, VALUES tuple, and any ON CONFLICT
        # clause without bleeding into the next SQL statement.
        window = suffix[:400]
        assert "ON CONFLICT (id) DO NOTHING" in window, (
            f"{rel_path} ({label}) INSERT #{i} is missing "
            f"`ON CONFLICT (id) DO NOTHING` — sequence drift would "
            f"re-flood the log.\n--- window ---\n{window}\n--- end ---"
        )


def test_kalshi_reconciler_has_two_insert_sites() -> None:
    """Belt-and-suspenders: the reconciler has *two* INSERT call sites
    (the warn-level position_drift alert and the info-level
    auto-correction audit).  If a future refactor collapses them into
    one helper that bypasses the conflict clause, this test catches it.
    """
    text = (_REPO_ROOT / "app" / "kalshi" / "reconciler.py").read_text(
        encoding="utf-8"
    )
    assert text.count("INSERT INTO model_health_alerts") == 2
    assert text.count("ON CONFLICT (id) DO NOTHING") >= 2


# ---------------------------------------------------------------------------
# Layer 3 — behavioral simulation of the bug + the fix.
# ---------------------------------------------------------------------------
#
# These tests replace the asyncpg pool with an in-memory simulator that
# mirrors the Postgres semantics this fix relies on:
#
#   * a serial column whose default is ``nextval(seq)``
#   * ``setval(seq, n, is_called)`` writes ``last_value=n`` and arranges
#     for the next ``nextval`` to return ``n`` (when is_called=false) or
#     ``n+1`` (when is_called=true)
#   * ``INSERT … ON CONFLICT (id) DO NOTHING`` returns silently when the
#     id already exists, instead of raising
#     ``asyncpg.exceptions.UniqueViolationError`` like a vanilla INSERT
#
# Together they reproduce the exact sequence-drift scenario observed in
# task #576 and demonstrate that:
#
#   (a) without the fix, an alerter helper crashes loudly on the
#       collision (proving the bug exists), and
#   (b) with the fix (ON CONFLICT clause + setval repair), the same
#       helper completes silently and the next insert lands at a fresh
#       id past MAX(id) (proving the fix works at runtime).


import asyncio
from typing import Any
from unittest.mock import patch


class _FakeSeqDB:
    """Minimal asyncpg-shaped simulator of ``model_health_alerts``."""

    def __init__(self) -> None:
        # rows: list of dicts keyed by (id, alert_type, metric_name, ...)
        self.rows: list[dict[str, Any]] = []
        self.seq_last_value: int = 1
        self.seq_is_called: bool = False
        self.executed_queries: list[tuple[str, tuple[Any, ...]]] = []

    # ------------------------------------------------------------------
    # Sequence helpers — Postgres-faithful semantics.
    # ------------------------------------------------------------------
    def _nextval(self) -> int:
        if self.seq_is_called:
            self.seq_last_value += 1
        else:
            # First call after setval(..., false) returns last_value as-is.
            self.seq_is_called = True
        return self.seq_last_value

    def _setval(self, n: int, is_called: bool) -> None:
        self.seq_last_value = max(int(n), 1)
        self.seq_is_called = bool(is_called)

    def seed_with_explicit_ids(self, *ids: int) -> None:
        """Bulk-load rows with hand-picked ids — does NOT call setval()
        afterwards.  This is the exact failure mode that produced the
        production sequence drift.
        """
        for i in ids:
            self.rows.append({"id": int(i), "alert_type": "seeded"})

    def max_id(self) -> int:
        return max((r["id"] for r in self.rows), default=0)

    # ------------------------------------------------------------------
    # asyncpg-shaped execute / fetchrow.
    # ------------------------------------------------------------------
    async def execute(self, query: str, *args: Any) -> str:
        self.executed_queries.append((query, args))
        if "INSERT INTO model_health_alerts" in query:
            new_id = self._nextval()
            collides = any(r["id"] == new_id for r in self.rows)
            if collides:
                if "ON CONFLICT (id) DO NOTHING" in query:
                    # Postgres swallows the collision silently.
                    return "INSERT 0 0"
                # Vanilla INSERT — emulate the asyncpg exception.
                import asyncpg  # local import keeps the test light
                raise asyncpg.exceptions.UniqueViolationError(
                    f'duplicate key value violates unique constraint '
                    f'"model_health_alerts_pkey" — DETAIL: '
                    f'Key (id)=({new_id}) already exists.'
                )
            self.rows.append({"id": new_id, "alert_type": "inserted"})
            return "INSERT 0 1"
        # Anything else — just record and ack.
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        # The reconciler dedupe lookup expects no recent row → return None.
        return None


@pytest.fixture
def fake_seq_db(monkeypatch: pytest.MonkeyPatch) -> _FakeSeqDB:
    db = _FakeSeqDB()
    from app import db as _appdb
    monkeypatch.setattr(_appdb, "execute", db.execute)
    monkeypatch.setattr(_appdb, "fetchrow", db.fetchrow)
    return db


def test_simulator_reproduces_sequence_drift_bug(fake_seq_db: _FakeSeqDB) -> None:
    """Sanity check: the in-memory shim faithfully reproduces the
    production failure mode — bulk-loaded rows + lagging sequence
    causes the next default-id INSERT to raise UniqueViolation.
    """
    import asyncpg

    # Pre-seed the way the production DB ended up: rows at 1..1577
    # but the sequence's last_value still at 827 (lagging).
    fake_seq_db.seed_with_explicit_ids(*range(1, 11))  # 10 rows
    fake_seq_db._setval(5, True)  # sequence behind: last_value=5, MAX(id)=10

    async def go() -> None:
        # A vanilla INSERT (no ON CONFLICT) must crash the way prod did.
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await fake_seq_db.execute(
                "INSERT INTO model_health_alerts (vertical) VALUES ($1)",
                "trading",
            )

    asyncio.run(go())


def test_on_conflict_clause_swallows_collision(fake_seq_db: _FakeSeqDB) -> None:
    """With the fix's ``ON CONFLICT (id) DO NOTHING`` clause, the same
    collision degrades to a silent no-op instead of an exception.
    """
    fake_seq_db.seed_with_explicit_ids(*range(1, 11))
    fake_seq_db._setval(5, True)  # next nextval = 6, which collides.

    async def go() -> None:
        # Identical to the prod-style INSERT but with the conflict clause.
        result = await fake_seq_db.execute(
            "INSERT INTO model_health_alerts (vertical) "
            "VALUES ($1) ON CONFLICT (id) DO NOTHING",
            "trading",
        )
        # Postgres returns "INSERT 0 0" for a swallowed conflict.
        assert "0 0" in result
        # Row count unchanged — the collision did NOT silently overwrite.
        assert len(fake_seq_db.rows) == 10

    asyncio.run(go())


def test_setval_repair_recipe_clears_drift(fake_seq_db: _FakeSeqDB) -> None:
    """The migration's setval recipe must restore the
    ``last_value > MAX(id)`` invariant so subsequent default-id
    inserts land at fresh ids without colliding.
    """
    fake_seq_db.seed_with_explicit_ids(*range(1, 11))   # MAX(id) = 10
    fake_seq_db._setval(5, True)                        # last_value = 5 (drift)
    assert fake_seq_db.seq_last_value < fake_seq_db.max_id()  # bug present

    # Apply the migration's exact recipe:
    #   GREATEST(COALESCE(MAX(id), 0) + 1, 1), is_called=false
    fake_seq_db._setval(max(fake_seq_db.max_id() + 1, 1), False)

    # Post-condition the task plan asks for: last_value > MAX(id).
    assert fake_seq_db.seq_last_value > fake_seq_db.max_id()
    # And exactly equal to MAX(id)+1 (the next nextval will land here).
    assert fake_seq_db.seq_last_value == fake_seq_db.max_id() + 1

    async def go() -> None:
        # The very next default-id INSERT must succeed at id=11.
        result = await fake_seq_db.execute(
            "INSERT INTO model_health_alerts (vertical) "
            "VALUES ($1) ON CONFLICT (id) DO NOTHING",
            "trading",
        )
        assert "0 1" in result, "first post-repair insert should land cleanly"
        new_ids = sorted(r["id"] for r in fake_seq_db.rows)
        assert new_ids[-1] == 11, f"expected id=11, got {new_ids[-1]}"

    asyncio.run(go())


def test_reconciler_helper_does_not_crash_on_collision(
    fake_seq_db: _FakeSeqDB,
) -> None:
    """End-to-end behavioral coverage: invoke the actual reconciler
    audit helper (the info-level auto-correction insert at
    ``app/kalshi/reconciler.py:_record_auto_correction``) under
    drifted-sequence conditions, and assert (a) no exception bubbles
    up and (b) the SQL the helper sent contained the conflict clause.
    """
    fake_seq_db.seed_with_explicit_ids(*range(1, 11))
    fake_seq_db._setval(5, True)  # collision guaranteed on first insert

    from app.kalshi import reconciler as _rec

    async def go() -> None:
        # _record_auto_correction is the info-level audit INSERT site.
        # It pulls ``db`` from the module-scope import.
        await _rec._record_auto_correction(
            ticker="MKT-TEST",
            local_qty=10,
            exchange_qty=11,
            drift=1,
            tolerance=2,
        )

    # Must NOT raise even though the underlying sequence is drifted.
    asyncio.run(go())

    # Must have attempted exactly one INSERT, and it must carry the
    # conflict clause.
    inserts = [
        q for q, _ in fake_seq_db.executed_queries
        if "INSERT INTO model_health_alerts" in q
    ]
    assert len(inserts) == 1, f"expected 1 audit insert, got {len(inserts)}"
    assert "ON CONFLICT (id) DO NOTHING" in inserts[0]
