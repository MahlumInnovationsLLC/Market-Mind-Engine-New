"""WNBA player-props dashboard helper (task #823, step 9).

Builds the JSON payload returned by ``GET /api/dashboard/wnba/props/today``
without owning the FastAPI route surface — the route is wired in
:mod:`app.main`.

Shape:

    {
      "date_utc": "2026-05-11",
      "min_sample": 5,
      "max_matchup_adj": 0.25,
      "props": [
        {
          "player": "...",
          "stat": "points",
          "line": 19.5,
          "side": "over",
          "book": "...",
          "kalshi_mid": null,
          "model_probability": 0.61,
          "implied_probability": 0.5238,
          "edge_bps": 870,
          "projected": 22.4,
          "sample_count": 14,
          "matchup_factor": 1.06,
          "gate_status": "ok" | "rejected:<reason>",
          "why": ["..."],
        },
        ...
      ],
      "rejected": [...]   # only when include_rejected=true
    }
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from .. import db
from ..player_props.injuries import fetch_player_status, is_blocking_status
from .matchup import DEFAULT_MAX_MATCHUP_ADJ
from .predictor import (
    PROP_MARKETS,
    predict_wnba_player_prop,
)


log = logging.getLogger(__name__)


def _decimal_to_implied(decimal_odds: float | None) -> float | None:
    if decimal_odds is None or decimal_odds <= 1.0:
        return None
    try:
        return 1.0 / float(decimal_odds)
    except (TypeError, ValueError):
        return None


async def _todays_quotes() -> list[dict]:
    """Latest quote per (game, player, market, side) for today's UTC
    day.  Conservative — we only include rows from the last 24h."""
    try:
        rows = await db.fetch(
            """
            WITH latest AS (
                SELECT DISTINCT ON (game_id, player, market, outcome)
                       game_id, player, market, outcome, line,
                       price_decimal, book, "timestamp", raw, team
                  FROM player_props_quotes
                 WHERE league = 'WNBA'
                   AND "timestamp" > now() - interval '24 hours'
                   AND market = ANY($1::text[])
                 ORDER BY game_id, player, market, outcome,
                          "timestamp" DESC
            )
            SELECT * FROM latest
            ORDER BY game_id, player, market, outcome
            """,
            list(PROP_MARKETS),
        )
    except Exception as e:  # pragma: no cover — fresh DB / no table yet
        log.debug("wnba dashboard quote read failed: %s", e)
        return []
    return [dict(r) for r in rows]


async def _opponent_lookup(game_id: str) -> str | None:
    """Best-effort opponent-team lookup from the cached schedule."""
    try:
        row = await db.fetchrow(
            """
            SELECT home_team, away_team
              FROM sportradar_schedules
             WHERE event_id = $1
                OR event_id = 'espn:WNBA:' || $1
             LIMIT 1
            """,
            game_id,
        )
    except Exception:
        return None
    if row is None:
        return None
    # We don't yet know which side the player is on (TheOddsAPI doesn't
    # tag it); the dashboard returns both teams in `raw` and the
    # predictor uses the away team as a conservative pace anchor.
    return row["away_team"] or row["home_team"]


async def build_dashboard_payload(
    *,
    include_rejected: bool = False,
) -> dict[str, Any]:
    """Build the day's tradable WNBA props payload."""
    quotes = await _todays_quotes()
    props_out: list[dict[str, Any]] = []
    rejected_out: list[dict[str, Any]] = []
    today = datetime.now(timezone.utc).date().isoformat()
    min_sample = int(os.environ.get("AXIOM_WNBA_PROPS_MIN_SAMPLE", "5") or "5")

    for q in quotes:
        player = q["player"]
        stat = q["market"]
        side = (q["outcome"] or "over").lower()
        line = q["line"]
        if line is None:
            continue

        opp = await _opponent_lookup(str(q["game_id"]))
        prediction = await predict_wnba_player_prop(
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
                "why": [f"Sample for {player}/{stat} below "
                        f"min_sample={min_sample}"],
            }
            rejected_out.append(row)
            continue

        status = await fetch_player_status("WNBA", player)
        if status is not None and is_blocking_status(status.status):
            row = {
                **base_row,
                "gate_status": f"rejected:player_status_{status.status.lower()}",
                "model_probability": None,
                "edge_bps": None,
                "why": [f"{player} listed as {status.status}"],
            }
            rejected_out.append(row)
            continue

        prob = prediction.probability
        edge_bps = (
            int(round((prob - implied) * 10000))
            if implied is not None
            else None
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
            "matchup_bounded": matchup.get("bounded") if matchup else None,
            "gate_status": "ok",
            "why": why,
        })

    payload: dict[str, Any] = {
        "date_utc": today,
        "min_sample": min_sample,
        "max_matchup_adj": DEFAULT_MAX_MATCHUP_ADJ,
        "props": props_out,
        "vertical_enabled": False,  # ships disabled until calibrated
    }
    if include_rejected:
        payload["rejected"] = rejected_out
    return payload
