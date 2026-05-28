"""Fit market-implied PRIOR calibrators from public closing-line data.

For leagues where we don't yet have enough of our own settled trades to
fit a real calibrator (``MIN_REFIT_SAMPLES=10``), this script pulls
publicly-available closing odds from free sources, devigs them into
implied probabilities, and fits a single Platt curve per league
against the actual outcomes.

These are PRIORS, not learned model fits. They encode known
market-level miscalibration (favorite-longshot bias, draw bias, etc.)
which our model probably also exhibits to some degree, but they are
explicitly tagged ``kind='market_prior'`` and ``valid_until_real_trades=true``
so the nightly ``calibrators.refit_all()`` will overwrite them with a
real fit the moment any league crosses ``MIN_REFIT_SAMPLES`` of our
own settled trades.

Sources (all free, no API key):

  * football-data.co.uk — EPL/LALIGA/SERIEA/BUNDES/LIGUE1 (Pinnacle
    closing 1X2 odds, last 4 seasons).
  * football-data.co.uk extras — MLS via the consolidated USA.csv.
  * tennis-data.co.uk — ATP/WTA singles (Pinnacle closing W/L odds,
    last 4 seasons).
  * nflverse/nfldata on GitHub — NFL regular-season moneylines and
    results, all available seasons.

Leagues with no honest free public source — UCL, NCAAB, UFC, F1, PGA,
COMMODITIES, TECH_AI, ELECTIONS, EVENTS_ECON, ENTERTAINMENT — keep
their identity calibrator from ``seed_identity_calibrators.py``.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import calibrators, db  # noqa: E402


SOCCER_SEASONS = ["2122", "2223", "2324", "2425"]
TENNIS_YEARS = ["2022", "2023", "2024", "2025"]
NFL_SEASONS = (2022, 2023, 2024, 2025)  # match the soccer/tennis window

FOOTBALL_DATA_LEAGUES: dict[str, str] = {
    "EPL": "E0",
    "LALIGA": "SP1",
    "SERIEA": "I1",
    "BUNDES": "D1",
    "LIGUE1": "F1",
}

# football-data.co.uk consolidated "extras" — MLS lives here.
EXTRAS_USA_URL = "https://www.football-data.co.uk/new/USA.csv"
NFLVERSE_URL = (
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
)

# tennis-data.co.uk does not offer HTTPS (verified empirically). Since we
# cannot get a TLS guarantee, we apply explicit content-integrity checks on
# every download (MIME type, plausible byte range, expected odds columns)
# and abort the per-year load if any check fails so a tampered or stale
# upstream cannot poison a calibrator snapshot.
TENNIS_BASE_HTTP = "http://www.tennis-data.co.uk"
TENNIS_MIN_BYTES = 50_000
TENNIS_MAX_BYTES = 5_000_000
TENNIS_REQUIRED_COLS = {"Winner", "Loser"}
TENNIS_REQUIRED_ODDS_COLS = (("PSW", "PSL"), ("B365W", "B365L"), ("AvgW", "AvgL"))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _devig_pair(po: float, qo: float) -> tuple[float, float] | None:
    """Devig a 2-way market into (p, q) summing to 1."""
    if not (po and qo) or po <= 1.0 or qo <= 1.0:
        return None
    p_imp, q_imp = 1.0 / po, 1.0 / qo
    s = p_imp + q_imp
    if s <= 0:
        return None
    return p_imp / s, q_imp / s


def _devig_triple(
    ho: float, do: float, ao: float
) -> tuple[float, float, float] | None:
    if not (ho and do and ao):
        return None
    if min(ho, do, ao) <= 1.0:
        return None
    h, d, a = 1.0 / ho, 1.0 / do, 1.0 / ao
    s = h + d + a
    if s <= 0:
        return None
    return h / s, d / s, a / s


def _moneyline_to_prob(ml: float) -> float | None:
    """American odds → implied probability (with vig)."""
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    if ml == 0 or pd.isna(ml):
        return None
    if ml < 0:
        return -ml / (-ml + 100.0)
    return 100.0 / (ml + 100.0)


def _fit_platt(probs: list[float], ys: list[int]) -> dict[str, Any]:
    X = np.array(probs, dtype=float).reshape(-1, 1)
    y = np.array(ys, dtype=int)
    # If all outcomes identical, skip — degenerate fit.
    if y.min() == y.max():
        return {}
    lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=2000)
    lr.fit(X, y)
    a = float(lr.coef_[0][0])
    b = float(lr.intercept_[0])
    return {"type": "platt", "a": a, "b": b, "n": len(probs)}


def _apply_platt(p: float, a: float, b: float) -> float:
    import math

    z = a * p + b
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _brier(probs: list[float], ys: list[int]) -> float:
    if not probs:
        return 0.0
    return sum((p - y) ** 2 for p, y in zip(probs, ys)) / len(probs)


# ---------------------------------------------------------------------------
# fetchers — return list of (devig'd implied prob, outcome) pairs
# ---------------------------------------------------------------------------


async def fetch_soccer(
    client: httpx.AsyncClient, league: str, code: str
) -> tuple[list[tuple[float, int]], list[str], list[str]]:
    pairs: list[tuple[float, int]] = []
    seasons_loaded: list[str] = []
    sources: list[str] = []
    for season in SOCCER_SEASONS:
        url = f"https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
        try:
            r = await client.get(url, timeout=30.0)
            r.raise_for_status()
        except Exception as e:
            print(f"  [{league}] season {season}: skip ({e})")
            continue
        try:
            df = pd.read_csv(BytesIO(r.content), encoding="latin-1", on_bad_lines="skip")
        except Exception as e:
            print(f"  [{league}] season {season}: parse fail ({e})")
            continue
        # CLOSING ODDS ONLY — opening lines (PSH/B365H/AvgH) are different
        # information and would mix priors of different quality. football-data.co.uk
        # marks closing columns with a trailing 'C' (PSCH/PSCD/PSCA, B365CH/D/A,
        # AvgCH/D/A). If none are present we skip rather than silently fall back.
        triples_cols: tuple[str, str, str] | None = None
        for cand in [("PSCH", "PSCD", "PSCA"),
                     ("B365CH", "B365CD", "B365CA"),
                     ("AvgCH", "AvgCD", "AvgCA")]:
            if all(c in df.columns for c in cand):
                triples_cols = cand
                break
        if triples_cols is None or "FTR" not in df.columns:
            print(f"  [{league}] season {season}: no closing-odds/result cols — skip")
            continue
        df2 = df.dropna(subset=list(triples_cols) + ["FTR"]).copy()
        added = 0
        for _, row in df2.iterrows():
            try:
                t = _devig_triple(
                    float(row[triples_cols[0]]),
                    float(row[triples_cols[1]]),
                    float(row[triples_cols[2]]),
                )
            except Exception:
                continue
            if t is None:
                continue
            ph_d, pd_d, pa_d = t
            ftr = str(row["FTR"]).strip().upper()
            if ftr not in ("H", "D", "A"):
                continue
            pairs.append((ph_d, 1 if ftr == "H" else 0))
            pairs.append((pd_d, 1 if ftr == "D" else 0))
            pairs.append((pa_d, 1 if ftr == "A" else 0))
            added += 3
        if added:
            seasons_loaded.append(season)
            sources.append(f"{url} (cols={triples_cols[0]}/D/A)")
            print(f"  [{league}] season {season}: +{added // 3} matches via {triples_cols[0][:-1]}")
    return pairs, seasons_loaded, sources


async def fetch_mls(
    client: httpx.AsyncClient,
) -> tuple[list[tuple[float, int]], list[str], list[str]]:
    """MLS lives in football-data.co.uk's consolidated USA.csv."""
    pairs: list[tuple[float, int]] = []
    try:
        r = await client.get(EXTRAS_USA_URL, timeout=60.0)
        r.raise_for_status()
    except Exception as e:
        print(f"  [MLS] fetch fail: {e}")
        return pairs, [], []
    try:
        df = pd.read_csv(BytesIO(r.content), encoding="latin-1", on_bad_lines="skip")
    except Exception as e:
        print(f"  [MLS] parse fail: {e}")
        return pairs, [], []
    # CLOSING ODDS ONLY (consistent with the per-season soccer fetcher).
    triples_cols: tuple[str, str, str] | None = None
    for cand in [("PSCH", "PSCD", "PSCA"),
                 ("B365CH", "B365CD", "B365CA"),
                 ("AvgCH", "AvgCD", "AvgCA")]:
        if all(c in df.columns for c in cand):
            triples_cols = cand
            break
    if triples_cols is None or "Res" not in df.columns:
        print(f"  [MLS] no closing-odds/result cols — skip")
        return pairs, [], []
    # Restrict to last 4 seasons; col is "Season" formatted like 2024
    if "Season" in df.columns:
        try:
            df["Season"] = df["Season"].astype(str)
            keep_seasons = sorted(df["Season"].dropna().unique())[-4:]
            df = df[df["Season"].isin(keep_seasons)]
        except Exception:
            keep_seasons = []
    else:
        keep_seasons = []
    df2 = df.dropna(subset=list(triples_cols) + ["Res"]).copy()
    added = 0
    for _, row in df2.iterrows():
        try:
            t = _devig_triple(
                float(row[triples_cols[0]]),
                float(row[triples_cols[1]]),
                float(row[triples_cols[2]]),
            )
        except Exception:
            continue
        if t is None:
            continue
        ph_d, pd_d, pa_d = t
        res = str(row["Res"]).strip().upper()
        if res not in ("H", "D", "A"):
            continue
        pairs.append((ph_d, 1 if res == "H" else 0))
        pairs.append((pd_d, 1 if res == "D" else 0))
        pairs.append((pa_d, 1 if res == "A" else 0))
        added += 3
    print(f"  [MLS] +{added // 3} matches across {len(keep_seasons)} seasons via {triples_cols[0]}")
    return pairs, [str(s) for s in keep_seasons], [f"{EXTRAS_USA_URL} (cols={triples_cols[0]}/D/A)"]


