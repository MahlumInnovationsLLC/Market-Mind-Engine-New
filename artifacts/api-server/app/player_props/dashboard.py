"""Sport-agnostic player-props dashboard helper (task #829).

Generalised from :mod:`app.wnba.dashboard` so the per-league dashboard
tiles for NBA / NHL / MLB / NFL all share the same JSON-payload
shape, only branching on the league spec.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from .. import db
from .injuries import fetch_player_status, is_blocking_status
from .league_specs import PlayerPropLeagueSpec, get_league_spec
from .matchup import DEFAULT_MAX_MATCHUP_ADJ
from .predictor import predict_player_prop


log = logging.getLogger(__name__)


def _decimal_to_implied(decimal_odds: float | None) -> float | None:
    if decimal_odds is None or decimal_odds <= 1.0:
        return None
    try:
        return 1.0 / float(decimal_odds)
    except (TypeError, ValueError):
        return None


async def _todays_quotes(spec: PlayerPropLeagueSpec) -> list[dict]:
    try:
        rows = await db.fetch(
            """
            WITH latest AS (
                SELECT DISTINCT ON (game_id, player, market, outcome)
                       game_id, player, market, outcome, line,
                       price_decimal, book, "timestamp", raw, team
                  FROM player_props_quotes
                 WHERE league = $1
                   AND "timestamp" > now() - interval '24 hours'
                   AND market = ANY($2::text[])
                 ORDER BY game_id, player, market, outcome,
                          "timestamp" DESC
            )
            SELECT * FROM latest
            ORDER BY game_id, player, market, outcome
            """,
            spec.league,
            list(spec.markets),
        )
    except Exception as e:  # pragma: no cover — fresh DB / no table yet
        log.debug(
            "%s dashboard quote read failed: %s", spec.vertical, e,
        )
        return []
    return [dict(r) for r in rows]


async def _opponent_lookup(game_id: str) -> str | None:
    try:
        row = await db.fetchrow(
            """
            SELECT home_team, away_team
              FROM sportradar_schedules
             WHERE event_id = $1
                OR event_id LIKE '%:' || $1
             LIMIT 1
            """,
            game_id,
        )
    except Exception:
        return None
    if row is None:
        return None
    return row["away_team"] or row["home_team"]


async def build_dashboard_payload(
    *,
    league: str,
    include_rejected: bool = False,
) -> dict[str, Any]:
    """Build the day's tradable player-props payload for a league."""
    spec = get_league_spec(league)
    if spec is None:
        return {
            "league": (league or "").upper(),
            "error": f"league {league!r} not registered for player props",
            "props": [],
        }
    quotes = await _todays_quotes(spec)
    props_out: list[dict[str, Any]] = []
    rejected_out: list[dict[str, Any]] = []
    today = datetime.now(timezone.utc).date().isoformat()
    min_sample = int(
        os.environ.get(
            f"AXIOM_{spec.league}_PROPS_MIN_SAMPLE",
            os.environ.get("AXIOM_WNBA_PROPS_MIN_SAMPLE", "5"),
        ) or "5"
    )

    for q in quotes:
        player = q["player"]
        stat = q["market"]
        side = (q["outcome"] or "over").lower()
        line = q["line"]
        if line is None:
            continue
        if stat not in spec.markets:
            continue

        opp = await _opponent_lookup(str(q["game_id"]))
        prediction = await predict_player_prop(
            league=spec.league,
            player=player,
            stat=stat,
            line=float(line),
            side=side,
            opponent_team=opp,
            kalshi_mid=None,
            min_sample=min_sample,
        )
        implied = _decimal_to_implied(q["price_decimal"])

        base_row = {
            "player": player,
            "team": q.get("team"),
            "stat": stat,
            "line": float(line),
            "side": side,
            "book": q["book"],
            "game_id": q["game_id"],
            "implied_probability": implied,
            "decimal_odds": q["price_decimal"],
            "fetched_at": (
                q["timestamp"].isoformat()
                if isinstance(q["timestamp"], datetime)
                else None
            ),
        }

        if prediction is None:
            row = {
                **base_row,
                "gate_status": "rejected:insufficient_sample",
                "model_probability": None,
                "edge_bps": None,
                "why": [
                    f"Sample for {player}/{stat} below "
                    f"min_sample={min_sample}"
                ],
            }
            rejected_out.append(row)
            continue

        status = await fetch_player_status(spec.league, player)
        if status is not None and is_blocking_status(status.status):
            row = {
                **base_row,
                "gate_status": (
                    f"rejected:player_status_{status.status.lower()}"
                ),
                "model_probability": None,
                "edge_bps": None,
                "why": [
                    f"{player} listed as {status.status} "
                    f"({spec.injury_status_label})"
                ],
            }
            rejected_out.append(row)
            continue

        prob = prediction.probability
        edge_bps = (
            int(round((prob - implied) * 10000))
            if implied is not None else None
        )
        baseline = prediction.raw.get("baseline") or {}
        matchup = prediction.raw.get("matchup") or {}
        why = list(prediction.raw.get("why") or [])

        props_out.append({
            **base_row,
            "model_probability": float(prob),
            "edge_bps": edge_bps,
            "projected": prediction.raw.get("projected"),
            "sample_count": baseline.get("sample_count"),
            "matchup_factor": matchup.get("factor") if matchup else None,
            "matchup_bounded": (
                matchup.get("bounded") if matchup else None
            ),
            "gate_status": "ok",
            "why": why,
        })

    payload: dict[str, Any] = {
        "league": spec.league,
        "vertical": spec.vertical,
        "date_utc": today,
        "min_sample": min_sample,
        "max_matchup_adj": DEFAULT_MAX_MATCHUP_ADJ,
        "inactive_window_minutes": spec.inactive_window_minutes,
        "injury_status_label": spec.injury_status_label,
        "calibrator_bucket": spec.calibrator_bucket,
        "markets": list(spec.markets),
        "props": props_out,
        "vertical_enabled": False,  # ships disabled until calibrated
    }
    if include_rejected:
        payload["rejected"] = rejected_out
    return payload
