"""Internal team-level Elo rating engine.

Provides per-(league, team) ratings used as a feature for the sports
predictors.  Design notes:

* Ratings live in `team_elo` (current snapshot) + `team_elo_history`
  (every game's before/after rating, for backtesting and recent-form).
* `record_game()` is the online updater; it is idempotent on
  `(league, team, game_id)` so settlement re-runs never double-count.
* `seed_from_history()` replays a chronologically-ordered iterable of
  finished games to bootstrap ratings from 5+ years of historical
  results.
* `get_team_features()` returns the dict that the sports feature
  builder injects under `game_context` (team_elo / opponent_elo /
  recent_form_delta) before the predictor runs.

K-factors are tuned per league based on schedule density and per-game
information content.  Higher K = ratings move faster (good for short
seasons / high variance), lower K = ratings stabilise (good for long
seasons of mostly-balanced teams).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .. import db


log = logging.getLogger(__name__)


INITIAL_RATING: float = 1500.0

# Per-league K-factor.  Defaults to 20 when the league is unknown.
K_FACTOR_BY_LEAGUE: dict[str, float] = {
    "NFL": 24.0,     # 17-game season -> each game high-info
    "NBA": 20.0,     # 82-game season
    "NHL": 14.0,     # 82-game season + lots of OT/SO noise
    "MLB": 6.0,      # 162-game season -> small per-game signal
    "NCAAB": 28.0,   # very deep talent pool spread, big signal swings
    "MLS": 20.0,
    "Soccer": 20.0,
    "PGA": 16.0,
}

# Home-field advantage in Elo points, per league.  Added to the home
# team's effective rating only for the expected-score calculation; the
# rating update uses the raw values so HFA doesn't compound.
HFA_BY_LEAGUE: dict[str, float] = {
    "NFL": 55.0,
    "NBA": 60.0,
    "NHL": 35.0,
    "MLB": 24.0,
    "NCAAB": 80.0,
    "MLS": 50.0,
    "Soccer": 50.0,
}

# Recent-form window — number of most recent games used to compute the
# rating-delta signal exposed to predictors.
RECENT_FORM_N: int = 10


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def k_factor(league: str | None) -> float:
    return K_FACTOR_BY_LEAGUE.get((league or "").strip(), 20.0)


def hfa(league: str | None) -> float:
    return HFA_BY_LEAGUE.get((league or "").strip(), 0.0)


def expected_score(rating_a: float, rating_b: float) -> float:
    """Standard Elo expected score for `a` against `b`.

    Returns a number in [0, 1] — the model's predicted win probability
    of `a` if the game has no draws.
    """
    return 1.0 / (1.0 + 10.0 ** ((float(rating_b) - float(rating_a)) / 400.0))


def _outcome_score(home_score: float, away_score: float) -> tuple[float, float]:
    """Convert raw scores to Elo's [0, 0.5, 1] outcome for (home, away)."""
    if home_score > away_score:
        return 1.0, 0.0
    if home_score < away_score:
        return 0.0, 1.0
    return 0.5, 0.5


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamRating:
    league: str
    team: str
    rating: float
    games_played: int


async def get_rating(league: str, team: str) -> TeamRating:
    """Return the current rating for (league, team), defaulting to
    `INITIAL_RATING` with 0 games played when the team is unknown."""
    try:
        row = await db.fetchrow(
            "SELECT rating::float AS rating, games_played::int AS games_played "
            "FROM team_elo WHERE league = $1 AND team = $2",
            league, team,
        )
    except Exception as e:
        log.debug("get_rating failed for %s/%s: %s", league, team, e)
        return TeamRating(league=league, team=team, rating=INITIAL_RATING, games_played=0)
    if row is None:
        return TeamRating(league=league, team=team, rating=INITIAL_RATING, games_played=0)
    return TeamRating(
        league=league, team=team,
        rating=float(row["rating"]),
        games_played=int(row["games_played"]),
    )


