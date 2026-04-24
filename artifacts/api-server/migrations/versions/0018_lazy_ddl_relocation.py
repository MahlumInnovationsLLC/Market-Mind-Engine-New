"""Move the remaining lazy ``ensure_schema`` helpers into Alembic.

Task #357 — extends the cleanup begun in #134 by relocating the rest
of the request-time ``CREATE TABLE IF NOT EXISTS`` /
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` helpers into a single
numbered revision.  After this migration runs, the following modules
no longer need to bootstrap their own tables:

* ``app/runtime_settings.py``  — ``runtime_settings``, ``system_settings``
* ``app/notifications/push.py`` — ``expo_push_tokens``,
  ``user_notification_prefs`` (incl. every per-user toggle column the
  old ALTER chain accumulated), ``mobile_notification_log``
* ``app/feeds/sportradar.py`` — ``sportradar_schedules``,
  ``sportradar_rosters``, ``sportradar_injuries``,
  ``sportradar_live_state``
* ``app/feeds/weather_climatology.py`` — ``weather_climatology``
* ``app/elo/__init__.py`` — ``team_elo``, ``team_elo_history``

``swarm_persona_toggles`` was already covered by 0005; the helper +
call sites in ``app/swarm/toggles.py`` are simply deleted in the same
patch (no SQL needed here).

Every statement uses ``IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS``
so the migration is safe to apply against the existing production
database where the lazy helpers have already created these objects.

Per repo convention (see #134), no destructive down-migration is
provided.

Revision ID: 0018_lazy_ddl_relocation
Revises: 0017_precip_emos_observations, 0017_weather_precip_today,
         0017_weather_temp_trained_models
"""
from __future__ import annotations

from alembic import op


revision = "0018_lazy_ddl_relocation"
down_revision = (
    "0017_precip_emos_observations",
    "0017_weather_precip_today",
    "0017_weather_temp_trained_models",
)
branch_labels = None
depends_on = None


