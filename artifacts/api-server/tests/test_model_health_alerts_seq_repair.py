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
    """The migration must run ``setval(..., GREATEST(MAX(id), 1), true)``."""
    mod = _load_migration_module()
    sql = mod._SQL_UP
    # The exact sequence name the table's serial default points at.
    assert "model_health_alerts_id_seq" in sql
    # Use GREATEST so an empty table (MAX(id) IS NULL → coalesce 0)
    # doesn't issue setval(0) — that's an error in Postgres.
    assert "GREATEST" in sql
    assert "COALESCE(MAX(id), 0)" in sql
    # is_called=true so the *next* nextval advances by one (matches
    # serial-column semantics).
    assert re.search(r"setval\(.*?,\s*true\s*\)", sql, re.DOTALL), sql
    # Idempotent: guarded by both table and sequence existence checks
    # so a fresh DB is a no-op.
    assert "information_schema.tables" in sql
    assert "information_schema.sequences" in sql


def test_migration_downgrade_refuses() -> None:
    """Rolling the sequence backwards would re-introduce the bug."""
    mod = _load_migration_module()
    with pytest.raises(NotImplementedError):
        mod.downgrade()


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