async def recent_form_delta(league: str, team: str, n: int = RECENT_FORM_N) -> float:
    """Sum of `(rating_after - rating_before)` over the team's last
    `n` games — a hot/cold streak signal independent of absolute level.

    Returns 0.0 when no history exists.
    """
    try:
        rows = await db.fetch(
            """
            SELECT (rating_after - rating_before)::float AS delta
              FROM team_elo_history
             WHERE league = $1 AND team = $2
             ORDER BY played_at DESC
             LIMIT $3
            """,
            league, team, int(n),
        )
    except Exception as e:
        log.debug("recent_form_delta failed for %s/%s: %s", league, team, e)
        return 0.0
    return float(sum(float(r["delta"]) for r in rows))


async def get_team_features(
    league: str, home_team: str, away_team: str, *, perspective: str = "home"
) -> dict[str, float]:
    """Build the elo sub-payload for the sports feature builder.

    `perspective` selects which team is treated as the focal "team" in
    the returned dict; the other becomes "opponent".  Defaults to home
    so callers that don't care can use it directly.
    """
    home, away = await asyncio.gather(
        get_rating(league, home_team),
        get_rating(league, away_team),
    )
    home_form, away_form = await asyncio.gather(
        recent_form_delta(league, home_team),
        recent_form_delta(league, away_team),
    )
    if perspective.lower() == "away":
        team, opp = away, home
        team_form, opp_form = away_form, home_form
    else:
        team, opp = home, away
        team_form, opp_form = home_form, away_form
    return {
        "team_elo": team.rating,
        "opponent_elo": opp.rating,
        "elo_diff": team.rating - opp.rating,
        "expected_win_prob": expected_score(team.rating + hfa(league)
                                            if perspective.lower() == "home" else team.rating,
                                            opp.rating),
        "recent_form_delta": team_form,
        "opponent_recent_form_delta": opp_form,
        "team_games_played": float(team.games_played),
        "opponent_games_played": float(opp.games_played),
    }


# ---------------------------------------------------------------------------
# Online updater
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameUpdate:
    league: str
    home_team: str
    away_team: str
    home_rating_before: float
    away_rating_before: float
    home_rating_after: float
    away_rating_after: float
    k: float
    expected_home: float
    score_home: float
    skipped: bool = False


