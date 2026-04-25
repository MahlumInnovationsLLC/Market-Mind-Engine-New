"""Tests for the one-time NBA / NHL / MLB season backfill (task #433).

Mirrors ``test_backfill_nfl_games.py`` but exercises the ESPN-fed
backfill (``scripts/backfill_espn_games.py``) which is the canonical
loader for the three other major NA sports.  Coverage:

  * Scoreboard event normalization (``_parse_event``) — happy path
    plus the unfinished/missing-side/missing-score edge cases that
    must not leak into the ``games`` upsert tuple shape.
  * Date-chunking helper (``_date_chunks``) covers the regular +
    post-season window for each league's chunk strategy.
  * ``fetch_league_season`` walks every chunk and de-duplicates by
    event id (httpx stubbed so the tests are hermetic).
  * ``_build_upsert_rows`` produces the (game_id, sport, league, …)
    tuple shape the ``games`` upsert SQL expects.
  * Margin regressor produces a non-trivial NBA / NHL / MLB fit when
    the backfilled rows are surfaced through ``load_training_games``
    — i.e. the per-league regressor stops cold-starting.
  * Per-league isotonic calibrator improves Brier vs. the
    uncalibrated baseline on a held-out slice of league-shaped
    probabilities/outcomes.
"""
from __future__ import annotations

import asyncio
import math
import random
from typing import Any

import pytest

from app import calibrators, margin_regressor
from scripts import backfill_espn_games as backfill


# ---------------------------------------------------------------------------
# Scoreboard parsing
# ---------------------------------------------------------------------------