async def fetch_tennis(
    client: httpx.AsyncClient, league: str
) -> tuple[list[tuple[float, int]], list[str], list[str]]:
    """ATP: /{year}/{year}.xlsx; WTA: /{year}w/{year}.xlsx.

    tennis-data.co.uk is plain HTTP (no TLS). To mitigate the absence of
    transport-level integrity, every download must pass these checks before
    we let it influence a calibrator snapshot:
      * Content-Type matches Excel (xlsx) or generic octet-stream
      * Byte size in [TENNIS_MIN_BYTES, TENNIS_MAX_BYTES]
      * Required columns ``Winner`` and ``Loser`` present
      * At least one of (PSW/PSL, B365W/B365L, AvgW/AvgL) odds pairs present
    Any failure aborts that year and prints a loud warning so a tampered or
    corrupted upstream cannot quietly poison a calibrator.
    """
    pairs: list[tuple[float, int]] = []
    years_loaded: list[str] = []
    sources: list[str] = []
    for year in TENNIS_YEARS:
        suffix = "w" if league == "WTA" else ""
        url = f"{TENNIS_BASE_HTTP}/{year}{suffix}/{year}.xlsx"
        try:
            r = await client.get(url, timeout=45.0)
            r.raise_for_status()
        except Exception as e:
            print(f"  [{league}] year {year}: skip ({e})")
            continue
        # ---- HTTP-integrity checks (HTTPS unavailable) ----
        n_bytes = len(r.content)
        ctype = (r.headers.get("content-type") or "").lower()
        ctype_ok = (
            "spreadsheetml" in ctype
            or "excel" in ctype
            or "octet-stream" in ctype
            or ctype.startswith("application/vnd.ms-")
            or ctype == ""  # some servers omit the header
        )
        if not ctype_ok:
            print(f"  [{league}] year {year}: REJECTED bad content-type {ctype!r}")
            continue
        if not (TENNIS_MIN_BYTES <= n_bytes <= TENNIS_MAX_BYTES):
            print(f"  [{league}] year {year}: REJECTED implausible size {n_bytes} bytes")
            continue
        try:
            df = pd.read_excel(BytesIO(r.content))
        except Exception as e:
            print(f"  [{league}] year {year}: parse fail ({e})")
            continue
        missing = TENNIS_REQUIRED_COLS - set(df.columns)
        if missing:
            print(f"  [{league}] year {year}: REJECTED missing required cols {missing}")
            continue
        cols: tuple[str, str] | None = None
        for cand in TENNIS_REQUIRED_ODDS_COLS:
            if all(c in df.columns for c in cand):
                cols = cand
                break
        if cols is None:
            print(f"  [{league}] year {year}: REJECTED no recognized odds cols")
            continue
        df2 = df.dropna(subset=list(cols)).copy()
        added = 0
        for _, row in df2.iterrows():
            try:
                pair = _devig_pair(float(row[cols[0]]), float(row[cols[1]]))
            except Exception:
                continue
            if pair is None:
                continue
            pw_d, pl_d = pair
            # Winner column = the actual winner; outcome=1 for them, 0 for loser.
            pairs.append((pw_d, 1))
            pairs.append((pl_d, 0))
            added += 2
        if added:
            years_loaded.append(year)
            sources.append(f"{url} (cols={cols[0]}/{cols[1]})")
            print(f"  [{league}] year {year}: +{added // 2} matches via {cols[0]}/{cols[1]}")
    return pairs, years_loaded, sources


