"""End-to-end tournament replay harness (task #697).

Task #651 added :func:`app.feeds.sportradar.iter_tournament_leaderboard_history`
which yields one ``{entrant: rating}`` dict per stored leaderboard
snapshot of a finished tournament, but nothing currently drives the
orchestrator's pricing engine through that history end-to-end.  This
module is that harness: walk every saved snapshot of an event, run
each one through the same Plackett-Luce predictor the live pipeline
uses, and grade the resulting probabilities against the actual
winner.  The point is to measure how well our orchestrator *quoted
mid-event* — the whole reason we save the snapshots in the first
place.

Two public entry points:

* :func:`replay_event` — score a single event end-to-end.  Optional
  ``winner`` overrides the inferred-from-final-snapshot pick, and
  optional ``since``/``until`` narrow the replay window so callers can
  re-grade just the late stages of a long tournament.
* :func:`replay_events_in_range` — sweep every event with stored
  snapshots in a date window (optionally filtered by league) and
  return one report per event.

Each replay returns a :class:`TournamentReplayResult` carrying:

* per-snapshot score (probability the *actual* winner had at the time
  + the matching Brier / log-loss),
* an event-level rollup (mean Brier / mean log-loss / "how often was
  the actual winner our top pick" accuracy).

Replays never write into :mod:`app.sports.ratings` — the iter helper
already isolates the live cache, and we mirror that contract end-to-end
so a backtest run can never pollute live pricing for the same event.

Completed runs are persisted via :func:`record_run` into
``sportradar_tournament_replays`` so the operator dashboard can list
recent replays without re-walking the snapshot history every render.
DB writes are best-effort — a flaky persist never masks the in-memory
report from the operator.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db
from ..feeds.sportradar import (
    iter_tournament_leaderboard_history,
    latest_tournament_leaderboard,
)
from ..predictor_n_way import plackett_luce_from_mapping


log = logging.getLogger(__name__)


# Clamp for log-loss stability — Plackett-Luce probabilities can hit
# 0 exactly when the actual winner is unranked in the snapshot's
# ratings dict (e.g. mid-event we hadn't seen them yet).  Mirrors the
# clamp used by the crypto backtest.
_LOG_LOSS_EPS: float = 1e-9


@dataclass
class SnapshotScore:
    fetched_at: str
    n_entrants: int
    yes_prob: float
    brier: float
    log_loss: float
    top_pick: str
    top_pick_was_winner: bool


@dataclass
class TournamentReplayResult:
    event_id: str
    league: str
    name: str
    winner: str | None
    n_snapshots: int
    accuracy: float       # fraction of snapshots where winner was top-rated
    brier_score: float    # mean (1 - p_winner)^2 across snapshots
    log_loss: float       # mean -ln(max(p_winner, eps)) across snapshots
    snapshots: list[SnapshotScore] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "league": self.league,
            "name": self.name,
            "winner": self.winner,
            "n_snapshots": self.n_snapshots,
            "accuracy": round(self.accuracy, 6),
            "brier_score": round(self.brier_score, 6),
            "log_loss": round(self.log_loss, 6),
            "snapshots": [
                {
                    "fetched_at": s.fetched_at,
                    "n_entrants": s.n_entrants,
                    "yes_prob": round(s.yes_prob, 6),
                    "brier": round(s.brier, 6),
                    "log_loss": round(s.log_loss, 6),
                    "top_pick": s.top_pick,
                    "top_pick_was_winner": s.top_pick_was_winner,
                }
                for s in self.snapshots
            ],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def _score_snapshot(
    ratings: dict[str, float], winner: str
) -> tuple[float, float, float, str]:
    """Return ``(yes_prob, brier, log_loss, top_pick)`` for ``ratings``.

    ``yes_prob`` is the Plackett-Luce probability assigned to the
    actual winner (0.0 when they aren't in the snapshot's field).
    ``top_pick`` is the entrant the orchestrator would have quoted as
    the favourite — used by the rollup to compute "how often were we
    pointed at the right horse".
    """
    probs = plackett_luce_from_mapping(ratings)
    p = float(probs.get(winner, 0.0))
    brier = (1.0 - p) ** 2
    p_clamped = max(_LOG_LOSS_EPS, min(1.0 - _LOG_LOSS_EPS, p))
    log_loss = -math.log(p_clamped)
    # `max` on a dict iterates keys; pick the entrant with the highest
    # rating.  Stable for ties via Python's deterministic max.
    top_pick = max(ratings, key=ratings.get) if ratings else ""
    return p, brier, log_loss, top_pick


async def _resolve_event_metadata(event_id: str) -> tuple[str, str]:
    """Best-effort ``(league, display_name)`` lookup from
    ``sportradar_schedules``.  Returns ``("", "")`` when the schedule
    table is missing (fresh dev DB) or the event was never scheduled
    locally — operators still get a useful report keyed off the raw
    event id.
    """
    try:
        row = await db.fetchrow(
            """
            SELECT league, home_team, away_team
              FROM sportradar_schedules
             WHERE game_id = $1
             LIMIT 1
            """,
            event_id,
        )
    except Exception as e:  # noqa: BLE001
        log.debug(
            "[tournament_backtest] schedule lookup skipped for %s (%s)",
            event_id, e,
        )
        return "", ""
    if not row:
        return "", ""
    league = (row["league"] or "").upper()
    name = (row["home_team"] or row["away_team"] or "").strip()
    return league, name


async def _infer_winner_from_history(event_id: str) -> str | None:
    """Use the most recent stored snapshot's position-1 entrant as the
    actual winner.  Returns ``None`` when nothing is recorded for the
    event yet — callers must surface that the report could not be
    scored rather than silently grading against a missing horse.
    """
    snap = await latest_tournament_leaderboard(event_id)
    if not snap:
        return None
    best_pos: int | None = None
    best_name: str | None = None
    for e in snap:
        name = e.get("name")
        pos = e.get("position")
        if not name or pos is None:
            continue
        try:
            p = int(float(pos))
        except (TypeError, ValueError):
            continue
        if p < 1:
            continue
        if best_pos is None or p < best_pos:
            best_pos = p
            best_name = str(name)
    return best_name


async def replay_event(
    event_id: str,
    *,
    winner: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> TournamentReplayResult:
    """Walk every persisted snapshot of ``event_id`` and grade the
    Plackett-Luce probabilities against the actual winner.

    ``winner`` overrides the inferred-from-final-snapshot pick — useful
    when the orchestrator has a more authoritative settlement source
    (a Kalshi outright market, say).  ``since``/``until`` narrow the
    replay window inclusively on ``fetched_at``; either may be
    ``None`` for an open bound.

    Returns a :class:`TournamentReplayResult` with ``n_snapshots == 0``
    (and zeroed metrics) when no snapshots exist for the event, so the
    caller can still tell the operator "nothing to replay" without
    crashing.
    """
    started = datetime.now(timezone.utc).isoformat()
    league, name = await _resolve_event_metadata(event_id)
    actual_winner = (
        winner.strip() if isinstance(winner, str) and winner.strip()
        else await _infer_winner_from_history(event_id)
    )

    snapshots: list[SnapshotScore] = []
    n_correct = 0
    sum_brier = 0.0
    sum_log_loss = 0.0

    if actual_winner:
        async for ts, ratings in iter_tournament_leaderboard_history(
            event_id, since=since, until=until,
        ):
            yes_prob, brier, log_loss, top_pick = _score_snapshot(
                ratings, actual_winner
            )
            ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            top_was_winner = top_pick == actual_winner
            snapshots.append(
                SnapshotScore(
                    fetched_at=ts_iso,
                    n_entrants=len(ratings),
                    yes_prob=yes_prob,
                    brier=brier,
                    log_loss=log_loss,
                    top_pick=top_pick,
                    top_pick_was_winner=top_was_winner,
                )
            )
            if top_was_winner:
                n_correct += 1
            sum_brier += brier
            sum_log_loss += log_loss

    n = len(snapshots)
    return TournamentReplayResult(
        event_id=event_id,
        league=league,
        name=name,
        winner=actual_winner,
        n_snapshots=n,
        accuracy=(n_correct / n) if n else 0.0,
        brier_score=(sum_brier / n) if n else 0.0,
        log_loss=(sum_log_loss / n) if n else 0.0,
        snapshots=snapshots,
        started_at=started,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


async def replay_events_in_range(
    *,
    start: datetime,
    end: datetime,
    league: str | None = None,
) -> list[TournamentReplayResult]:
    """Replay every event with at least one stored snapshot whose
    ``fetched_at`` falls inside ``[start, end]``.

    Optional ``league`` (e.g. ``"PGA"`` / ``"F1"``) restricts the
    sweep to a single discipline by joining against
    ``sportradar_schedules`` — events without a schedule row are
    skipped when a league filter is set.

    Returns one :class:`TournamentReplayResult` per event in
    deterministic ``event_id`` order.  Per-event failures are isolated
    so one missing snapshot can't blank the rest of the sweep.
    """
    args: list[Any] = [start, end]
    sql = (
        "SELECT DISTINCT t.event_id "
        "  FROM sportradar_tournament_leaderboards t "
        " WHERE t.fetched_at >= $1 AND t.fetched_at <= $2"
    )
    if league:
        args.append(league.upper())
        sql += (
            " AND EXISTS ("
            "   SELECT 1 FROM sportradar_schedules s "
            "    WHERE s.game_id = t.event_id "
            "      AND upper(s.league) = $3"
            " )"
        )
    sql += " ORDER BY t.event_id"

    try:
        rows = await db.fetch(sql, *args)
    except Exception as e:  # noqa: BLE001
        log.info("[tournament_backtest] event sweep skipped (%s)", e)
        return []

    out: list[TournamentReplayResult] = []
    for r in rows:
        event_id = str(r["event_id"])
        try:
            # Forward the window bounds so per-event metrics are
            # computed over the *requested* slice of history rather
            # than the event's full stored history — without this, a
            # narrow range query would still trigger full-history
            # replay work and produce metrics that don't match the
            # operator's stated window.
            result = await replay_event(
                event_id, since=start, until=end,
            )
        except Exception:  # pragma: no cover — defensive
            log.exception(
                "[tournament_backtest] replay failed for %s", event_id,
            )
            continue
        out.append(result)
    return out


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def record_run(result: TournamentReplayResult) -> None:
    """Persist a completed replay summary into
    ``sportradar_tournament_replays`` so the operator dashboard can
    list recent runs without re-walking the snapshot history every
    render.

    Best-effort: callers report the in-memory summary regardless, so a
    write failure here is logged and swallowed rather than failing the
    surrounding HTTP call.  Empty replays (``n_snapshots == 0`` — no
    history was recorded for the event) are skipped so the trend
    panel isn't polluted with meaningless zero rows.
    """
    if result.n_snapshots <= 0:
        return
    started = _parse_ts(result.started_at)
    completed = _parse_ts(result.completed_at)
    try:
        await db.execute(
            """
            INSERT INTO sportradar_tournament_replays (
                event_id, league, event_name, winner,
                n_snapshots, accuracy, brier_score, log_loss,
                started_at, completed_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8,
                COALESCE($9, now()), COALESCE($10, now())
            )
            """,
            result.event_id,
            result.league,
            result.name,
            result.winner,
            int(result.n_snapshots),
            float(result.accuracy),
            float(result.brier_score),
            float(result.log_loss),
            started,
            completed,
        )
    except Exception:  # pragma: no cover — defensive: never fail the caller
        log.exception(
            "[tournament_backtest] failed to persist replay for %s",
            result.event_id,
        )


async def list_recent_runs(*, limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent persisted replays in newest-first order
    so the operator dashboard can render a "recent runs" table without
    a follow-up sort.  Returns an empty list when the table is missing
    (fresh dev DB) so the panel falls back gracefully.
    """
    capped = max(1, min(int(limit), 500))
    try:
        rows = await db.fetch(
            """
            SELECT id, event_id, league, event_name, winner,
                   n_snapshots, accuracy, brier_score, log_loss,
                   started_at, completed_at
              FROM sportradar_tournament_replays
             ORDER BY completed_at DESC
             LIMIT $1
            """,
            capped,
        )
    except Exception as e:  # noqa: BLE001
        log.info("[tournament_backtest] list_recent_runs skipped (%s)", e)
        return []
    return [
        {
            "id": int(r["id"]),
            "event_id": r["event_id"],
            "league": r["league"] or "",
            "event_name": r["event_name"] or "",
            "winner": r["winner"],
            "n_snapshots": int(r["n_snapshots"]),
            "accuracy": float(r["accuracy"]),
            "brier_score": float(r["brier_score"]),
            "log_loss": float(r["log_loss"]),
            "started_at": (
                r["started_at"].isoformat() if r["started_at"] else None
            ),
            "completed_at": (
                r["completed_at"].isoformat() if r["completed_at"] else None
            ),
        }
        for r in rows
    ]


