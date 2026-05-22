"""In-game live prediction loop (task #830).

Schedule-driven re-evaluator for open sports markets while their
underlying game is in-progress.  Mirrors the shape of
:mod:`app.learning.shadow_predictor`:

  * A single :func:`run_in_game_cycle` entrypoint per sport that the
    scheduler ticks on a per-sport cadence.
  * Per-sport cadence floor read from ``runtime_settings`` under
    ``risk.live_loop_cadence_<sport>`` (seconds) so the operator can
    bump or throttle a single league without redeploy.
  * Per-sport enable flag at ``risk.live_loop_enabled_<sport>`` —
    **default False**.  This ships per-league disabled until the
    shadow Brier for the league is within 0.02 of the pre-game model
    (the validation rollout sequence is documented in the task
    brief; the per-league flip happens via the operator console once
    the criterion is met).
  * Idempotency keyed on ``(market_id, minute_bucket)`` so a
    same-minute re-run is a no-op.
  * In-memory ``_LAST_RUN_AT`` map enforces the cadence floor without
    needing a separate scheduler-side lock.

This module deliberately does NOT itself call the swarm / predictor
chain — that path is large enough that each sport's live feature
builder lands as its own follow-up task (#831/#832 + per-sport).
What this module DOES do:

  1. Enumerate open ``predictions`` rows whose league belongs to the
     requested sport and whose game is currently in ``sportradar_live_state``
     with a status in ``_LIVE_STATUSES``.
  2. Tag each row's underlying ``predictions.is_live`` to ``true``
     (monotonic — the column never flips back).
  3. Return a structured result the scheduler / dashboard can render
     so operators can see the live loop is actually doing work even
     before the per-sport prediction wiring lands.

When the per-sport prediction wiring DOES land it will plug into
``_predict_live_for_market`` (the stub at the bottom of this file)
and the orchestrator will route the updated ``model_yes_prob``
through the same ``RiskContext`` (with ``is_live=True``) so the
new :mod:`app.risk.gates` ``stale_live_state`` and
``live_kelly_multiplier`` gates take effect.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db


log = logging.getLogger(__name__)


# Sportradar live-state statuses that count as "game in-progress" for
# the loop.  Matches the set the sportradar feed itself filters on
# (see ``app.feeds.sportradar._LIVE_STATUSES``) so the loop and the
# feed agree on what "live" means.
_LIVE_STATUSES = frozenset(
    {
        "inprogress",
        "in_progress",
        "live",
        "started",
        "halftime",
        "delayed",
        "1st_half",
        "2nd_half",
        "pause",
        "1st_quarter",
        "2nd_quarter",
        "3rd_quarter",
        "4th_quarter",
        "ot",
        "overtime",
    }
)


# Per-sport cadence defaults (seconds between re-evaluations).  These
# are the floor — the operator can override per sport via the
# runtime_setting key ``risk.live_loop_cadence_<sport>``.  Tighter
# leagues (NBA, NHL, soccer) re-evaluate every 30s; slower leagues
# (NFL between drives, MLB between innings) settle for 60-90s.
DEFAULT_CADENCE_SECONDS: dict[str, float] = {
    "nba": 30.0,
    "nhl": 30.0,
    "mlb": 60.0,
    "nfl": 60.0,
    "ncaab": 30.0,
    "ncaaf": 60.0,
    "soccer": 30.0,
}
DEFAULT_CADENCE_FALLBACK_SECONDS = 60.0


# Per-sport batch cap so a runaway sportradar feed cannot make the
# loop scan more than this many markets in one pass.  Matches the
# shadow_predictor convention.
DEFAULT_BATCH_LIMIT = 1_000


# Sport name → ordered list of league strings stored on ``predictions.league``.
# Keeps the loop sport-scoped (operator throttles one sport without
# touching the others) while still letting a "sport" like "soccer"
# fan out across multiple leagues.  Source of truth for league names
# is the predictions table itself — the values below match what the
# orchestrator + sportradar feed write.
SPORT_LEAGUES: dict[str, tuple[str, ...]] = {
    "nba": ("NBA",),
    "nhl": ("NHL",),
    "mlb": ("MLB",),
    "nfl": ("NFL",),
    "ncaab": ("NCAAB",),
    "ncaaf": ("NCAAF",),
    "soccer": (
        "EPL", "LALIGA", "BUNDESLIGA", "SERIE_A", "LIGUE_1",
        "MLS", "UCL",
    ),
}


# In-process cadence enforcement.  A dict keyed on (sport,
# minute_bucket) is sufficient — the worker process is restart-safe
# because the next tick after a restart will simply re-run (idempotent
# tagging means no harm), and the cadence floor is "at most once per
# window" not "exactly once".
_LAST_RUN_AT: dict[str, float] = {}


# Idempotency cache: keyed on (market_id, minute_bucket).  We don't
# write a new "shadow_predictions"-style table for the live loop —
# the operator-visible effect of a run is the ``predictions.is_live``
# tag and the dashboard tile counts, both of which are idempotent
# under same-minute re-runs by construction.
_TAGGED_THIS_MINUTE: dict[tuple[str, str], float] = {}
_TAGGED_THIS_MINUTE_MAX_ENTRIES = 50_000


@dataclass
class InGameCycleResult:
    sport: str
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    enabled: bool = False
    skipped_reason: str | None = None
    cadence_seconds: float = 0.0
    candidates_scanned: int = 0
    candidates_live: int = 0
    candidates_tagged: int = 0
    candidates_skipped_duplicate: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sport": self.sport,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 4),
            "enabled": self.enabled,
            "skipped_reason": self.skipped_reason,
            "cadence_seconds": self.cadence_seconds,
            "candidates_scanned": self.candidates_scanned,
            "candidates_live": self.candidates_live,
            "candidates_tagged": self.candidates_tagged,
            "candidates_skipped_duplicate": self.candidates_skipped_duplicate,
            "errors": list(self.errors),
        }


def _minute_bucket(now: datetime | None = None) -> str:
    n = now or datetime.now(timezone.utc)
    return n.replace(second=0, microsecond=0).isoformat()


def _normalize_sport(sport: str) -> str:
    return (sport or "").strip().lower()


async def _runtime_value(key: str) -> str | None:
    try:
        row = await db.fetchrow(
            "SELECT value FROM runtime_settings WHERE key = $1", key
        )
        return (row["value"] or "").strip() if row else None
    except Exception as e:  # pragma: no cover — defensive
        log.debug("runtime_settings lookup for %s failed: %s", key, e)
        return None


async def is_enabled(sport: str) -> bool:
    """Per-sport enable flag.  Default False (ships disabled)."""
    raw = await _runtime_value(f"risk.live_loop_enabled_{_normalize_sport(sport)}")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


async def cadence_seconds(sport: str) -> float:
    """Per-sport cadence floor (seconds)."""
    sp = _normalize_sport(sport)
    raw = await _runtime_value(f"risk.live_loop_cadence_{sp}")
    default = DEFAULT_CADENCE_SECONDS.get(sp, DEFAULT_CADENCE_FALLBACK_SECONDS)
    if raw is None:
        return default
    try:
        v = float(raw)
        if v <= 0:
            return default
        return v
    except (TypeError, ValueError):
        return default


async def _fetch_live_eligible(
    sport: str, limit: int = DEFAULT_BATCH_LIMIT
) -> list[dict[str, Any]]:
    """Open ``predictions`` rows whose league belongs to ``sport`` and
    whose sportradar game is currently in-progress.  Returned dicts
    carry the fields the future swarm/predictor wiring will need
    (market_id, league, game_id, fetched_at) plus the loop's bookkeeping
    keys.
    """
    leagues = SPORT_LEAGUES.get(_normalize_sport(sport)) or ()
    if not leagues:
        return []
    try:
        rows = await db.fetch(
            """
            SELECT p.kalshi_market_id  AS market_id,
                   p.league             AS league,
                   p.game_id            AS game_id,
                   p.game_time          AS game_time,
                   p.is_live            AS is_live,
                   p.model_yes_prob     AS model_yes_prob,
                   p.kalshi_price       AS kalshi_price,
                   s.status             AS status,
                   extract(epoch from (now() - s.fetched_at))::float
                                        AS state_age_seconds
              FROM predictions p
              JOIN sportradar_live_state s
                ON s.game_id = p.game_id
             WHERE upper(p.league) = ANY($1::text[])
               AND p.kalshi_market_id IS NOT NULL
               AND lower(s.status) = ANY($2::text[])
             ORDER BY s.fetched_at DESC
             LIMIT $3
            """,
            list(leagues),
            list(_LIVE_STATUSES),
            int(limit),
        )
        return [dict(r) for r in rows]
    except Exception as e:  # pragma: no cover — defensive
        log.warning("in_game_loop _fetch_live_eligible(%s) failed: %s", sport, e)
        return []


async def _tag_is_live(market_ids: list[str]) -> int:
    """Mark each ``predictions`` row as live.  Monotonic: a row that
    was already live stays live.  Returns the number of rows whose
    value actually flipped this call (the UPDATE's natural row-count
    so we don't double-count the dashboard tile)."""
    if not market_ids:
        return 0
    try:
        result = await db.execute(
            """
            UPDATE predictions
               SET is_live = true
             WHERE kalshi_market_id = ANY($1::text[])
               AND is_live = false
            """,
            market_ids,
        )
        # asyncpg returns "UPDATE <n>" as the status string.
        if isinstance(result, str):
            parts = result.split()
            if len(parts) == 2 and parts[0].upper() == "UPDATE":
                try:
                    return int(parts[1])
                except ValueError:
                    return 0
        return 0
    except Exception as e:  # pragma: no cover — defensive
        log.warning("in_game_loop tag is_live UPDATE failed: %s", e)
        return 0


def _gc_tagged_cache() -> None:
    """Evict the cache if it has grown past the cap.  Trivial LRU-ish
    sweep (drop oldest half) — the cache only matters for cadence-
    floor de-duplication, not correctness."""
    if len(_TAGGED_THIS_MINUTE) <= _TAGGED_THIS_MINUTE_MAX_ENTRIES:
        return
    items = sorted(_TAGGED_THIS_MINUTE.items(), key=lambda kv: kv[1])
    keep_from = len(items) // 2
    _TAGGED_THIS_MINUTE.clear()
    for k, v in items[keep_from:]:
        _TAGGED_THIS_MINUTE[k] = v


async def _predict_live_for_market(row: dict[str, Any]) -> float | None:  # noqa: ARG001
    """Stub: per-sport live prediction wiring lands as follow-up tasks
    (#831 niche-sport calibration; per-sport in-game predictors after
    the calibrated shadow-Brier criterion is met).

    Until then this returns None and the loop only tags rows + drives
    dashboard counts so operators can see the schedule is live.
    """
    return None


async def run_in_game_cycle(
    sport: str, *, now: datetime | None = None
) -> InGameCycleResult:
    """One scheduled tick of the in-game loop for ``sport``.

    Steps:

      1. Honor the per-sport enabled flag (default False).
      2. Enforce the per-sport cadence floor via the in-memory
         ``_LAST_RUN_AT`` map.
      3. Fetch open predictions whose game is currently live.
      4. Tag ``predictions.is_live = true`` for any row not yet tagged.
      5. Return a structured result the scheduler / dashboard can use.

    Restart-safe: cadence enforcement is in-memory so a restart just
    resets the clock; the work is idempotent (same-minute re-run tags
    no new rows because the UPDATE filters on ``is_live = false``).
    """
    started_perf = time.perf_counter()
    started_at = (now or datetime.now(timezone.utc)).isoformat()
    sp = _normalize_sport(sport)
    cadence = await cadence_seconds(sp)
    result = InGameCycleResult(
        sport=sp,
        started_at=started_at,
        cadence_seconds=cadence,
    )

    enabled = await is_enabled(sp)
    result.enabled = enabled
    if not enabled:
        result.skipped_reason = (
            f"risk.live_loop_enabled_{sp} is false (ships disabled until "
            "per-sport shadow Brier within 0.02 of pre-game model)"
        )
        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.duration_seconds = time.perf_counter() - started_perf
        return result

    last_at = _LAST_RUN_AT.get(sp)
    monotonic = time.monotonic()
    if last_at is not None and (monotonic - last_at) < cadence:
        wait_remaining = cadence - (monotonic - last_at)
        result.skipped_reason = (
            f"cadence floor: {wait_remaining:.1f}s remaining of "
            f"{cadence:.1f}s window"
        )
        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.duration_seconds = time.perf_counter() - started_perf
        return result
    _LAST_RUN_AT[sp] = monotonic

    rows = await _fetch_live_eligible(sp)
    result.candidates_scanned = len(rows)
    if not rows:
        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.duration_seconds = time.perf_counter() - started_perf
        return result

    bucket = _minute_bucket(now)
    to_tag: list[str] = []
    for r in rows:
        mid = r.get("market_id")
        if not mid:
            continue
        result.candidates_live += 1
        key = (str(mid), bucket)
        if key in _TAGGED_THIS_MINUTE:
            result.candidates_skipped_duplicate += 1
            continue
        _TAGGED_THIS_MINUTE[key] = monotonic
        if not r.get("is_live"):
            to_tag.append(str(mid))

    flipped = await _tag_is_live(to_tag)
    result.candidates_tagged = flipped
    _gc_tagged_cache()

    result.completed_at = datetime.now(timezone.utc).isoformat()
    result.duration_seconds = time.perf_counter() - started_perf
    return result


def _reset_state_for_tests() -> None:
    """Test hook: clear in-memory cadence + idempotency state."""
    _LAST_RUN_AT.clear()
    _TAGGED_THIS_MINUTE.clear()