async def fetch_nfl(
    client: httpx.AsyncClient,
) -> tuple[list[tuple[float, int]], list[str], list[str]]:
    """NFL via nflverse Lee-Sharpe games.csv (REG season, with moneylines)."""
    pairs: list[tuple[float, int]] = []
    try:
        r = await client.get(NFLVERSE_URL, timeout=60.0)
        r.raise_for_status()
    except Exception as e:
        print(f"  [NFL] fetch fail: {e}")
        return pairs, [], []
    df = pd.read_csv(BytesIO(r.content))
    df = df[df["game_type"] == "REG"].copy()
    df = df.dropna(subset=["home_moneyline", "away_moneyline", "result"]).copy()
    # Restrict to a recent window so we don't bake obsolete market behavior
    # into the prior. Mirror the soccer/tennis lookback (~4 seasons).
    df = df[df["season"].isin(NFL_SEASONS)].copy()
    seasons_loaded = sorted(df["season"].dropna().unique().tolist())
    added = 0
    for _, row in df.iterrows():
        ph_imp = _moneyline_to_prob(row["home_moneyline"])
        pa_imp = _moneyline_to_prob(row["away_moneyline"])
        if ph_imp is None or pa_imp is None:
            continue
        s = ph_imp + pa_imp
        if s <= 0:
            continue
        ph_d, pa_d = ph_imp / s, pa_imp / s
        try:
            result = float(row["result"])  # home_score - away_score
        except Exception:
            continue
        if result == 0:  # tie — skip ambiguous outcome
            continue
        home_won = 1 if result > 0 else 0
        pairs.append((ph_d, home_won))
        pairs.append((pa_d, 1 - home_won))
        added += 2
    if not seasons_loaded:
        print(f"  [NFL] no rows survived NFL_SEASONS={NFL_SEASONS} filter — skip")
        return pairs, [], []
    print(f"  [NFL] +{added // 2} games across seasons {min(seasons_loaded)}–{max(seasons_loaded)}")
    return pairs, [str(int(s)) for s in seasons_loaded], [f"{NFLVERSE_URL} (cols=home_moneyline/away_moneyline)"]


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


