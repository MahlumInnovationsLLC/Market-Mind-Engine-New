"""Merge the two divergent migration heads into one.

The migration DAG had two independent heads:

  * ``0048_position_drift_resolutions_exchange``
  * ``0065_trades_placed_by_edge_terminal`` (chained off the 006x line)

With two heads, the startup ``command.upgrade(cfg, "head")`` in
``app/migrations.py`` fails with *"Multiple head revisions are
present"* on every boot (caught and logged, but it leaves the readyz
``migrations`` check perpetually not-ok and the schema effectively
managed out-of-band).  This is a no-op merge revision that unifies the
two lineages so ``alembic upgrade head`` resolves to a single target
again — no schema changes, just DAG topology.
"""
from __future__ import annotations


revision = "0066_merge_heads"
down_revision = (
    "0048_position_drift_resolutions_exchange",
    "0065_trades_placed_by_edge_terminal",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