_SQL = """
-- ---------------------------------------------------------------------
-- runtime_settings + system_settings
-- (was app/runtime_settings.py::ensure_schema /
--      ::_ensure_system_settings_schema)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runtime_settings (
    key         text PRIMARY KEY,
    value       text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text
);

CREATE TABLE IF NOT EXISTS system_settings (
    key         text PRIMARY KEY,
    value       text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Push notification tables (was app/notifications/push.py::ensure_schema).
-- The ALTER chain that accumulated per-user toggle columns over many
-- releases is folded directly into the CREATE TABLE column list so a
-- fresh database lands the full shape in one statement; ADD COLUMN IF
-- NOT EXISTS still runs below so an existing prod database that only
-- has the early columns gets the rest.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS expo_push_tokens (
    expo_token      text PRIMARY KEY,
    clerk_user_id   text NOT NULL,
    platform        text,
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS expo_push_tokens_user_idx
    ON expo_push_tokens (clerk_user_id);

CREATE TABLE IF NOT EXISTS user_notification_prefs (
    clerk_user_id     text PRIMARY KEY,
    notify_live_trade boolean NOT NULL DEFAULT true,
    notify_halt       boolean NOT NULL DEFAULT true,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    email_daily_summary       boolean NOT NULL DEFAULT true,
    email_drift               boolean NOT NULL DEFAULT true,
    email_halt                boolean NOT NULL DEFAULT true,
    email_live_trade          boolean NOT NULL DEFAULT false,
    email_weekly_roundup      boolean NOT NULL DEFAULT true,
    email_trade_confirmation  boolean NOT NULL DEFAULT true,
    digest_partials           boolean NOT NULL DEFAULT false,
    sms_live_trade            boolean NOT NULL DEFAULT false,
    telegram_live_trade       boolean NOT NULL DEFAULT false,
    sms_phone_number          text,
    quiet_hours_start_hour    smallint,
    quiet_hours_end_hour      smallint,
    quiet_hours_tz            text,
    notify_settlement         boolean NOT NULL DEFAULT true,
    sms_phone_verified_at             timestamptz,
    sms_phone_verification_code       text,
    sms_phone_verification_expires_at timestamptz
);

ALTER TABLE user_notification_prefs
    ADD COLUMN IF NOT EXISTS email_daily_summary       boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS email_drift               boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS email_halt                boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS email_live_trade          boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS email_weekly_roundup      boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS email_trade_confirmation  boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS digest_partials           boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS sms_live_trade            boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS telegram_live_trade       boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS sms_phone_number          text,
    ADD COLUMN IF NOT EXISTS quiet_hours_start_hour    smallint,
    ADD COLUMN IF NOT EXISTS quiet_hours_end_hour      smallint,
    ADD COLUMN IF NOT EXISTS quiet_hours_tz            text,
    ADD COLUMN IF NOT EXISTS notify_settlement         boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS sms_phone_verified_at             timestamptz,
    ADD COLUMN IF NOT EXISTS sms_phone_verification_code       text,
    ADD COLUMN IF NOT EXISTS sms_phone_verification_expires_at timestamptz;

CREATE TABLE IF NOT EXISTS mobile_notification_log (
    id              bigserial PRIMARY KEY,
    clerk_user_id   text NOT NULL,
    type            text NOT NULL,
    title           text NOT NULL,
    body            text NOT NULL,
    deep_link       text,
    data            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    read_at         timestamptz
);

CREATE INDEX IF NOT EXISTS mobile_notification_log_user_idx
    ON mobile_notification_log (clerk_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS mobile_notification_log_unread_idx
    ON mobile_notification_log (clerk_user_id)
    WHERE read_at IS NULL;

-- ---------------------------------------------------------------------
-- Sportradar caches (was app/feeds/sportradar.py::ensure_schema).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sportradar_schedules (
    league       text        NOT NULL,
    game_id      text        NOT NULL,
    home_team    text,
    away_team    text,
    scheduled    timestamptz,
    status       text,
    venue        text,
    payload      jsonb,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id)
);
CREATE INDEX IF NOT EXISTS sportradar_schedules_when_idx
    ON sportradar_schedules (league, scheduled);

CREATE TABLE IF NOT EXISTS sportradar_rosters (
    league       text        NOT NULL,
    team_id      text        NOT NULL,
    team_name    text,
    player_id    text        NOT NULL,
    player_name  text,
    position     text,
    jersey       text,
    status       text,
    height       text,
    weight       text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, team_id, player_id)
);
CREATE INDEX IF NOT EXISTS sportradar_rosters_team_idx
    ON sportradar_rosters (league, team_id);

CREATE TABLE IF NOT EXISTS sportradar_injuries (
    league       text        NOT NULL,
    player_id    text        NOT NULL,
    player_name  text,
    team_id      text,
    team_name    text,
    position     text,
    status       text,
    description  text,
    updated_at   timestamptz,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, player_id)
);
CREATE INDEX IF NOT EXISTS sportradar_injuries_team_idx
    ON sportradar_injuries (league, team_id);

CREATE TABLE IF NOT EXISTS sportradar_live_state (
    league       text        NOT NULL,
    game_id      text        NOT NULL,
    status       text,
    home_score   integer,
    away_score   integer,
    period       text,
    clock        text,
    scheduled    timestamptz,
    payload      jsonb,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id)
);
CREATE INDEX IF NOT EXISTS sportradar_live_state_status_idx
    ON sportradar_live_state (league, status);

-- ---------------------------------------------------------------------
-- Weather climatology baselines
-- (was app/feeds/weather_climatology.py::ensure_schema).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS weather_climatology (
    location      text        NOT NULL,
    day_of_year   smallint    NOT NULL,
    metric        text        NOT NULL,
    value_f       double precision NOT NULL,
    source        text        NOT NULL DEFAULT 'sinusoidal_baseline',
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (location, day_of_year, metric),
    CHECK (day_of_year BETWEEN 1 AND 366)
);
CREATE INDEX IF NOT EXISTS weather_climatology_lookup_idx
    ON weather_climatology (location, day_of_year);

-- ---------------------------------------------------------------------
-- Internal team Elo rating engine (was app/elo/__init__.py::ensure_schema).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS team_elo (
    league         text        NOT NULL,
    team           text        NOT NULL,
    rating         real        NOT NULL DEFAULT 1500.0,
    games_played   int         NOT NULL DEFAULT 0,
    last_updated   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, team)
);

CREATE TABLE IF NOT EXISTS team_elo_history (
    id                       bigserial   PRIMARY KEY,
    league                   text        NOT NULL,
    team                     text        NOT NULL,
    opponent                 text        NOT NULL,
    game_id                  text        NOT NULL,
    played_at                timestamptz NOT NULL,
    rating_before            real        NOT NULL,
    rating_after             real        NOT NULL,
    opponent_rating_before   real        NOT NULL,
    k_factor                 real        NOT NULL,
    score                    real        NOT NULL,
    expected                 real        NOT NULL,
    is_home                  boolean     NOT NULL,
    created_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE (league, team, game_id)
);

CREATE INDEX IF NOT EXISTS team_elo_history_team_played_idx
    ON team_elo_history (league, team, played_at DESC);
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    # Out of scope per task spec — destructive down-migrations are not
    # provided for relocated lazy-DDL revisions (see 0001_baseline).
    raise NotImplementedError(
        "destructive down-migration not provided for 0018_lazy_ddl_relocation"
    )