# Tags written by the prior/seed scripts. Any of these in any classification
# field signals "this is NOT a real learned fit, refresh is safe".
_PRIOR_TAGS: frozenset[str] = frozenset(
    {"market_prior", "identity_seed", "identity", "prior", "seeded"}
)
# Tags that mean "this row was produced by the real refit path". Any of these
# in any classification field — without an overriding prior tag — blocks the
# seed so we cannot overwrite a real calibrator with a market prior.
_REAL_FIT_TAGS: frozenset[str] = frozenset({"fitted", "isotonic", "platt", "logistic"})


async def check_eligibility(league: str) -> tuple[bool, str]:
    """Decide whether the league is eligible for a market-prior seed.

    Re-run-safety: this function MUST refuse to seed any league whose latest
    snapshot was produced by the real ``calibrators._persist_calibrator()``
    refit path, otherwise ``load_all()`` (which always picks the most-recent
    snapshot per ``sub_category``) would silently downgrade a real fit to a
    market prior.

    Decision tree:
      1. If the league has accumulated >= MIN_REFIT_SAMPLES settled trades
         in the last 90 days, skip (the nightly refit will produce a real
         calibrator on its next run).
      2. If no snapshot exists for the league at all, eligible.
      3. Read the latest snapshot's classification across **four** fields
         (``config.kind``, ``drift_status.status``, ``drift_status.kind``,
         ``calibrator_state.type``):
           * If ANY field carries an explicit prior tag (``market_prior``,
             ``identity_seed``, ...) → eligible (refresh allowed).
           * Otherwise, if ANY field carries a real-fit tag (``fitted``,
             ``isotonic``, ``platt``, ...) → ineligible (real fit detected).
           * Otherwise (status is ``rolled_back`` or unknown shape) →
             eligible.

    Reading four fields is intentional defense-in-depth: even if a future
    codepath stores the algorithm in only one of these slots, the gate will
    still recognise it.
    """
    min_required = int(getattr(calibrators, "MIN_REFIT_SAMPLES", 10))
    settled = await db.fetchval(
        """
        SELECT count(*)
          FROM trades t
          JOIN trade_postmortems pm
            ON pm.trade_id = coalesce(t.trade_id, t.id::text)
         WHERE t.settled_at IS NOT NULL
           AND t.settled_at > now() - interval '90 days'
           AND t.model_prob IS NOT NULL
           AND pm.actual_outcome IS NOT NULL
           AND upper(t.league) = $1
        """,
        league,
    )
    settled = int(settled or 0)
    if settled >= min_required:
        return False, f"has {settled} settled trades (>= {min_required}) — let nightly refit handle it"

    latest = await db.fetchrow(
        """
        SELECT config->>'kind'              AS cfg_kind,
               drift_status->>'status'      AS ds_status,
               drift_status->>'kind'        AS ds_kind,
               calibrator_state->>'type'    AS cs_type
          FROM calibration_snapshots
         WHERE vertical = 'sports'
           AND sub_category = $1
         ORDER BY created_at DESC
         LIMIT 1
        """,
        f"per_league:{league}",
    )
    if latest is None:
        return True, f"settled={settled} < {min_required}, no snapshot on file"

    fields: dict[str, str] = {
        "config.kind":            (latest["cfg_kind"]  or "").lower(),
        "drift_status.status":    (latest["ds_status"] or "").lower(),
        "drift_status.kind":      (latest["ds_kind"]   or "").lower(),
        "calibrator_state.type":  (latest["cs_type"]   or "").lower(),
    }
    # Step 3a: any explicit prior tag → safe to refresh.
    for fname, fval in fields.items():
        if fval in _PRIOR_TAGS:
            return True, f"latest is a {fval!r} snapshot ({fname}) — refresh allowed"
    # Step 3b: any real-fit tag → block.
    for fname, fval in fields.items():
        if fval in _REAL_FIT_TAGS:
            return False, (
                f"latest snapshot looks like a real fit "
                f"({fname}={fval!r}; full={fields}) — leaving it alone"
            )
    # Step 3c: unknown classification (e.g. status=rolled_back). Safe to seed.
    return True, f"latest snapshot has unrecognized classification {fields} — safe to seed"


