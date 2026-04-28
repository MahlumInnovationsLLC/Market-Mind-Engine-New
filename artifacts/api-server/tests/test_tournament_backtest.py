"""Tests for the tournament-replay backtest harness (task #697).

The harness lives in :mod:`app.sports.tournament_backtest` and walks
every saved leaderboard snapshot of a finished tournament, prices each
one through Plackett-Luce, and grades the resulting probabilities
against the actual winner.  Coverage:

* Pure scoring math: per-snapshot Brier / log-loss, the rolled-up
  accuracy ("how often was the actual winner our top pick"), and the
  zero-snapshot fall-through.
* Winner-inference: defaults to the position-1 entrant of the most
  recent stored snapshot, with an explicit override honoured.
* Date-range sweep: walks every distinct event with snapshots in the
  window, optionally filtered by league.
* Replay never writes into the live ratings cache (a backtest run
  must not pollute live pricing for the same event).
* Admin endpoints (single-event + date-range) round-trip the harness
  payload and require admin auth.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import admin as admin_module
from app import auth as auth_module
from app import db as db_module
from app import main as main_module
from app.feeds import sportradar as sr
from app.main import app
from app.predictor_n_way import plackett_luce_from_mapping
from app.sports import (
    get_field,
    ratings as ratings_mod,
    tournament_backtest,
)
from tests.conftest import TEST_CLERK_HOST


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------------
# Pure scoring math
# ---------------------------------------------------------------------------


def _make_snapshot_conn(all_rows):
    """Build a fake DB connection that routes the iter-history vs the
    latest-snapshot SELECTs to the right rows, the way real Postgres
    would: the iter helper sees every row, the latest helper sees only
    rows whose ``fetched_at`` matches MAX(fetched_at).
    """
    class _Conn:
        async def fetch(self, query, *args):
            normalised = " ".join(query.split())
            if (
                "fetched_at = ( SELECT MAX(fetched_at)" in normalised
                or "fetched_at = (SELECT MAX(fetched_at)" in normalised
            ):
                if not all_rows:
                    return []
                latest = max(r["fetched_at"] for r in all_rows)
                return [r for r in all_rows if r["fetched_at"] == latest]
            return list(all_rows)

    return _Conn


def test_replay_event_scores_each_snapshot_against_actual_winner(monkeypatch):
    """Every snapshot's Plackett-Luce probability for the actual
    winner must be scored on the same Brier / log-loss axes the
    crypto backtest uses, and the rollup must average those across
    snapshots so operators can compare "how well we quoted this
    tournament" head-to-head with other events.
    """
    ratings_mod.clear_cache()

    t1 = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 26, 12, 5, tzinfo=timezone.utc)
    t3 = datetime(2026, 4, 26, 12, 10, tzinfo=timezone.utc)
    event_id = "sr:tournament:pga-1"
    canned_rows = [
        # Snapshot 1: Rahm leading, Scheffler 2nd.
        {"event_id": event_id, "position": 1, "name": "Rahm", "fetched_at": t1},
        {"event_id": event_id, "position": 2, "name": "Scheffler", "fetched_at": t1},
        # Snapshot 2: Scheffler takes the lead.
        {"event_id": event_id, "position": 1, "name": "Scheffler", "fetched_at": t2},
        {"event_id": event_id, "position": 2, "name": "Rahm", "fetched_at": t2},
        # Snapshot 3 (final): McIlroy wins, Rahm 2nd, Scheffler 3rd.
        {"event_id": event_id, "position": 1, "name": "McIlroy", "fetched_at": t3},
        {"event_id": event_id, "position": 2, "name": "Rahm", "fetched_at": t3},
        {"event_id": event_id, "position": 3, "name": "Scheffler", "fetched_at": t3},
    ]

    @asynccontextmanager
    async def fake_acquire():
        yield _make_snapshot_conn(canned_rows)()

    monkeypatch.setattr(db_module, "acquire", fake_acquire)
    # No schedule row + no override winner — should infer McIlroy from
    # the final snapshot's position-1 entrant.
    async def fake_fetchrow(*_a, **_k):
        return None

    monkeypatch.setattr(db_module, "fetchrow", fake_fetchrow)

    result = _run(
        tournament_backtest.replay_event(event_id)
    )

    assert result.event_id == "sr:tournament:pga-1"
    assert result.winner == "McIlroy"
    assert result.n_snapshots == 3

    # Per-snapshot probs match the live Plackett-Luce mapping the
    # orchestrator would have priced off.  McIlroy isn't in
    # snapshots 1 and 2 (yes_prob == 0), so brier == 1.0 there and
    # the log-loss term is the eps-clamped ceiling.  Snapshot 3 has
    # McIlroy at position 1 — the highest-rated entrant — so the
    # winner is also our top pick and accuracy increments.
    assert result.snapshots[0].yes_prob == 0.0
    assert result.snapshots[0].brier == pytest.approx(1.0)
    assert result.snapshots[0].top_pick == "Rahm"
    assert result.snapshots[0].top_pick_was_winner is False

    assert result.snapshots[1].yes_prob == 0.0
    assert result.snapshots[1].top_pick == "Scheffler"
    assert result.snapshots[1].top_pick_was_winner is False

    final_probs = plackett_luce_from_mapping(
        {
            "McIlroy": ratings_mod._rating_from_seed(1),
            "Rahm": ratings_mod._rating_from_seed(2),
            "Scheffler": ratings_mod._rating_from_seed(3),
        }
    )
    assert result.snapshots[2].yes_prob == pytest.approx(final_probs["McIlroy"])
    assert result.snapshots[2].top_pick == "McIlroy"
    assert result.snapshots[2].top_pick_was_winner is True

    # Rollup: accuracy = correct_top_picks / n_snapshots = 1/3.
    assert result.accuracy == pytest.approx(1 / 3)
    # Brier is the mean of the per-snapshot values.
    expected_brier = sum(s.brier for s in result.snapshots) / 3
    assert result.brier_score == pytest.approx(expected_brier)
    # Log-loss is the mean of the per-snapshot values.
    expected_log_loss = sum(s.log_loss for s in result.snapshots) / 3
    assert result.log_loss == pytest.approx(expected_log_loss)


def test_replay_event_does_not_touch_live_ratings_cache(monkeypatch):
    """The backtest harness must NOT write into the live
    ``app.sports.ratings`` cache — a replay run shouldn't be able to
    pollute the live pricing path for the same event.
    """
    ratings_mod.clear_cache()

    ts = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    event_id = "sr:tournament:pga-1"
    canned_rows = [
        {"event_id": event_id, "position": 1, "name": "Rahm", "fetched_at": ts},
        {"event_id": event_id, "position": 2, "name": "Scheffler", "fetched_at": ts},
    ]

    @asynccontextmanager
    async def fake_acquire():
        yield _make_snapshot_conn(canned_rows)()

    monkeypatch.setattr(db_module, "acquire", fake_acquire)

    async def fake_fetchrow(*_a, **_k):
        return None

    monkeypatch.setattr(db_module, "fetchrow", fake_fetchrow)

    result = _run(
        tournament_backtest.replay_event(event_id)
    )
    assert result.n_snapshots == 1
    # The replay path must never write into the live cache.
    assert get_field(event_id) is None


def test_replay_event_returns_zero_when_no_snapshots(monkeypatch):
    """A fresh event with no stored snapshots must produce an empty
    report rather than raising — operators see "nothing to replay"
    instead of an opaque 502.
    """
    async def fake_fetchrow(*_a, **_k):
        return None

    async def fake_latest(*_a, **_k):
        return []

    monkeypatch.setattr(db_module, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(
        sr, "latest_tournament_leaderboard", fake_latest,
    )

    # Even the iter helper returns nothing.
    class _Conn:
        async def fetch(self, *_a, **_k):
            return []

    @asynccontextmanager
    async def fake_acquire():
        yield _Conn()

    monkeypatch.setattr(db_module, "acquire", fake_acquire)

    result = _run(
        tournament_backtest.replay_event("sr:tournament:does-not-exist")
    )
    assert result.n_snapshots == 0
    assert result.winner is None
    assert result.snapshots == []
    assert result.accuracy == 0.0
    assert result.brier_score == 0.0
    assert result.log_loss == 0.0


def test_replay_event_explicit_winner_overrides_inferred(monkeypatch):
    """An explicit ``winner`` query param must bypass the
    final-snapshot inference — useful when a Kalshi outright market is
    a more authoritative settlement source.
    """
    ratings_mod.clear_cache()

    ts = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    event_id = "sr:tournament:pga-1"
    canned_rows = [
        {"event_id": event_id, "position": 1, "name": "Rahm", "fetched_at": ts},
        {"event_id": event_id, "position": 2, "name": "Scheffler", "fetched_at": ts},
    ]

    @asynccontextmanager
    async def fake_acquire():
        yield _make_snapshot_conn(canned_rows)()

    monkeypatch.setattr(db_module, "acquire", fake_acquire)

    async def fake_fetchrow(*_a, **_k):
        return None

    monkeypatch.setattr(db_module, "fetchrow", fake_fetchrow)

    result = _run(
        tournament_backtest.replay_event(
            event_id, winner="Scheffler",
        )
    )
    # Final-snapshot leader is "Rahm", but the override wins — the
    # report scores against Scheffler.
    assert result.winner == "Scheffler"
    assert result.snapshots[0].top_pick == "Rahm"
    assert result.snapshots[0].top_pick_was_winner is False
    # Scheffler is position 2 in the snapshot, so yes_prob > 0 but
    # less than Rahm's.
    assert 0.0 < result.snapshots[0].yes_prob < 0.5


def test_replay_events_in_range_walks_every_distinct_event(monkeypatch):
    """The date-range sweep yields one report per distinct event with
    snapshots in [start, end].  Per-event failures are isolated so one
    missing snapshot can't blank the rest of the sweep.

    The bounds must also be forwarded to ``replay_event`` so per-event
    metrics are computed over the *requested* slice — without this,
    a narrow range query would still trigger full-history replay work
    and produce metrics that don't match the operator's stated window.
    """
    captured_calls: list[dict[str, Any]] = []

    async def fake_replay_event(event_id, *, since=None, until=None, **_kwargs):
        captured_calls.append(
            {"event_id": event_id, "since": since, "until": until}
        )
        return tournament_backtest.TournamentReplayResult(
            event_id=event_id,
            league="PGA",
            name="The Masters",
            winner="Rahm",
            n_snapshots=3,
            accuracy=0.5,
            brier_score=0.25,
            log_loss=0.5,
            snapshots=[],
            started_at="2026-04-26T12:00:00+00:00",
            completed_at="2026-04-26T12:00:01+00:00",
        )

    monkeypatch.setattr(
        tournament_backtest, "replay_event", fake_replay_event,
    )

    distinct_rows = [
        {"event_id": "sr:tournament:pga-1"},
        {"event_id": "sr:race:f1-1"},
    ]

    seen_sql: list[tuple[str, tuple]] = []

    async def fake_fetch(query, *args):
        seen_sql.append((query, args))
        return distinct_rows

    monkeypatch.setattr(db_module, "fetch", fake_fetch)

    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 30, tzinfo=timezone.utc)
    results = _run(
        tournament_backtest.replay_events_in_range(start=start, end=end)
    )

    assert [r.event_id for r in results] == [
        "sr:tournament:pga-1",
        "sr:race:f1-1",
    ]
    assert [c["event_id"] for c in captured_calls] == [
        "sr:tournament:pga-1",
        "sr:race:f1-1",
    ]
    # The window bounds must be forwarded to every per-event replay so
    # the rolled-up Brier / log-loss / accuracy reflect only snapshots
    # inside the operator-requested window.
    for call in captured_calls:
        assert call["since"] == start
        assert call["until"] == end
    # The bounds must also round-trip into the SQL so the sweep doesn't
    # materialise the entire history client-side.
    assert seen_sql and seen_sql[0][1][:2] == (start, end)


def test_replay_events_in_range_window_bounds_filter_snapshots(monkeypatch):
    """Integration check: the window bounds must filter the per-event
    snapshot history end-to-end.  A snapshot outside [start, end] must
    not be scored, even though the event itself qualifies for the
    sweep because *some* of its snapshots fell inside the window.
    """
    ratings_mod.clear_cache()

    in_window = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    out_of_window = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    event_id = "sr:tournament:pga-1"

    # Two snapshots: one inside the window, one before it.  The sweep
    # window is [2026-04-25, 2026-04-30] so only `in_window` should be
    # scored.
    canned_rows = [
        {"event_id": event_id, "position": 1, "name": "Rahm", "fetched_at": out_of_window},
        {"event_id": event_id, "position": 2, "name": "Scheffler", "fetched_at": out_of_window},
        {"event_id": event_id, "position": 1, "name": "Scheffler", "fetched_at": in_window},
        {"event_id": event_id, "position": 2, "name": "Rahm", "fetched_at": in_window},
    ]

    @asynccontextmanager
    async def fake_acquire():
        # Wrap the fake conn so the iter helper sees only rows inside
        # the requested window — that's what the production query does
        # via `WHERE fetched_at BETWEEN $2 AND $3`.
        class _Conn:
            async def fetch(self, query, *args):
                normalised = " ".join(query.split())
                if (
                    "fetched_at = ( SELECT MAX(fetched_at)" in normalised
                    or "fetched_at = (SELECT MAX(fetched_at)" in normalised
                ):
                    latest = max(r["fetched_at"] for r in canned_rows)
                    return [r for r in canned_rows if r["fetched_at"] == latest]
                # iter_tournament_leaderboard_history filters by
                # fetched_at when bounds are supplied — args[1]/args[2]
                # carry the lower / upper bounds.
                lo = args[1] if len(args) >= 2 else None
                hi = args[2] if len(args) >= 3 else None
                rows = list(canned_rows)
                if lo is not None:
                    rows = [r for r in rows if r["fetched_at"] >= lo]
                if hi is not None:
                    rows = [r for r in rows if r["fetched_at"] <= hi]
                return rows

        yield _Conn()

    async def fake_fetchrow(*_a, **_k):
        return None

    async def fake_fetch_distinct(*_a, **_k):
        return [{"event_id": event_id}]

    monkeypatch.setattr(db_module, "acquire", fake_acquire)
    monkeypatch.setattr(db_module, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(db_module, "fetch", fake_fetch_distinct)

    start = datetime(2026, 4, 25, tzinfo=timezone.utc)
    end = datetime(2026, 4, 30, tzinfo=timezone.utc)
    results = _run(
        tournament_backtest.replay_events_in_range(start=start, end=end)
    )
    assert len(results) == 1
    # Only the in-window snapshot is scored — the pre-window snapshot
    # never makes it into the per-event metrics.
    assert results[0].n_snapshots == 1
    assert results[0].snapshots[0].fetched_at == in_window.isoformat()


def test_replay_events_in_range_filters_by_league(monkeypatch):
    """Optional ``league`` filter must surface in the SQL so PGA-only
    sweeps don't pull F1 events along for the ride.
    """
    seen_sql: list[tuple[str, tuple]] = []

    async def fake_fetch(query, *args):
        seen_sql.append((query, args))
        return []

    monkeypatch.setattr(db_module, "fetch", fake_fetch)

    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 30, tzinfo=timezone.utc)
    _run(
        tournament_backtest.replay_events_in_range(
            start=start, end=end, league="pga",
        )
    )

    assert seen_sql, "sweep must hit the DB"
    query, args = seen_sql[0]
    assert "sportradar_schedules" in query
    assert "upper(s.league) = $3" in query
    # League is uppercased before the query so the filter matches the
    # canonical PGA / F1 codes the schedule poller writes.
    assert args == (start, end, "PGA")


def test_replay_events_in_range_returns_empty_on_missing_table(monkeypatch):
    """A fresh DB without the snapshots table must yield ``[]`` rather
    than raising — the harness should fall back gracefully so the
    operator dashboard never blanks out on bootstrap.
    """
    async def boom(*_a, **_k):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(db_module, "fetch", boom)

    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 30, tzinfo=timezone.utc)
    results = _run(
        tournament_backtest.replay_events_in_range(start=start, end=end)
    )
    assert results == []


def test_record_run_skips_empty_replay(monkeypatch):
    """Empty replays (no snapshots recorded) must skip the persist
    step so the dashboard's recent-runs trend doesn't fill up with
    meaningless zero rows.
    """
    called = False

    async def fake_execute(*_a, **_k):
        nonlocal called
        called = True
        return "OK"

    monkeypatch.setattr(db_module, "execute", fake_execute)

    empty = tournament_backtest.TournamentReplayResult(
        event_id="sr:tournament:pga-1",
        league="PGA",
        name="The Masters",
        winner=None,
        n_snapshots=0,
        accuracy=0.0,
        brier_score=0.0,
        log_loss=0.0,
        snapshots=[],
        started_at="2026-04-26T12:00:00+00:00",
        completed_at="2026-04-26T12:00:01+00:00",
    )
    _run(tournament_backtest.record_run(empty))
    assert called is False


def test_record_run_persists_completed_replay(monkeypatch):
    """Non-empty replays must INSERT into the persisted-runs table
    with the rollup metrics + the inferred winner so the dashboard can
    reconstruct the summary without re-walking the snapshot history.
    """
    captured: list[tuple[str, tuple]] = []

    async def fake_execute(query, *args):
        captured.append((query, args))
        return "OK"

    monkeypatch.setattr(db_module, "execute", fake_execute)

    result = tournament_backtest.TournamentReplayResult(
        event_id="sr:tournament:pga-1",
        league="PGA",
        name="The Masters",
        winner="Rahm",
        n_snapshots=4,
        accuracy=0.75,
        brier_score=0.2,
        log_loss=0.4,
        snapshots=[],
        started_at="2026-04-26T12:00:00+00:00",
        completed_at="2026-04-26T12:00:05+00:00",
    )
    _run(tournament_backtest.record_run(result))
    assert len(captured) == 1
    query, args = captured[0]
    assert "INSERT INTO sportradar_tournament_replays" in query
    assert args[0] == "sr:tournament:pga-1"
    assert args[1] == "PGA"
    assert args[2] == "The Masters"
    assert args[3] == "Rahm"
    assert args[4] == 4
    assert args[5] == pytest.approx(0.75)
    assert args[6] == pytest.approx(0.2)
    assert args[7] == pytest.approx(0.4)


def test_list_recent_runs_returns_empty_on_missing_table(monkeypatch):
    """Newly-bootstrapped dev DBs without the ``sportradar_tournament_replays``
    table must return ``[]`` rather than 500 — the dashboard's
    recent-runs panel renders an empty state and the operator can
    move on.
    """
    async def boom(*_a, **_k):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(db_module, "fetch", boom)
    items = _run(tournament_backtest.list_recent_runs(limit=10))
    assert items == []


def test_list_recent_runs_normalises_persisted_rows(monkeypatch):
    """The dashboard panel expects ISO timestamps + numeric metrics.
    The list helper must coerce ``asyncpg``-style row objects into the
    plain-dict shape the JSON serializer can hand back unchanged.
    """
    completed = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    started = datetime(2026, 4, 27, 11, 59, tzinfo=timezone.utc)

    async def fake_fetch(*_a, **_k):
        return [
            {
                "id": 7,
                "event_id": "sr:tournament:pga-1",
                "league": "PGA",
                "event_name": "The Masters",
                "winner": "Rahm",
                "n_snapshots": 12,
                "accuracy": 0.5,
                "brier_score": 0.21,
                "log_loss": 0.62,
                "started_at": started,
                "completed_at": completed,
            }
        ]

    monkeypatch.setattr(db_module, "fetch", fake_fetch)
    items = _run(tournament_backtest.list_recent_runs(limit=10))
    assert items == [
        {
            "id": 7,
            "event_id": "sr:tournament:pga-1",
            "league": "PGA",
            "event_name": "The Masters",
            "winner": "Rahm",
            "n_snapshots": 12,
            "accuracy": 0.5,
            "brier_score": 0.21,
            "log_loss": 0.62,
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
        }
    ]


# ---------------------------------------------------------------------------
# Admin endpoint round-trip — auth, validation, payload shape.
# ---------------------------------------------------------------------------


ISSUER = f"https://{TEST_CLERK_HOST}"
ADMIN_UID = "user_admin_tournament_backtest"


class _StubSigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _StubJWKSClient:
    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, _token: str) -> _StubSigningKey:
        return _StubSigningKey(self._public_key)


@pytest.fixture(scope="module")
def _keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _mint(private_key, *, sub: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "sub": sub,
            "iat": now - 60,
            "nbf": now - 60,
            "exp": now + 3600,
            "sid": "sess_test",
        },
        private_key,
        algorithm="RS256",
    )


class _FakeUsersConn:
    def __init__(self, users: dict[str, dict[str, Any]]) -> None:
        self.users = users

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "from app_users" in " ".join(query.split()).lower():
            return self.users.get(args[0])
        return None

    async def execute(self, *_a: Any, **_k: Any) -> str:
        return "OK"

    async def fetch(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return []

    async def fetchval(self, *_a: Any, **_k: Any) -> Any:
        return None


class _FakeResponse:
    def __init__(self, status_code: int = 404, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def get(self, *_a: Any, **_k: Any) -> _FakeResponse:
        return _FakeResponse(404, {})

    async def patch(self, *_a: Any, **_k: Any) -> _FakeResponse:
        return _FakeResponse(200, {})


@pytest.fixture(autouse=True)
def _wire_env(monkeypatch: pytest.MonkeyPatch, _keypair):
    auth_module._frontend_api_host.cache_clear()
    auth_module._issuer.cache_clear()
    auth_module._jwks_client.cache_clear()
    auth_module._user_cache.clear()
    monkeypatch.setattr(
        auth_module,
        "_jwks_client",
        lambda: _StubJWKSClient(_keypair.public_key()),
    )
    monkeypatch.setattr(admin_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_admin_tournament_backtest")
    monkeypatch.delenv("OPERATOR_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)

    users: dict[str, dict[str, Any]] = {
        ADMIN_UID: {
            "clerk_user_id": ADMIN_UID,
            "email": "admin@example.com",
            "display_name": None,
            "approved": True,
            "is_admin": True,
            "denied_at": None,
            "activity_count": 0,
            "first_seen_at": None,
            "last_seen_at": None,
            "clerk_created_at": None,
            "clerk_last_sign_in_at": None,
            "clerk_synced_at": None,
        }
    }

    @asynccontextmanager
    async def fake_acquire():
        yield _FakeUsersConn(users)

    monkeypatch.setattr(db_module, "acquire", fake_acquire)
    monkeypatch.setattr(admin_module.db, "acquire", fake_acquire)

    yield

    auth_module._user_cache.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_admin_replay_requires_auth(client: TestClient) -> None:
    r = client.post("/api/admin/tournament-backtest/replay")
    assert r.status_code in (401, 403), r.text


def test_admin_replay_single_event_returns_report(
    client: TestClient, _keypair, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: stub the harness so the endpoint stays decoupled
    from Postgres, then confirm the rolled-up report + per-snapshot
    trace round-trip the response, the persist hook fires, and the
    explicit ``winner`` override is forwarded.
    """
    captured: dict[str, Any] = {}

    async def fake_replay_event(event_id, *, winner=None, since=None, until=None):
        captured["event_id"] = event_id
        captured["winner"] = winner
        captured["since"] = since
        captured["until"] = until
        return tournament_backtest.TournamentReplayResult(
            event_id=event_id,
            league="PGA",
            name="The Masters",
            winner=winner or "Rahm",
            n_snapshots=2,
            accuracy=0.5,
            brier_score=0.3,
            log_loss=0.6,
            snapshots=[
                tournament_backtest.SnapshotScore(
                    fetched_at="2026-04-26T12:00:00+00:00",
                    n_entrants=3,
                    yes_prob=0.4,
                    brier=0.36,
                    log_loss=0.92,
                    top_pick="Scheffler",
                    top_pick_was_winner=False,
                ),
                tournament_backtest.SnapshotScore(
                    fetched_at="2026-04-26T12:05:00+00:00",
                    n_entrants=3,
                    yes_prob=0.6,
                    brier=0.16,
                    log_loss=0.51,
                    top_pick="Rahm",
                    top_pick_was_winner=True,
                ),
            ],
            started_at="2026-04-26T12:00:00+00:00",
            completed_at="2026-04-26T12:00:05+00:00",
        )

    persisted: list[Any] = []

    async def fake_record_run(result):
        persisted.append(result)

    monkeypatch.setattr(
        tournament_backtest, "replay_event", fake_replay_event,
    )
    monkeypatch.setattr(
        main_module._tournament_backtest, "replay_event", fake_replay_event,
    )
    monkeypatch.setattr(
        tournament_backtest, "record_run", fake_record_run,
    )
    monkeypatch.setattr(
        main_module._tournament_backtest, "record_run", fake_record_run,
    )

    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.post(
        "/api/admin/tournament-backtest/replay"
        "?event_id=sr:tournament:pga-1&winner=Rahm",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_id"] == "sr:tournament:pga-1"
    assert body["winner"] == "Rahm"
    assert body["league"] == "PGA"
    assert body["n_snapshots"] == 2
    assert body["accuracy"] == 0.5
    assert body["brier_score"] == 0.3
    assert body["log_loss"] == 0.6
    # Per-snapshot trace round-trips with the same shape the dashboard
    # consumes.
    assert len(body["snapshots"]) == 2
    first = body["snapshots"][0]
    assert first["fetched_at"] == "2026-04-26T12:00:00+00:00"
    assert first["yes_prob"] == 0.4
    assert first["top_pick"] == "Scheffler"
    assert first["top_pick_was_winner"] is False

    assert captured["event_id"] == "sr:tournament:pga-1"
    assert captured["winner"] == "Rahm"
    assert captured["since"] is None
    assert captured["until"] is None

    # Persist hook fired with the replay result so the dashboard
    # recent-runs panel will pick it up on the next render.
    assert len(persisted) == 1
    assert persisted[0].event_id == "sr:tournament:pga-1"


def test_admin_replay_date_range_drops_per_snapshot_detail(
    client: TestClient, _keypair, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A date-range sweep returns one rollup per event (no
    per-snapshot trace) so the response stays compact when the
    operator scans a busy week.  Bounds round-trip as ISO strings and
    the persist hook fires once per event.
    """
    captured: dict[str, Any] = {}

    async def fake_sweep(*, start, end, league=None):
        captured["start"] = start
        captured["end"] = end
        captured["league"] = league
        return [
            tournament_backtest.TournamentReplayResult(
                event_id="sr:tournament:pga-1",
                league="PGA",
                name="The Masters",
                winner="Rahm",
                n_snapshots=2,
                accuracy=0.5,
                brier_score=0.3,
                log_loss=0.6,
                snapshots=[
                    tournament_backtest.SnapshotScore(
                        fetched_at="2026-04-26T12:00:00+00:00",
                        n_entrants=3,
                        yes_prob=0.4,
                        brier=0.36,
                        log_loss=0.92,
                        top_pick="Scheffler",
                        top_pick_was_winner=False,
                    ),
                ],
                started_at="2026-04-26T12:00:00+00:00",
                completed_at="2026-04-26T12:00:05+00:00",
            ),
            tournament_backtest.TournamentReplayResult(
                event_id="sr:race:f1-1",
                league="F1",
                name="Miami GP",
                winner="Verstappen",
                n_snapshots=4,
                accuracy=1.0,
                brier_score=0.05,
                log_loss=0.2,
                snapshots=[],
                started_at="2026-04-27T12:00:00+00:00",
                completed_at="2026-04-27T12:00:05+00:00",
            ),
        ]

    persisted: list[Any] = []

    async def fake_record_run(result):
        persisted.append(result)

    monkeypatch.setattr(
        tournament_backtest, "replay_events_in_range", fake_sweep,
    )
    monkeypatch.setattr(
        main_module._tournament_backtest,
        "replay_events_in_range",
        fake_sweep,
    )
    monkeypatch.setattr(
        tournament_backtest, "record_run", fake_record_run,
    )
    monkeypatch.setattr(
        main_module._tournament_backtest, "record_run", fake_record_run,
    )

    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.post(
        "/api/admin/tournament-backtest/replay"
        "?start=2026-04-26T00:00:00&end=2026-04-30T00:00:00&league=pga",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["start"] == "2026-04-26T00:00:00+00:00"
    assert body["end"] == "2026-04-30T00:00:00+00:00"
    # League surfaces uppercased so the panel can show a clean badge.
    assert body["league"] == "PGA"
    assert body["n_events"] == 2
    assert [e["event_id"] for e in body["events"]] == [
        "sr:tournament:pga-1",
        "sr:race:f1-1",
    ]
    # Per-snapshot detail is intentionally dropped from the sweep
    # response — operators drill in via single-event replay.
    assert all("snapshots" not in e for e in body["events"])

    # Bounds were forwarded as timezone-aware datetimes (naive ISO is
    # interpreted as UTC) and league was uppercased before the sweep.
    assert captured["start"].isoformat() == "2026-04-26T00:00:00+00:00"
    assert captured["end"].isoformat() == "2026-04-30T00:00:00+00:00"
    assert captured["league"] == "pga"  # raw value forwarded to harness

    # Persist hook fires once per event.
    assert len(persisted) == 2


def test_admin_replay_rejects_missing_bounds_without_event(
    client: TestClient, _keypair,
) -> None:
    """Without ``event_id``, both ``start`` and ``end`` are required
    so the harness can't be tricked into a full-table sweep by an
    accidental empty request.
    """
    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.post(
        "/api/admin/tournament-backtest/replay",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400, r.text
    assert "event_id" in r.json()["detail"]


def test_admin_replay_rejects_inverted_bounds(
    client: TestClient, _keypair,
) -> None:
    """``start > end`` must 400 instead of silently returning an empty
    sweep — operators get a fast signal they typed the dates the wrong
    way around.
    """
    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.post(
        "/api/admin/tournament-backtest/replay"
        "?start=2026-04-30T00:00:00&end=2026-04-26T00:00:00",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400, r.text
    assert "start" in r.json()["detail"]


def test_admin_replay_rejects_unparseable_bound(
    client: TestClient, _keypair,
) -> None:
    """A malformed ISO string must 400 instead of silently widening
    the replay window — a typo shouldn't cost the operator a few
    minutes of wondering why every snapshot is missing.
    """
    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.post(
        "/api/admin/tournament-backtest/replay"
        "?event_id=sr:tournament:pga-1&start=not-a-date",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400, r.text
    assert "start" in r.json()["detail"]


def test_admin_recent_returns_persisted_rows(
    client: TestClient, _keypair, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path for the persisted-runs lookup: stub
    ``list_recent_runs`` so the test stays decoupled from Postgres,
    then confirm the endpoint round-trips the rows in newest-first
    order behind the admin gate.
    """
    captured: dict[str, Any] = {}

    async def fake_list_recent_runs(*, limit: int = 50):
        captured["limit"] = limit
        return [
            {
                "id": 2,
                "event_id": "sr:race:f1-1",
                "league": "F1",
                "event_name": "Miami GP",
                "winner": "Verstappen",
                "n_snapshots": 4,
                "accuracy": 1.0,
                "brier_score": 0.05,
                "log_loss": 0.2,
                "started_at": "2026-04-27T12:00:00+00:00",
                "completed_at": "2026-04-27T12:00:05+00:00",
            },
            {
                "id": 1,
                "event_id": "sr:tournament:pga-1",
                "league": "PGA",
                "event_name": "The Masters",
                "winner": "Rahm",
                "n_snapshots": 12,
                "accuracy": 0.5,
                "brier_score": 0.21,
                "log_loss": 0.62,
                "started_at": "2026-04-26T11:59:00+00:00",
                "completed_at": "2026-04-26T12:00:00+00:00",
            },
        ]

    monkeypatch.setattr(
        tournament_backtest,
        "list_recent_runs",
        fake_list_recent_runs,
    )
    monkeypatch.setattr(
        main_module._tournament_backtest,
        "list_recent_runs",
        fake_list_recent_runs,
    )

    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/tournament-backtest/recent?limit=25",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert captured == {"limit": 25}
    assert [it["event_id"] for it in body["items"]] == [
        "sr:race:f1-1",
        "sr:tournament:pga-1",
    ]
    assert body["items"][0]["accuracy"] == 1.0
    assert body["items"][1]["brier_score"] == 0.21


def test_admin_recent_limit_validated(
    client: TestClient, _keypair,
) -> None:
    """``limit`` is clamped server-side via ``Query(ge=1, le=500)`` so
    operators can't ask for an absurd window.
    """
    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/tournament-backtest/recent?limit=9999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422, r.text


def test_dashboard_recent_endpoint_round_trips_payload(
    client: TestClient, _keypair, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard mirror sits behind the standard approved-user
    gate so the read-only operator panel can render without the
    admin-only badge — the response shape must match the admin
    endpoint so the panel can share a renderer.
    """
    async def fake_list_recent_runs(*, limit: int = 50):
        return [
            {
                "id": 1,
                "event_id": "sr:tournament:pga-1",
                "league": "PGA",
                "event_name": "The Masters",
                "winner": "Rahm",
                "n_snapshots": 12,
                "accuracy": 0.5,
                "brier_score": 0.21,
                "log_loss": 0.62,
                "started_at": "2026-04-26T11:59:00+00:00",
                "completed_at": "2026-04-26T12:00:00+00:00",
            }
        ]

    monkeypatch.setattr(
        tournament_backtest,
        "list_recent_runs",
        fake_list_recent_runs,
    )
    monkeypatch.setattr(
        main_module._tournament_backtest,
        "list_recent_runs",
        fake_list_recent_runs,
    )

    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/dashboard/tournament-backtest/recent",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["event_id"] == "sr:tournament:pga-1"
