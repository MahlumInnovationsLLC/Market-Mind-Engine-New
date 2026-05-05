"""Backup soccer settlement source — Task #769.

The settlement loop must consult an independent source (ESPN public
scoreboard) before settling a soccer market on Sportradar alone.  When
the two disagree, or the primary is stale and the secondary is also
unavailable, the row is parked on ``soccer_settlement_review`` for an
operator to resolve from the dashboard.

These tests exercise the agreement-check decision matrix, the enqueue
helper's idempotency, the admin resolve flow (audit row + UPDATE +
``settle_market`` invocation), the dashboard tile freshness logic, and
the ESPN adapter's name-matching against an injected scoreboard
payload.  Each test patches ``app.settlement.pipeline.db.*`` directly
following the pattern established in ``test_three_way_settlement.py``
so no live database is required.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.feeds import espn_soccer
from app.settlement import pipeline as _pipeline


# ---------------------------------------------------------------------------
# Decision matrix for _resolve_soccer_settlement
# ---------------------------------------------------------------------------


def _summary(result: str, *, source: str = "sportradar") -> dict[str, Any]:
    return {
        "result": result,
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 2 if result == "home" else (1 if result == "draw" else 0),
        "away_score": 2 if result == "away" else (1 if result == "draw" else 0),
        "status": "closed",
        "source": source,
    }


def _stub_resolve_helpers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fallback: dict[str, Any] | None,
    age_seconds: float | None = None,
) -> None:
    """Patch ESPN lookup + scheduled-age helper used by _resolve_soccer_settlement."""

    async def _fake_espn(_market_id: str) -> dict[str, Any] | None:
        return fallback

    async def _fake_age(_market_id: str) -> float | None:
        return age_seconds

    monkeypatch.setattr(_pipeline, "_espn_soccer_match_summary", _fake_espn)
    monkeypatch.setattr(_pipeline, "_market_scheduled_age_seconds", _fake_age)


def test_resolve_soccer_both_agree_settles(monkeypatch):
    _stub_resolve_helpers(monkeypatch, fallback=_summary("home", source="espn"))
    decision = asyncio.run(
        _pipeline._resolve_soccer_settlement(
            market_id="MKT-EPL-HOME",
            league="EPL",
            sportradar_summary=_summary("home"),
        )
    )
    assert decision["action"] == "settle"
    assert decision["result_3way"] == "home"
    assert decision["disagreement_kind"] is None
    assert decision["match_summary"]["source"] == "sportradar"
    assert decision["primary"]["result"] == "home"
    assert decision["fallback"]["result"] == "home"


def test_resolve_soccer_disagreement_queues(monkeypatch):
    _stub_resolve_helpers(monkeypatch, fallback=_summary("away", source="espn"))
    decision = asyncio.run(
        _pipeline._resolve_soccer_settlement(
            market_id="MKT-EPL-DISAGREE",
            league="EPL",
            sportradar_summary=_summary("home"),
        )
    )
    assert decision["action"] == "queue"
    assert decision["disagreement_kind"] == "disagree"
    assert decision["result_3way"] is None
    assert decision["primary"]["result"] == "home"
    assert decision["fallback"]["result"] == "away"


def test_resolve_soccer_primary_only_settles_when_fallback_unavailable(monkeypatch):
    # ESPN returned None — single-source settlement is acceptable when
    # the primary itself is final, otherwise an outage of the secondary
    # would block every soccer settlement.
    _stub_resolve_helpers(monkeypatch, fallback=None)
    decision = asyncio.run(
        _pipeline._resolve_soccer_settlement(
            market_id="MKT-EPL-PRIM-ONLY",
            league="EPL",
            sportradar_summary=_summary("draw"),
        )
    )
    assert decision["action"] == "settle"
    assert decision["result_3way"] == "draw"
    assert decision["fallback"] is None


def test_resolve_soccer_fallback_only_queues_when_primary_stale(monkeypatch):
    # Sportradar hasn't published a final score yet, but ESPN has, AND
    # kickoff was over 3 hours ago.  Queue for review — the operator
    # signs off on settling from the secondary alone.
    _stub_resolve_helpers(
        monkeypatch,
        fallback=_summary("home", source="espn"),
        age_seconds=4 * 3600,
    )
    decision = asyncio.run(
        _pipeline._resolve_soccer_settlement(
            market_id="MKT-EPL-PRIM-STALE",
            league="EPL",
            sportradar_summary=None,
        )
    )
    assert decision["action"] == "queue"
    assert decision["disagreement_kind"] == "primary_stale_secondary_final"


def test_resolve_soccer_fallback_only_skips_when_primary_fresh(monkeypatch):
    # Same as above but match started 30 minutes ago — Sportradar may
    # just be slow.  Skip and wait, do NOT enqueue and do NOT settle.
    _stub_resolve_helpers(
        monkeypatch,
        fallback=_summary("home", source="espn"),
        age_seconds=30 * 60,
    )
    decision = asyncio.run(
        _pipeline._resolve_soccer_settlement(
            market_id="MKT-EPL-FRESH",
            league="EPL",
            sportradar_summary=None,
        )
    )
    assert decision["action"] == "skip"
    assert decision["disagreement_kind"] is None
    assert decision["result_3way"] is None


def test_resolve_soccer_neither_source_fresh_skips(monkeypatch):
    # Both sources unavailable AND kickoff was very recent — the
    # cycle should hold and try again later, NOT enqueue.
    _stub_resolve_helpers(monkeypatch, fallback=None, age_seconds=15 * 60)
    decision = asyncio.run(
        _pipeline._resolve_soccer_settlement(
            market_id="MKT-EPL-FRESH-DOWN",
            league="EPL",
            sportradar_summary=None,
        )
    )
    assert decision["action"] == "skip"
    assert decision["disagreement_kind"] is None


def test_resolve_soccer_secondary_down_primary_stale_queues(monkeypatch):
    # Validator-flagged required scenario: ESPN unavailable AND
    # Sportradar still hasn't published a final scoreline more than 3h
    # past kickoff.  Without queuing here the market would sit forever
    # waiting for a primary verdict that may never come (Task #769
    # done criterion).
    _stub_resolve_helpers(monkeypatch, fallback=None, age_seconds=5 * 3600)
    decision = asyncio.run(
        _pipeline._resolve_soccer_settlement(
            market_id="MKT-EPL-BOTH-DOWN-STALE",
            league="EPL",
            sportradar_summary=None,
        )
    )
    assert decision["action"] == "queue"
    assert decision["disagreement_kind"] == "primary_stale_secondary_down"
    assert decision["result_3way"] is None
    assert decision["primary"] is None
    assert decision["fallback"] is None


def test_resolve_soccer_neither_source_no_schedule_skips(monkeypatch):
    # Both sources unavailable AND we don't even have a scheduled
    # kickoff on file (age=None) — can't say whether primary is
    # actually stale, so hold rather than auto-enqueue.
    _stub_resolve_helpers(monkeypatch, fallback=None, age_seconds=None)
    decision = asyncio.run(
        _pipeline._resolve_soccer_settlement(
            market_id="MKT-EPL-NO-SCHEDULE",
            league="EPL",
            sportradar_summary=None,
        )
    )
    assert decision["action"] == "skip"
    assert decision["disagreement_kind"] is None


def test_resolve_soccer_unsupported_league_skips_fallback(monkeypatch):
    # League with no ESPN slug — _espn_soccer_match_summary should not
    # even be invoked; fall through to "primary only" semantics.
    espn_calls: list[str] = []

    async def _fake_espn(market_id: str) -> dict[str, Any] | None:
        espn_calls.append(market_id)
        return None

    async def _fake_age(_market_id: str) -> float | None:
        return None

    monkeypatch.setattr(_pipeline, "_espn_soccer_match_summary", _fake_espn)
    monkeypatch.setattr(_pipeline, "_market_scheduled_age_seconds", _fake_age)

    decision = asyncio.run(
        _pipeline._resolve_soccer_settlement(
            market_id="MKT-X-UNK",
            league="UNKNOWN_LEAGUE",
            sportradar_summary=_summary("home"),
        )
    )
    assert decision["action"] == "settle"
    assert decision["result_3way"] == "home"
    assert espn_calls == [], "fallback should be skipped for unsupported leagues"


# ---------------------------------------------------------------------------
# enqueue_soccer_review — idempotent on open market
# ---------------------------------------------------------------------------


def test_enqueue_soccer_review_idempotent(monkeypatch):
    inserted: list[tuple[str, str]] = []

    async def _fake_fetchval(query: str, *args: Any) -> Any:
        # Simulate one open row already present after the first insert.
        if "FROM soccer_settlement_review" in query:
            return 1 if inserted else None
        return None

    async def _fake_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO soccer_settlement_review" in query:
            inserted.append((args[0], args[5]))  # market_id, disagreement_kind
            return {"id": 42}
        return None

    monkeypatch.setattr(_pipeline.db, "fetchval", _fake_fetchval)
    monkeypatch.setattr(_pipeline.db, "fetchrow", _fake_fetchrow)

    rid1 = asyncio.run(
        _pipeline.enqueue_soccer_review(
            market_id="MKT-EPL-DUP",
            league="EPL",
            game_id="game-1",
            primary=_summary("home"),
            fallback=_summary("away", source="espn"),
            disagreement_kind="disagree",
        )
    )
    rid2 = asyncio.run(
        _pipeline.enqueue_soccer_review(
            market_id="MKT-EPL-DUP",
            league="EPL",
            game_id="game-1",
            primary=_summary("home"),
            fallback=_summary("away", source="espn"),
            disagreement_kind="disagree",
        )
    )
    assert rid1 == 42
    assert rid2 is None, "second enqueue should no-op when an open row exists"
    assert len(inserted) == 1


# ---------------------------------------------------------------------------
# apply_soccer_review_resolution
# ---------------------------------------------------------------------------


def _stub_resolve_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    review: dict[str, Any] | None,
) -> dict[str, list[Any]]:
    captured: dict[str, list[Any]] = {
        "audit": [],
        "update": [],
        "settle": [],
    }

    async def _fake_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM soccer_settlement_review" in query and "WHERE id" in query:
            return review
        return None

    async def _fake_execute(query: str, *args: Any) -> str:
        if "INSERT INTO admin_audit_log" in query:
            captured["audit"].append(
                {
                    "actor_user_id": args[0],
                    "actor_email": args[1],
                    "action_before": json.loads(args[4]) if isinstance(args[4], str) else args[4],
                    "action_after": json.loads(args[5]) if isinstance(args[5], str) else args[5],
                }
            )
        elif "UPDATE soccer_settlement_review" in query:
            captured["update"].append(args)
        return "OK"

    async def _fake_settle(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["settle"].append({"args": args, "kwargs": kwargs})
        return {
            "trades_settled": 3,
            "persona_metrics_updated": 2,
            "alerts_raised": 0,
        }

    monkeypatch.setattr(_pipeline.db, "fetchrow", _fake_fetchrow)
    monkeypatch.setattr(_pipeline.db, "execute", _fake_execute)
    monkeypatch.setattr(_pipeline, "settle_market", _fake_settle)
    return captured


def _make_review_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 7,
        "market_id": "MKT-EPL-X",
        "game_id": "game-x",
        "league": "EPL",
        "primary_payload": json.dumps(_summary("home")),
        "fallback_payload": json.dumps(_summary("away", source="espn")),
        "disagreement_kind": "disagree",
        "resolved_at": None,
        "resolution": None,
        "resolved_result": None,
        "settle_completed_at": None,
    }
    base.update(overrides)
    return base


def test_apply_resolution_sportradar_routes_through_settle_market(monkeypatch):
    cap = _stub_resolve_db(monkeypatch, review=_make_review_row())

    out = asyncio.run(
        _pipeline.apply_soccer_review_resolution(
            review_id=7,
            actor_user_id="user_admin1",
            actor_email="ops@example.com",
            resolution="sportradar",
            notes="primary verified via match centre",
        )
    )

    assert out["ok"] is True
    assert out["resolution"] == "sportradar"
    assert out["result_3way"] == "home"
    assert out["trades_settled"] == 3
    # Audit row written before settle, with the chosen resolution.
    assert len(cap["audit"]) == 1
    assert cap["audit"][0]["action_after"]["resolution"] == "sportradar"
    assert cap["audit"][0]["action_after"]["result_3way"] == "home"
    # UPDATE flips the row closed.
    assert len(cap["update"]) == 1
    upd_args = cap["update"][0]
    assert upd_args[0] == 7
    assert upd_args[3] == "sportradar"
    assert upd_args[4] == "home"
    # settle_market got the right 3-way result and source payload.
    assert len(cap["settle"]) == 1
    settle_kwargs = cap["settle"][0]["kwargs"]
    assert settle_kwargs["result_3way"] == "home"
    assert settle_kwargs["match_summary"]["source"] == "sportradar"


def test_apply_resolution_fallback_uses_espn_payload(monkeypatch):
    cap = _stub_resolve_db(monkeypatch, review=_make_review_row())
    out = asyncio.run(
        _pipeline.apply_soccer_review_resolution(
            review_id=7,
            actor_user_id="user_admin1",
            actor_email="ops@example.com",
            resolution="fallback",
            notes=None,
        )
    )
    assert out["result_3way"] == "away"
    settle_kwargs = cap["settle"][0]["kwargs"]
    assert settle_kwargs["match_summary"]["source"] == "espn"


def test_apply_resolution_manual_overrides(monkeypatch):
    cap = _stub_resolve_db(monkeypatch, review=_make_review_row())
    out = asyncio.run(
        _pipeline.apply_soccer_review_resolution(
            review_id=7,
            actor_user_id="user_admin1",
            actor_email="ops@example.com",
            resolution="manual_draw",
            notes="confirmed via tv replay",
        )
    )
    assert out["result_3way"] == "draw"
    settle_kwargs = cap["settle"][0]["kwargs"]
    assert settle_kwargs["match_summary"]["source"] == "manual"
    assert settle_kwargs["result_3way"] == "draw"


def test_apply_resolution_rejects_unknown_resolution(monkeypatch):
    _stub_resolve_db(monkeypatch, review=_make_review_row())
    with pytest.raises(ValueError, match="unknown soccer review resolution"):
        asyncio.run(
            _pipeline.apply_soccer_review_resolution(
                review_id=7,
                actor_user_id="u",
                actor_email=None,
                resolution="bogus",
            )
        )


def test_apply_resolution_rejects_already_resolved(monkeypatch):
    # A row with BOTH resolved_at and settle_completed_at set is fully
    # resolved end-to-end — second resolve attempts must be rejected
    # so the operator UI doesn't double-settle.
    review = _make_review_row(
        resolved_at=datetime.now(timezone.utc),
        settle_completed_at=datetime.now(timezone.utc),
        resolution="sportradar",
        resolved_result="home",
    )
    _stub_resolve_db(monkeypatch, review=review)
    with pytest.raises(ValueError, match="already resolved"):
        asyncio.run(
            _pipeline.apply_soccer_review_resolution(
                review_id=7,
                actor_user_id="u",
                actor_email=None,
                resolution="sportradar",
            )
        )


def test_apply_resolution_settle_failure_leaves_row_open_for_retry(monkeypatch):
    """settle_market raising must NOT mark the row resolved.

    Validator-flagged correctness fix: previously the row was flipped
    closed *before* settle ran, so a transient settle failure left the
    market unsettled but locked out of further resolve attempts.  Now
    settle runs first and the UPDATE only fires on success.
    """
    update_calls: list[Any] = []
    settle_calls: list[Any] = []

    async def _fake_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM soccer_settlement_review" in query and "WHERE id" in query:
            return _make_review_row()
        return None

    async def _fake_execute(query: str, *args: Any) -> str:
        if "UPDATE soccer_settlement_review" in query:
            update_calls.append(args)
        # admin_audit_log is fire-and-forget and may run; we don't
        # care for this assertion.
        return "OK"

    async def _fake_settle_failing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        settle_calls.append({"args": args, "kwargs": kwargs})
        raise RuntimeError("kalshi timeout")

    monkeypatch.setattr(_pipeline.db, "fetchrow", _fake_fetchrow)
    monkeypatch.setattr(_pipeline.db, "execute", _fake_execute)
    monkeypatch.setattr(_pipeline, "settle_market", _fake_settle_failing)

    with pytest.raises(RuntimeError, match="kalshi timeout"):
        asyncio.run(
            _pipeline.apply_soccer_review_resolution(
                review_id=7,
                actor_user_id="user_admin1",
                actor_email="ops@example.com",
                resolution="sportradar",
            )
        )

    assert len(settle_calls) == 1, "settle_market should have been attempted"
    assert update_calls == [], (
        "review row must NOT be flipped closed when settle_market fails — "
        "the operator must be able to retry"
    )


def test_apply_resolution_retry_after_partial_settle_succeeds(monkeypatch):
    """A previous resolved-but-unsettled row must accept a retry.

    settle_completed_at IS NULL on the prior attempt → the second call
    is allowed through and runs settle + completes the row.
    """
    captured = _stub_resolve_db(
        monkeypatch,
        review=_make_review_row(
            # resolved_at set by an earlier (partial) attempt that
            # crashed before settle returned, but settle_completed_at
            # is still NULL.
            resolved_at=datetime.now(timezone.utc),
            resolution="sportradar",
            resolved_result="home",
            settle_completed_at=None,
        ),
    )

    out = asyncio.run(
        _pipeline.apply_soccer_review_resolution(
            review_id=7,
            actor_user_id="user_admin1",
            actor_email="ops@example.com",
            resolution="sportradar",
        )
    )
    assert out["ok"] is True
    assert len(captured["settle"]) == 1, "retry should have re-run settle"
    assert len(captured["update"]) == 1, "retry should re-stamp settle_completed_at"


def test_apply_resolution_rejects_missing_review(monkeypatch):
    _stub_resolve_db(monkeypatch, review=None)
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(
            _pipeline.apply_soccer_review_resolution(
                review_id=999,
                actor_user_id="u",
                actor_email=None,
                resolution="sportradar",
            )
        )


# ---------------------------------------------------------------------------
# Dashboard tile — RED when the oldest open review > 6h
# ---------------------------------------------------------------------------


def test_dashboard_tile_red_when_oldest_above_threshold(monkeypatch):
    from app import dashboard

    async def _fake_fetchrow(_q: str, *_a: Any) -> dict[str, Any]:
        return {
            "open_count": 2,
            "oldest_age_seconds": 7 * 3600.0,  # 7h, above the 6h threshold
            "newest_market_id": "MKT-EPL-Z",
        }

    async def _fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "market_id": "MKT-EPL-Z",
                "league": "EPL",
                "disagreement_kind": "disagree",
                "created_at": "2026-05-04T10:00:00Z",
                "age_seconds": 7 * 3600.0,
            }
        ]

    monkeypatch.setattr(dashboard.db, "fetchrow", _fake_fetchrow)
    monkeypatch.setattr(dashboard.db, "fetch", _fake_fetch)

    summary = asyncio.run(dashboard.get_soccer_settlement_review_summary())
    assert summary["open_count"] == 2
    assert summary["red"] is True
    assert summary["oldest_age_seconds"] == pytest.approx(7 * 3600)
    assert summary["red_threshold_seconds"] == pytest.approx(6 * 3600)
    assert len(summary["items"]) == 1


def test_dashboard_tile_green_below_threshold(monkeypatch):
    from app import dashboard

    async def _fake_fetchrow(_q: str, *_a: Any) -> dict[str, Any]:
        return {
            "open_count": 1,
            "oldest_age_seconds": 30 * 60.0,
            "newest_market_id": "MKT-EPL-A",
        }

    async def _fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "market_id": "MKT-EPL-A",
                "league": "EPL",
                "disagreement_kind": "disagree",
                "created_at": "2026-05-04T15:00:00Z",
                "age_seconds": 30 * 60.0,
            }
        ]

    monkeypatch.setattr(dashboard.db, "fetchrow", _fake_fetchrow)
    monkeypatch.setattr(dashboard.db, "fetch", _fake_fetch)

    summary = asyncio.run(dashboard.get_soccer_settlement_review_summary())
    assert summary["open_count"] == 1
    assert summary["red"] is False


def test_dashboard_tile_empty_queue(monkeypatch):
    from app import dashboard

    async def _fake_fetchrow(_q: str, *_a: Any) -> dict[str, Any]:
        return {
            "open_count": 0,
            "oldest_age_seconds": None,
            "newest_market_id": None,
        }

    monkeypatch.setattr(dashboard.db, "fetchrow", _fake_fetchrow)

    summary = asyncio.run(dashboard.get_soccer_settlement_review_summary())
    assert summary["open_count"] == 0
    assert summary["red"] is False
    assert summary["oldest_age_seconds"] is None
    assert summary["items"] == []


# ---------------------------------------------------------------------------
# ESPN adapter — name normalisation, parsing, opt-out flag
# ---------------------------------------------------------------------------


def test_espn_name_score_handles_diacritics_and_stopwords():
    # Real Madrid CF vs Real Madrid — stopword "CF" stripped, dominant
    # tokens identical → score above the matcher threshold (0.34).
    assert espn_soccer._name_score("Real Madrid CF", "Real Madrid") >= 0.5
    # São Paulo vs Sao Paulo — diacritics folded.
    assert espn_soccer._name_score("São Paulo", "Sao Paulo") >= 0.5
    # Identical full names — perfect score.
    assert espn_soccer._name_score("Manchester City", "Manchester City") == 1.0
    # Real Madrid vs Real Sociedad — only the generic "real" is shared;
    # jaccard over distinctive tokens stays low so the matcher won't
    # cross-pair them.
    assert espn_soccer._name_score("Real Madrid", "Real Sociedad") < 0.5


def test_espn_supported_leagues_includes_three_way_set():
    leagues = espn_soccer.supported_leagues()
    for league in ("EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL"):
        assert league in leagues


def test_espn_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("AXIOM_ESPN_SOCCER_DISABLED", "1")
    out = asyncio.run(
        espn_soccer.fetch_match_summary(
            league="EPL", home_team="Arsenal", away_team="Chelsea"
        )
    )
    assert out is None


_ESPN_EVENT_SEQ = {"n": 0}


def _espn_event(home: str, away: str, hs: int, as_: int, *, state: str = "post") -> dict[str, Any]:
    # Each event needs a unique id; the adapter dedupes by id when it
    # probes ±1 day around the scheduled kickoff so a fixture with
    # collisions would collapse the scoreboard down to a single entry.
    _ESPN_EVENT_SEQ["n"] += 1
    eid = f"e{_ESPN_EVENT_SEQ['n']}"
    return {
        "id": eid,
        "date": "2026-05-04T19:00Z",
        "competitions": [
            {
                "date": "2026-05-04T19:00Z",
                "status": {"type": {"state": state, "name": "STATUS_FULL_TIME"}},
                "competitors": [
                    {
                        "homeAway": "home",
                        "score": str(hs),
                        "team": {"displayName": home},
                    },
                    {
                        "homeAway": "away",
                        "score": str(as_),
                        "team": {"displayName": away},
                    },
                ],
            }
        ],
    }


def test_espn_fetch_match_summary_picks_correct_match(monkeypatch):
    monkeypatch.delenv("AXIOM_ESPN_SOCCER_DISABLED", raising=False)
    body = {
        "events": [
            _espn_event("Liverpool", "Brighton", 1, 1),
            _espn_event("Arsenal FC", "Chelsea FC", 3, 0),  # the one we want
            _espn_event("Tottenham Hotspur", "Aston Villa", 2, 4),
        ]
    }

    async def _fake_fetch_scoreboard(_league: str, _when: Any) -> dict[str, Any]:
        return body

    monkeypatch.setattr(espn_soccer, "_fetch_scoreboard", _fake_fetch_scoreboard)
    out = asyncio.run(
        espn_soccer.fetch_match_summary(
            league="EPL",
            home_team="Arsenal",
            away_team="Chelsea",
            scheduled_at=datetime(2026, 5, 4, 19, 0, tzinfo=timezone.utc),
        )
    )
    assert out is not None
    assert out["result"] == "home"
    assert out["home_score"] == 3
    assert out["away_score"] == 0
    assert out["source"] == "espn"


def test_espn_fetch_returns_none_when_match_not_final(monkeypatch):
    monkeypatch.delenv("AXIOM_ESPN_SOCCER_DISABLED", raising=False)
    body = {"events": [_espn_event("Arsenal", "Chelsea", 1, 0, state="in")]}

    async def _fake_fetch_scoreboard(_league: str, _when: Any) -> dict[str, Any]:
        return body

    monkeypatch.setattr(espn_soccer, "_fetch_scoreboard", _fake_fetch_scoreboard)
    out = asyncio.run(
        espn_soccer.fetch_match_summary(
            league="EPL", home_team="Arsenal", away_team="Chelsea"
        )
    )
    assert out is None


def test_espn_fetch_returns_none_for_unsupported_league(monkeypatch):
    monkeypatch.delenv("AXIOM_ESPN_SOCCER_DISABLED", raising=False)
    fetched: list[str] = []

    async def _fake_fetch_scoreboard(league: str, _when: Any) -> dict[str, Any]:
        fetched.append(league)
        return {}

    monkeypatch.setattr(espn_soccer, "_fetch_scoreboard", _fake_fetch_scoreboard)
    out = asyncio.run(
        espn_soccer.fetch_match_summary(
            league="UNKNOWN", home_team="A", away_team="B"
        )
    )
    assert out is None
    assert fetched == [], "unsupported leagues must short-circuit before HTTP"


def test_espn_fetch_no_matching_team_returns_none(monkeypatch):
    monkeypatch.delenv("AXIOM_ESPN_SOCCER_DISABLED", raising=False)
    body = {"events": [_espn_event("Liverpool", "Brighton", 1, 1)]}

    async def _fake_fetch_scoreboard(_league: str, _when: Any) -> dict[str, Any]:
        return body

    monkeypatch.setattr(espn_soccer, "_fetch_scoreboard", _fake_fetch_scoreboard)
    out = asyncio.run(
        espn_soccer.fetch_match_summary(
            league="EPL", home_team="Arsenal", away_team="Chelsea"
        )
    )
    assert out is None