async def persist_market_prior(
    league: str,
    state: dict[str, Any],
    pairs: list[tuple[float, int]],
    brier_before: float,
    brier_after: float,
    data_source: str,
    seasons: list[str],
    source_urls: list[str],
) -> int | None:
    state = dict(state)
    state["fit_at"] = datetime.now(timezone.utc).isoformat()
    state["source"] = "market_prior"
    config = {
        "source": "seed_market_prior_calibrators",
        "kind": "market_prior",
        "data_source": data_source,
        "seasons": seasons,
        "source_urls": source_urls,
        "n": len(pairs),
        "brier_before": float(brier_before),
        "brier_after": float(brier_after),
        "valid_until_real_trades": True,
        "min_required_for_real_fit": int(getattr(calibrators, "MIN_REFIT_SAMPLES", 10)),
        "note": (
            "Fit on devigged public closing-line implied probabilities — "
            "this is a prior on market miscalibration, NOT a fit on our model's "
            "predictions. Will be replaced by refit_all() the moment this league "
            "accumulates >=10 of our own settled trades."
        ),
    }
    drift_status = {
        "status": "market_prior",
        "kind": "platt",
        "window_days": 0,
    }
    snap_id = await db.fetchval(
        """
        INSERT INTO calibration_snapshots
            (vertical, sub_category, ece, mce, brier_score, reliability,
             resolution, sample_count, calibrator_state, drift_status, config)
        VALUES ('sports', $1, $2, $2, $2, NULL, NULL, $3,
                $4::jsonb, $5::jsonb, $6::jsonb)
        RETURNING id
        """,
        f"per_league:{league}",
        float(brier_after),
        int(len(pairs)),
        json.dumps(state),
        json.dumps(drift_status),
        json.dumps(config),
    )
    return int(snap_id) if snap_id is not None else None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