async def _upsert_team_rating(
    conn, league: str, team: str, new_rating: float, played_at: datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO team_elo (league, team, rating, games_played, last_updated)
        VALUES ($1, $2, $3, 1, $4)
        ON CONFLICT (league, team) DO UPDATE
           SET rating       = EXCLUDED.rating,
               games_played = team_elo.games_played + 1,
               last_updated = EXCLUDED.last_updated
        """,
        league, team, float(new_rating), played_at,
    )


async def record_game(
    league: str,
    home_team: str,
    away_team: str,
    home_score: float,
    away_score: float,
    *,
    game_id: str,
    played_at: datetime | None = None,
    k_override: float | None = None,
) -> GameUpdate:
    """Apply one game's Elo update and persist both team_elo + history.

    Idempotent on (league, team, game_id) — re-running for the same
    game is a no-op (returns `skipped=True`).
    """
    if not (league and home_team and away_team and game_id):
        raise ValueError("league, home_team, away_team, game_id are required")
    played_at = played_at or datetime.now(timezone.utc)
    k = float(k_override) if k_override is not None else k_factor(league)

    home_rating = (await get_rating(league, home_team)).rating
    away_rating = (await get_rating(league, away_team)).rating

    # Expected score uses HFA-adjusted home rating, but the rating
    # update applies to the raw values (so HFA doesn't compound).
    exp_home = expected_score(home_rating + hfa(league), away_rating)
    exp_away = 1.0 - exp_home
    score_home, score_away = _outcome_score(home_score, away_score)
    new_home = home_rating + k * (score_home - exp_home)
    new_away = away_rating + k * (score_away - exp_away)

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Idempotency claim: if either history row already exists
            # for this (team, game_id), bail without touching ratings.
            existing = await conn.fetchval(
                """
                SELECT 1 FROM team_elo_history
                 WHERE league = $1 AND game_id = $2
                   AND team IN ($3, $4)
                 LIMIT 1
                """,
                league, game_id, home_team, away_team,
            )
            if existing:
                return GameUpdate(
                    league=league, home_team=home_team, away_team=away_team,
                    home_rating_before=home_rating, away_rating_before=away_rating,
                    home_rating_after=home_rating, away_rating_after=away_rating,
                    k=k, expected_home=exp_home, score_home=score_home,
                    skipped=True,
                )
            await conn.execute(
                """
                INSERT INTO team_elo_history
                    (league, team, opponent, game_id, played_at,
                     rating_before, rating_after, opponent_rating_before,
                     k_factor, score, expected, is_home)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, true)
                """,
                league, home_team, away_team, game_id, played_at,
                home_rating, new_home, away_rating, k, score_home, exp_home,
            )
            await conn.execute(
                """
                INSERT INTO team_elo_history
                    (league, team, opponent, game_id, played_at,
                     rating_before, rating_after, opponent_rating_before,
                     k_factor, score, expected, is_home)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, false)
                """,
                league, away_team, home_team, game_id, played_at,
                away_rating, new_away, home_rating, k, score_away, exp_away,
            )
            await _upsert_team_rating(conn, league, home_team, new_home, played_at)
            await _upsert_team_rating(conn, league, away_team, new_away, played_at)

    return GameUpdate(
        league=league, home_team=home_team, away_team=away_team,
        home_rating_before=home_rating, away_rating_before=away_rating,
        home_rating_after=new_home, away_rating_after=new_away,
        k=k, expected_home=exp_home, score_home=score_home,
    )


# ---------------------------------------------------------------------------
# Backfill / seeding
# ---------------------------------------------------------------------------


@dataclass
class SeedResult:
    games_processed: int = 0
    games_skipped: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


async def seed_from_history(games: Iterable[Mapping[str, Any]]) -> SeedResult:
    """Replay chronologically-ordered games to bootstrap ratings.

    Each `game` mapping must carry: league, home_team, away_team,
    home_score, away_score, game_id, played_at.  Re-running with the
    same game_ids is safe — duplicates are skipped via the history
    table's unique constraint.

    Designed for "load 5 years of historical results to seed ratings"
    — caller fetches games sorted by date and passes them through.
    """
    res = SeedResult()
    for g in games:
        try:
            played_at = g.get("played_at")
            if isinstance(played_at, str):
                played_at = datetime.fromisoformat(played_at.replace("Z", "+00:00"))
            update = await record_game(
                league=str(g["league"]),
                home_team=str(g["home_team"]),
                away_team=str(g["away_team"]),
                home_score=float(g["home_score"]),
                away_score=float(g["away_score"]),
                game_id=str(g["game_id"]),
                played_at=played_at,
            )
            if update.skipped:
                res.games_skipped += 1
            else:
                res.games_processed += 1
        except Exception as e:
            res.errors.append(f"{g.get('game_id')}: {e}")
            log.warning("seed_from_history game failed (%s): %s", g.get("game_id"), e)
    return res


# ---------------------------------------------------------------------------
# Feature payload helper for the orchestrator
# ---------------------------------------------------------------------------


async def attach_elo_to_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Mutate `candidate` in place: enrich `game_context` with elo
    features when the candidate carries enough team info.  Best-effort
    — silently no-ops when team names or league are missing or the DB
    is unavailable, so the trading loop never fails on an Elo lookup.
    """
    ctx = candidate.get("game_context")
    if isinstance(ctx, str):
        try:
            import json as _json
            ctx = _json.loads(ctx)
        except Exception:
            ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}

    league = candidate.get("league") or ctx.get("league")
    home = ctx.get("home_team") or candidate.get("home_team")
    away = ctx.get("away_team") or candidate.get("away_team")
    if not (league and home and away):
        return candidate
    try:
        feats = await get_team_features(str(league), str(home), str(away))
    except Exception as e:
        log.debug("attach_elo_to_candidate failed: %s", e)
        return candidate
    ctx.update(feats)
    candidate["game_context"] = ctx
    return candidate


__all__ = [
    "INITIAL_RATING",
    "K_FACTOR_BY_LEAGUE",
    "HFA_BY_LEAGUE",
    "RECENT_FORM_N",
    "TeamRating",
    "GameUpdate",
    "SeedResult",
    "k_factor",
    "hfa",
    "expected_score",
    "get_rating",
    "recent_form_delta",
    "get_team_features",
    "record_game",
    "seed_from_history",
    "attach_elo_to_candidate",
]