# Schedule statuses that mark a tournament / race weekend as finished
# and therefore eligible for an automatic replay.  Sportradar's golf
# v3 uses "closed" once the final round wraps, F1 v2 uses "ended" /
# "complete", and a few legacy payloads emit "finished" — accept all
# four so the auto-replay hook fires on every shape we see in the
# wild without bespoke per-league branching.
_FINISHED_TOURNAMENT_STATUSES: frozenset[str] = frozenset({
    "closed", "complete", "completed", "ended", "finished",
})


async def _finished_tournament_event_ids() -> list[str]:
    """Return every ``sportradar_schedules.game_id`` whose status marks
    the tournament as finished (closed / complete / ended / finished).

    Restricted to the two leagues whose leaderboard snapshots are
    persisted (PGA + F1) so the auto-replay loop doesn't waste a SQL
    round-trip walking soccer / NBA games it could never replay.

    Best-effort: returns ``[]`` when the schedules table is missing
    (fresh dev DB) so the caller can no-op cleanly without the
    surrounding feed flipping to error.
    """
    try:
        rows = await db.fetch(
            """
            SELECT game_id
              FROM sportradar_schedules
             WHERE league = ANY($1::text[])
               AND lower(status) = ANY($2::text[])
             ORDER BY game_id
            """,
            ["PGA", "F1"],
            list(_FINISHED_TOURNAMENT_STATUSES),
        )
    except Exception as e:  # noqa: BLE001
        log.info(
            "[tournament_backtest] finished-event lookup skipped (%s)", e,
        )
        return []
    return [str(r["game_id"]) for r in rows if r["game_id"]]