CANDIDATE_LEAGUES: tuple[str, ...] = (
    "EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1",  # soccer (football-data.co.uk)
    "MLS",                                            # MLS (football-data.co.uk extras)
    "ATP", "WTA",                                     # tennis (tennis-data.co.uk)
    "NFL",                                            # NFL (nflverse)
)


async def main() -> int:
    print("Fetching public closing-line data and fitting market-prior Platt curves...")
    print("=" * 70)

    # ---- eligibility gate (idempotent / re-run-safe) ----
    print("\nEligibility check (skip leagues that already have a real fit or enough trades):")
    eligible: list[str] = []
    skipped_eligibility: list[tuple[str, str]] = []
    for league in CANDIDATE_LEAGUES:
        ok, reason = await check_eligibility(league)
        marker = "✓ seed " if ok else "✗ skip "
        print(f"  {marker} {league:<8} {reason}")
        if ok:
            eligible.append(league)
        else:
            skipped_eligibility.append((league, reason))

    if not eligible:
        print("\nNothing to seed — every candidate league already has a real fit or sufficient trades.")
        return 0

    summary: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        headers={"User-Agent": "axiom-predict/calibrator-prior (+https://replit.com)"},
        follow_redirects=True,
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
    ) as client:
        # --- soccer ---
        for league, code in FOOTBALL_DATA_LEAGUES.items():
            if league not in eligible:
                continue
            print(f"\n[{league}] fetching from football-data.co.uk (code={code})...")
            pairs, seasons, urls = await fetch_soccer(client, league, code)
            summary.append({"league": league, "pairs": pairs, "seasons": seasons,
                            "urls": urls, "data_source": "football-data.co.uk"})

        # --- MLS ---
        if "MLS" in eligible:
            print("\n[MLS] fetching from football-data.co.uk extras...")
            pairs, seasons, urls = await fetch_mls(client)
            summary.append({"league": "MLS", "pairs": pairs, "seasons": seasons,
                            "urls": urls, "data_source": "football-data.co.uk/new/USA.csv"})

        # --- tennis ---
        for league in ("ATP", "WTA"):
            if league not in eligible:
                continue
            print(f"\n[{league}] fetching from tennis-data.co.uk...")
            pairs, years, urls = await fetch_tennis(client, league)
            summary.append({"league": league, "pairs": pairs, "seasons": years,
                            "urls": urls, "data_source": "tennis-data.co.uk"})

        # --- NFL ---
        if "NFL" in eligible:
            print("\n[NFL] fetching from nflverse on GitHub...")
            pairs, seasons, urls = await fetch_nfl(client)
            summary.append({"league": "NFL", "pairs": pairs, "seasons": seasons,
                            "urls": urls, "data_source": "nflverse/nfldata"})

    print("\n" + "=" * 70)
    print("Fitting Platt curves and persisting...")
    fitted = 0
    skipped = 0
    for entry in summary:
        league = entry["league"]
        pairs = entry["pairs"]
        if len(pairs) < 50:
            print(f"  [{league}] only {len(pairs)} samples — skip (kept identity)")
            skipped += 1
            continue
        probs = [p for p, _ in pairs]
        ys = [y for _, y in pairs]
        state = _fit_platt(probs, ys)
        if not state:
            print(f"  [{league}] degenerate fit (all outcomes identical) — skip")
            skipped += 1
            continue
        brier_before = _brier(probs, ys)
        calibrated = [_apply_platt(p, state["a"], state["b"]) for p in probs]
        brier_after = _brier(calibrated, ys)
        snap_id = await persist_market_prior(
            league, state, pairs, brier_before, brier_after,
            entry["data_source"], entry["seasons"], entry["urls"],
        )
        delta = brier_before - brier_after
        sign = "↓" if delta > 0 else ("↑" if delta < 0 else "·")
        print(f"  [{league:<7}] n={len(pairs):>5}  Platt(a={state['a']:+.3f}, b={state['b']:+.3f})  "
              f"Brier {brier_before:.4f} → {brier_after:.4f} ({sign}{abs(delta):.4f})  snap_id={snap_id}")
        fitted += 1

    # Refresh in-memory state.
    await calibrators.load_all()
    print("\n" + "=" * 70)
    print(f"DONE: fitted {fitted} market-prior calibrators, skipped {skipped}.")
    print("Identity calibrators are kept for: UCL, NCAAB, UFC, F1, PGA,")
    print("                                   COMMODITIES, TECH_AI, ELECTIONS,")
    print("                                   EVENTS_ECON, ENTERTAINMENT")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
