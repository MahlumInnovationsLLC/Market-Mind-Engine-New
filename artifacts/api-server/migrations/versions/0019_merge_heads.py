"""Merge the two parallel 0018 heads.

Both ``0018_lazy_ddl_relocation`` (task #357) and
``0018_market_price_ticks`` (task #359) descend from the same fan-out
at 0017 but were never merged, leaving Alembic with two heads.  The
boot-time ``alembic upgrade head`` call therefore raises
``MultipleHeads`` BEFORE any 0003+ migration has a chance to run,
which is the upstream cause of every "column does not exist /
relation does not exist" warning the API server has been emitting
(crypto_ohlcv.granularity, bankroll_events.exchange,
swarm_fallback_monitor.created_at, entertainment_signals,
free_tier_market_views, ...).  Merging the heads is a no-op DDL
revision; it simply lets the upgrade walker continue past 0018.
"""
from __future__ import annotations


revision = "0019_merge_heads"
down_revision = ("0018_lazy_ddl_relocation", "0018_market_price_ticks")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