async def _already_replayed_event_ids(event_ids: list[str]) -> set[str]:
    """Return the subset of ``event_ids`` that already have at least
    one persisted replay row.

    Used by :func:`auto_replay_finished_tournaments` to keep the auto
    hook idempotent — once a tournament has been replayed we don't
    re-grade it on every poll cycle.  Returns an empty set when the
    persisted-runs table is missing so the caller falls back to
    "replay everything" rather than silently skipping every event.
    """
    if not event_ids:
        return set()
    try:
        rows = await db.fetch(
            """
            SELECT DISTINCT event_id
              FROM sportradar_tournament_replays
             WHERE event_id = ANY($1::text[])
            """,
            list(event_ids),
        )
    except Exception as e:  # noqa: BLE001
        log.info(
            "[tournament_backtest] already-replayed lookup skipped (%s)", e,
        )
        return set()
    return {str(r["event_id"]) for r in rows if r["event_id"]}


async def auto_replay_finished_tournaments() -> dict[str, Any]:
    """Replay every newly-finished tournament once and persist the
    summary so the operator dashboard's accuracy trend keeps growing
    without an operator manually POSTing to the admin endpoint.

    Walks ``sportradar_schedules`` for events whose status flipped to
    one of :data:`_FINISHED_TOURNAMENT_STATUSES`, skips any event that
    already has a persisted replay (idempotent — a tournament is
    graded exactly once), and runs :func:`replay_event` +
    :func:`record_run` for the rest.  Per-event failures are logged
    and isolated so one missing snapshot history can't block the rest
    of the sweep.

    Returns a small bookkeeping dict (``finished``, ``skipped``,
    ``replayed``, ``failed``, ``event_ids``) so the caller — typically
    a registered ``Feed`` poller — can stash it on feed metadata for
    the operator console without re-running the sweep.
    """
    finished_ids = await _finished_tournament_event_ids()
    already = await _already_replayed_event_ids(finished_ids)
    pending = [eid for eid in finished_ids if eid not in already]

    replayed: list[str] = []
    failed: list[str] = []
    for event_id in pending:
        try:
            result = await replay_event(event_id)
        except Exception:  # noqa: BLE001 — one event must not blank the rest
            log.exception(
                "[tournament_backtest] auto-replay failed for %s", event_id,
            )
            failed.append(event_id)
            continue
        # ``record_run`` is itself best-effort and skips empty replays
        # (n_snapshots == 0).  Treat an empty replay as "nothing to
        # grade" — leave it out of ``replayed`` so the next poll cycle
        # tries again once snapshots actually land.
        if result.n_snapshots <= 0:
            log.info(
                "[tournament_backtest] auto-replay skipped %s (no snapshots)",
                event_id,
            )
            continue
        await record_run(result)
        replayed.append(event_id)

    return {
        "finished": len(finished_ids),
        "skipped": len(already),
        "replayed": len(replayed),
        "failed": len(failed),
        "event_ids": replayed,
    }


__all__ = [
    "SnapshotScore",
    "TournamentReplayResult",
    "replay_event",
    "replay_events_in_range",
    "record_run",
    "list_recent_runs",
    "auto_replay_finished_tournaments",
]