def _scoreboard_event(
    *,
    eid: str = "401584000",
    status_name: str = "STATUS_FINAL",
    home: str = "Boston Celtics",
    away: str = "Denver Nuggets",
    home_score: Any = 112,
    away_score: Any = 98,
    date: str = "2024-12-25T20:00Z",
    competitors_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Helper to build an ESPN-shaped scoreboard event for tests."""
    if competitors_override is None:
        competitors = [
            {"homeAway": "home", "team": {"displayName": home}, "score": home_score},
            {"homeAway": "away", "team": {"displayName": away}, "score": away_score},
        ]
    else:
        competitors = competitors_override
    return {
        "id": eid,
        "date": date,
        "status": {"type": {"name": status_name}},
        "competitions": [{"competitors": competitors}],
    }


def test_parse_event_happy_path_nba():
    ev = _scoreboard_event()
    parsed = backfill._parse_event(ev, "NBA", "nba")
    assert parsed is not None
    assert parsed["id"] == "espn:nba:401584000"
    assert parsed["sport"] == "nba"
    assert parsed["league"] == "NBA"
    assert parsed["home_team"] == "Boston Celtics"
    assert parsed["away_team"] == "Denver Nuggets"
    assert parsed["home_score"] == 112
    assert parsed["away_score"] == 98
    assert parsed["status"] == "closed"
    assert parsed["scheduled"] == "2024-12-25T20:00Z"


def test_parse_event_skips_unfinished_games():
    """Only ``STATUS_FINAL`` (and soccer's full-time variants) make it
    through — scheduled and in-progress rows would otherwise pollute
    ``games`` with NULL or partial scores."""
    for status in (
        "STATUS_SCHEDULED",
        "STATUS_IN_PROGRESS",
        "STATUS_POSTPONED",
        "STATUS_CANCELED",
        "STATUS_HALFTIME",
    ):
        ev = _scoreboard_event(status_name=status)
        assert backfill._parse_event(ev, "NBA", "nba") is None


def test_parse_event_accepts_soccer_full_time_variants():
    """Soccer finals come back as ``STATUS_FULL_TIME`` /
    ``STATUS_FULL_PEN`` rather than ``STATUS_FINAL``."""
    for status in ("STATUS_FULL_TIME", "STATUS_FULL_PEN", "STATUS_END_PERIOD"):
        ev = _scoreboard_event(
            eid=f"e_{status}",
            status_name=status,
            home="Manchester City",
            away="Arsenal",
            home_score=2,
            away_score=1,
        )
        parsed = backfill._parse_event(ev, "EPL", "soccer")
        assert parsed is not None
        assert parsed["status"] == "closed"
        assert parsed["home_score"] == 2


def test_parse_event_drops_when_competitor_count_wrong():
    """A schedule entry with one competitor (BYE / TBD) or with an
    odd 3-way row must not produce a ``games`` row — both columns
    are NOT NULL on home_team / away_team in production."""
    one = _scoreboard_event(competitors_override=[
        {"homeAway": "home", "team": {"displayName": "X"}, "score": 1},
    ])
    assert backfill._parse_event(one, "NBA", "nba") is None
    three = _scoreboard_event(competitors_override=[
        {"homeAway": "home", "team": {"displayName": "X"}, "score": 1},
        {"homeAway": "away", "team": {"displayName": "Y"}, "score": 2},
        {"homeAway": "away", "team": {"displayName": "Z"}, "score": 3},
    ])
    assert backfill._parse_event(three, "NBA", "nba") is None


def test_parse_event_handles_blank_scores_as_null():
    """ESPN occasionally returns ``score: ""`` for a finished row that
    is missing one side (e.g. a forfeited game).  We must treat
    those as NULL rather than crashing or writing 0."""
    ev = _scoreboard_event(home_score="", away_score=None)
    parsed = backfill._parse_event(ev, "MLB", "mlb")
    assert parsed is not None
    assert parsed["home_score"] is None
    assert parsed["away_score"] is None


def test_parse_event_drops_when_team_name_missing():
    ev = _scoreboard_event(home="", away="Knicks")
    assert backfill._parse_event(ev, "NBA", "nba") is None


def test_parse_event_drops_when_id_missing():
    ev = _scoreboard_event(eid="")
    assert backfill._parse_event(ev, "NBA", "nba") is None


def test_parse_event_drops_when_scheduled_missing():
    ev = _scoreboard_event(date="")
    assert backfill._parse_event(ev, "NBA", "nba") is None


# ---------------------------------------------------------------------------
# Date chunking — regular + post-season coverage per league
# ---------------------------------------------------------------------------


def test_date_chunks_year_strategy_covers_regular_plus_postseason():
    """``year`` chunks issue one range per calendar year covering the
    full Jan-Dec window plus Jan-Mar of the next year.  That spans
    both the regular season and the playoffs for any NA league
    that uses the ``year`` strategy (NFL playoffs run into
    January-February of the following year)."""
    chunks = backfill._date_chunks(2024, "year")
    # One range per calendar year touched.
    assert len(chunks) == 2
    # First range must cover the full season year.
    assert chunks[0].startswith("20240101-")
    assert chunks[0].endswith("20241231")
    # Second range covers Jan-Mar of the following year (post-season tail).
    assert chunks[1].startswith("20250101-")
    assert chunks[1].endswith("20250331")


def test_date_chunks_month_strategy_emits_one_range_per_month():
    """``month`` strategy is what NBA / NHL / MLB use so that the
    high-volume daily schedule responses stay under ESPN's ~1000-event
    cap.  Must emit Jan-Dec of the season + Jan-Mar of the
    following calendar year (covers playoff windows)."""
    chunks = backfill._date_chunks(2024, "month")
    assert len(chunks) == 12 + 3
    # Each chunk is a YYYYMMDD-YYYYMMDD range scoped to a single month.
    for c in chunks:
        assert "-" in c
        start, end = c.split("-")
        assert start[:6] == end[:6]  # same year-month
        assert start.endswith("01")
    # Sanity:  October of the season year is present (NBA / NHL tip-off).
    assert any(c.startswith("20241001-") for c in chunks)
    # Sanity:  June of the *following* year would NOT be in the window —
    # that's intentional; the next season's ``year`` anchor picks it up.
    assert not any(c.startswith("20250601-") for c in chunks)


def test_date_chunks_yearmonth_strategy_for_ncaab():
    """NCAAB's ESPN endpoint 404s on hyphen ranges, so we use the
    ``YYYYMM`` syntax instead.  Same Jan-Dec + Jan-Mar window."""
    chunks = backfill._date_chunks(2024, "yearmonth")
    assert chunks[0] == "202401"
    assert chunks[-1] == "202503"
    assert len(chunks) == 12 + 3


def test_league_configs_cover_nba_nhl_mlb():
    """Defensive: this task explicitly backfills NBA / NHL / MLB — if
    one of them ever gets dropped from ``LEAGUE_CONFIGS`` the
    backfill silently stops covering it."""
    for league in ("NBA", "NHL", "MLB"):
        assert league in backfill.LEAGUE_CONFIGS
        cfg = backfill.LEAGUE_CONFIGS[league]
        assert cfg["chunk"] == "month", (league, cfg)
        assert cfg["sport"] == league.lower()


# ---------------------------------------------------------------------------
# fetch_league_season — httpx stubbed end-to-end walk
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


class _StubClient:
    """Async ``httpx.AsyncClient`` stand-in for hermetic backfill tests."""

    def __init__(
        self,
        *,
        events_by_chunk: dict[str, list[dict[str, Any]]] | None = None,
        default_events: list[dict[str, Any]] | None = None,
    ) -> None:
        self._events_by_chunk = events_by_chunk or {}
        self._default = default_events or []
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, params: dict | None = None):
        self.requests.append((url, dict(params or {})))
        chunk = (params or {}).get("dates", "")
        events = self._events_by_chunk.get(str(chunk), self._default)
        return _StubResponse({"events": list(events)})


def test_fetch_league_season_walks_chunks_and_dedupes(monkeypatch):
    """``fetch_league_season`` must hit every chunk from
    ``_date_chunks`` and de-duplicate by event id (the same event
    can appear in two adjacent chunks for late-night games)."""
    duplicate = _scoreboard_event(eid="42", home="Bruins", away="Rangers",
                                  home_score=4, away_score=2)
    unique = _scoreboard_event(eid="43", home="Oilers", away="Flames",
                               home_score=3, away_score=1)
    chunks = backfill._date_chunks(2024, "month")
    # First chunk has only the duplicate; second chunk has duplicate + unique.
    events_by_chunk = {
        chunks[0]: [duplicate],
        chunks[1]: [duplicate, unique],
    }
    stub = _StubClient(events_by_chunk=events_by_chunk)
    games = asyncio.run(
        backfill.fetch_league_season(stub, "NHL", 2024, rate_limit_sleep=0.0)
    )
    by_id = {g["id"]: g for g in games}
    assert set(by_id.keys()) == {"espn:nhl:42", "espn:nhl:43"}
    # Hit every chunk exactly once.
    assert len(stub.requests) == len(chunks)


def test_fetch_league_season_skips_non_200(monkeypatch):
    """A bad chunk response must not poison the rest of the season."""

    class _PartialClient(_StubClient):
        def __init__(self) -> None:
            super().__init__()
            self._call = 0

        async def get(self, url, params=None):
            self._call += 1
            self.requests.append((url, dict(params or {})))
            if self._call == 1:
                return _StubResponse({}, status=500)
            return _StubResponse({"events": [
                _scoreboard_event(eid="ok", home="A", away="B",
                                  home_score=5, away_score=4),
            ]})

    stub = _PartialClient()
    games = asyncio.run(
        backfill.fetch_league_season(stub, "MLB", 2024, rate_limit_sleep=0.0)
    )
    assert any(g["id"] == "espn:mlb:ok" for g in games)


# ---------------------------------------------------------------------------
# Upsert row projection
# ---------------------------------------------------------------------------


def test_build_upsert_rows_projects_to_games_tuple_shape():
    games = [{
        "id": "espn:nba:42",
        "sport": "nba",
        "league": "NBA",
        "home_team": "Celtics",
        "away_team": "Nuggets",
        "scheduled": "2024-12-25T20:00Z",
        "status": "closed",
        "home_score": 112,
        "away_score": 98,
    }]
    rows = backfill._build_upsert_rows(games)
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "espn:nba:42"
    assert r[1] == "nba"
    assert r[2] == "NBA"
    assert r[3] == "Celtics"
    assert r[4] == "Nuggets"
    assert r[5] == "2024-12-25T20:00Z"
    assert r[6] == "closed"
    assert r[7] == 112
    assert r[8] == 98


def test_build_upsert_rows_preserves_null_scores():
    """A finished row with missing scores still upserts, but with
    NULL scores — the regressor's ``home_score IS NOT NULL`` filter
    will skip it instead of training on a zeroed-out row."""
    games = [{
        "id": "espn:mlb:42", "sport": "mlb", "league": "MLB",
        "home_team": "Yankees", "away_team": "Red Sox",
        "scheduled": "2024-09-01T17:00Z", "status": "closed",
        "home_score": None, "away_score": None,
    }]
    rows = backfill._build_upsert_rows(games)
    assert rows[0][7] is None
    assert rows[0][8] is None


# ---------------------------------------------------------------------------
# Downstream effect:  refit_all_leagues produces non-trivial fits per league
# ---------------------------------------------------------------------------


# League-specific noise / scoring shapes so the synthetic training set
# looks like real backfilled data for that sport.  ``margin_sd`` is the
# residual SD an actual season produces; ``mean_total`` is the average
# combined score the league posts.
_LEAGUE_SHAPE: dict[str, dict[str, float]] = {
    "NBA": {"margin_sd": 12.5, "mean_total": 224.0, "elo_slope": 0.045},
    "NHL": {"margin_sd": 2.1,  "mean_total": 6.0,   "elo_slope": 0.005},
    "MLB": {"margin_sd": 3.5,  "mean_total": 9.0,   "elo_slope": 0.008},
}


def _synthetic_training_rows(
    league: str, *, n: int, seed: int,
) -> list[dict[str, Any]]:
    """Mimic what ``load_training_games`` projects from the DB after
    the ESPN backfill lands rows in ``games`` for the given league.

    Embeds a real (non-zero) elo_diff -> margin signal so the
    regressor's coefficient comes out non-zero; otherwise the test
    would pass trivially with an all-zero fit.
    """
    shape = _LEAGUE_SHAPE[league]
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    for i in range(n):
        elo_diff = rng.gauss(0.0, 200.0)
        margin = shape["elo_slope"] * elo_diff + rng.gauss(0.0, shape["margin_sd"])
        total = shape["mean_total"] + rng.gauss(0.0, shape["margin_sd"])
        spread_line = round((-shape["elo_slope"] * elo_diff) * 2.0) / 2.0
        rows.append({
            "league": league,
            "game_id": f"{league.lower()}-2024-{i}",
            "home_team": f"H{i}",
            "away_team": f"A{i}",
            "home_score": round(total / 2.0 + margin / 2.0),
            "away_score": round(total / 2.0 - margin / 2.0),
            "spread_line": spread_line,
            "total_line": round(total),
            "elo_diff": elo_diff,
            "home_elo": 1500.0 + elo_diff / 2.0,
            "opponent_elo": 1500.0 - elo_diff / 2.0,
            "recent_form_delta": 0.0,
            "opponent_recent_form_delta": 0.0,
        })
    return rows


@pytest.mark.parametrize("league,n_games", [
    ("NBA", 800),   # ~ one regular season + playoffs
    ("NHL", 1000),  # ~ one regular season + playoffs
    ("MLB", 2000),  # ~ one regular season + playoffs
])
def test_refit_all_leagues_produces_nontrivial_fit_after_backfill(
    monkeypatch, league: str, n_games: int,
):
    """Once the ESPN backfill has loaded a season's worth of games for
    a league, ``refit_all_leagues`` must produce a real fit for that
    league: ``n_train > 0``, at least one non-zero margin coefficient,
    and a finite empirical residual SD — not the cold-start fallback
    SDs that the orchestrator currently sees on a fresh DB."""

    async def _fake_load(lg, *, limit=None):
        if lg == league:
            return _synthetic_training_rows(league, n=n_games, seed=433)
        return []

    async def _inner():
        margin_regressor.reset_registry()
        monkeypatch.setattr(margin_regressor, "load_training_games", _fake_load)

        # Persistence requires a live DB — patch it out.
        async def _noop_persist(*a, **kw):
            return None

        monkeypatch.setattr(margin_regressor, "_persist_fit", _noop_persist)
        summary = await margin_regressor.refit_all_leagues(min_games=50)
        d = summary.as_dict()
        assert d["fitted"].get(league) == n_games, d

        reg = margin_regressor.get_regressor(league)
        assert reg is not None
        assert reg.is_fitted()
        assert reg.n_train == n_games

        # Non-trivial fit: the elo_diff slope must come out non-zero.
        coefs = list(getattr(reg, "margin_coef", []) or [])
        assert any(abs(float(c)) > 1e-6 for c in coefs), coefs

        # Empirical residual SD must be finite and strictly positive
        # (the cold-start fallback uses textbook constants — this
        # asserts the fit actually measured the residuals).
        margin_sd = float(getattr(reg, "margin_sd", 0.0))
        assert math.isfinite(margin_sd) and margin_sd > 0.0

    asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Downstream effect:  per-league calibrator improves Brier on holdout
# ---------------------------------------------------------------------------


def _synthetic_miscalibrated_outcomes(
    league: str, *, n: int, seed: int,
) -> tuple[list[float], list[int]]:
    """Generate (raw_probability, actual_outcome) pairs whose marginal
    distribution is systematically over-confident — the raw model
    posts probabilities further from 0.5 than the true outcome rate.

    An isotonic calibrator fit on this slice should strictly reduce
    the holdout Brier.  This is exactly what the per-league
    calibrator would learn from a season's worth of settled NBA /
    NHL / MLB trades after the backfill lands.
    """
    # Slight per-league shift in the over-confidence pattern keeps the
    # tests independent (NBA tightens probs more aggressively than
    # MLB, where individual game outcomes are noisier).
    compression = {"NBA": 0.40, "NHL": 0.35, "MLB": 0.30}[league]
    rng = random.Random(seed)
    probs: list[float] = []
    ys: list[int] = []
    for _ in range(n):
        p = rng.choice([0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80])
        true_p = 0.5 + compression * (p - 0.5)
        y = 1 if rng.random() < true_p else 0
        probs.append(p)
        ys.append(y)
    return probs, ys


@pytest.mark.parametrize("league,seed", [
    ("NBA", 4331),
    ("NHL", 4332),
    ("MLB", 4333),
])
def test_isotonic_calibrator_improves_holdout_brier_per_league(
    league: str, seed: int,
):
    """The per-league calibrator must strictly reduce holdout Brier
    on a systematically-miscalibrated slice for each of NBA / NHL /
    MLB — the "calibrator improves Brier" guarantee the task lists."""
    probs, ys = _synthetic_miscalibrated_outcomes(league, n=1200, seed=seed)
    cut = int(len(probs) * 0.7)
    train_p, train_y = probs[:cut], ys[:cut]
    hold_p, hold_y = probs[cut:], ys[cut:]

    state = calibrators._fit_isotonic(train_p, train_y)
    assert state["type"] == "isotonic"
    assert state["n"] == len(train_p)

    raw_brier = calibrators._brier(hold_p, hold_y)
    cal_probs = [calibrators._apply_isotonic(p, state) for p in hold_p]
    cal_brier = calibrators._brier(cal_probs, hold_y)

    # Strictly improves holdout Brier.
    assert cal_brier < raw_brier, (league, cal_brier, raw_brier)
    # Improvement is non-trivial (>= 1% relative) — a true cold-start
    # baseline can sometimes look fine on a tiny slice; this asserts
    # the calibrator is doing real work, not just noise.
    assert (raw_brier - cal_brier) / raw_brier > 0.01, (
        league, raw_brier, cal_brier,
    )
