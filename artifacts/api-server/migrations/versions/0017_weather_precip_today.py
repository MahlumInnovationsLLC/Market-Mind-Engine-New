"""Daily precipitation totals on ``weather_extended``.

Per task #279.  The KXRAIN precipitation predictor (task #255) settles
on *daily* accumulations, but the weather pollers only persisted the
*1-hour* precipitation rates from Open-Meteo and Tomorrow.io.  This
revision adds ``precip_today_in`` to ``weather_extended`` so each
provider can write its daily QPF (in inches) alongside the existing
1-hour rate.

The column is nullable so existing rows continue to load and pollers
that have not yet been redeployed simply leave it ``NULL`` — the
payload builder treats a missing daily value as "fall back to the
hourly rate" via ``precip_predictor._PRECIP_SOURCES``.

Revision ID: 0017_weather_precip_today
Revises: 0016_predictions_kalshi_candidates
"""
from __future__ import annotations

from alembic import op


revision = "0017_weather_precip_today"
down_revision = "0016_predictions_kalshi_candidates"
branch_labels = None
depends_on = None


_SQL = """
ALTER TABLE weather_extended
    ADD COLUMN IF NOT EXISTS precip_today_in double precision;
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    op.execute("ALTER TABLE weather_extended DROP COLUMN IF EXISTS precip_today_in")
