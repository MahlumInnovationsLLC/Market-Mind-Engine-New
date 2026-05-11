# Cross-Vertical Upgrade Audit

**Status:** READ-ONLY. No code changes were made for this audit.
**Scope:** Every shipping vertical in `app/verticals/builtins.py` plus the
WNBA player-props prototype merged in task #823.
**Author:** main agent, task #824.
**Date:** 2026-05-11.

---

## 0. How to read this document

This is a single prioritized backlog. The operator is expected to read the
top-20 table, pick **one** entry to fund, and then open a separate task to
implement it. **No follow-up tasks are proposed inside this document** —
that is enforced by the task spec for #824.

Every recommendation is rated on five dimensions on a 1–5 scale (5 is
highest):

| Dim. | Meaning |
|------|---------|
| `prop_depth`     | Player / participant prop coverage and richness |
| `live_depth`     | In-game / live-line modelling and execution |
| `cal_freshness`  | Frequency and quality of calibrator updates |
| `feed_redundancy`| Number of independent upstream sources |
| `vertical_edge`  | Vertical-specific edge sources beyond price + Elo |

Each upgrade carries a **score**:

```
score = (estimated_edge_bps × confidence) / effort_person_days
```

`confidence` is in `[0, 1]`. `edge_bps` is the operator's best honest
guess at incremental ROI (bps of bankroll per cycle on the *targeted*
universe — not portfolio-wide). Every recommendation has a "**why this
might NOT work**" counter so the operator does not over-anchor on the
optimistic edge estimate.

Citations are file paths + line ranges, not summaries. If a claim has no
citation, treat it as opinion and discount accordingly.

---

## 1. Top-20 Aggregated Backlog

Ordered by `score` (desc). Ties broken by lower effort first.

| # | Vertical | Upgrade | Edge (bps) | Conf. | Effort (PD) | **Score** |
|---|----------|---------|-----------:|------:|------------:|----------:|
| 1 | NBA            | Ship player-props vertical (mirror WNBA scaffold) | 80 | 0.55 | 8  | **5.5** |
| 2 | NFL            | Ship player-props vertical (passing/rushing/receiving yds, anytime TD) | 90 | 0.50 | 12 | **3.75** |
| 3 | crypto         | Replace process-cached baselines with calibrated OHLCV champion+challenger | 60 | 0.65 | 10 | **3.9** |
| 4 | commodities    | Add EIA inventory + COT positioning feed | 70 | 0.55 | 12 | **3.2** |
| 5 | weather        | Add ECMWF (or GFS) ensemble blend on top of HRRR | 50 | 0.55 | 10 | **2.75** |
| 6 | MLB            | Ship batter HR / pitcher Ks player-props vertical | 70 | 0.45 | 12 | **2.6** |
| 7 | elections      | Replace Cook-rating regex with structured pollster+state-effects model | 60 | 0.45 | 12 | **2.25** |
| 8 | tech_ai        | Add SEC EDGAR 8-K + earnings-transcript evidence ingestion | 40 | 0.55 | 10 | **2.2** |
| 9 | UFC            | Add round-by-round simulator + TheOddsAPI prop markets (`fight_to_go_distance`, `method_of_victory`) | 70 | 0.40 | 13 | **2.15** |
| 10 | (all sports)  | Wire `live_calibrator.py` into orchestrator dispatch (currently dormant) | 30 | 0.65 | 10 | **1.95** |
| 11 | EPL/UCL       | Add xG (Understat / FBref) feed + Poisson goal-scorer model | 50 | 0.45 | 12 | **1.9** |
| 12 | events_econ    | Add Fed nowcast (Atlanta GDPNow / NY Nowcast) feed | 35 | 0.55 | 11 | **1.75** |
| 13 | NHL            | Add MoneyPuck / Natural Stat Trick xG + goalie-confirmed line | 45 | 0.45 | 12 | **1.7** |
| 14 | F1             | Add weather-aware practice-pace + tyre-strategy simulator | 50 | 0.40 | 12 | **1.65** |
| 15 | PGA            | Add Strokes-Gained & course-fit prior; ship top-10 / top-20 props | 55 | 0.40 | 14 | **1.55** |
| 16 | ATP/WTA        | Add Jeff Sackmann elo + surface/serve-speed adjustment | 50 | 0.40 | 13 | **1.55** |
| 17 | NCAAB         | Add KenPom adj-eff feed + free pace prior | 45 | 0.45 | 14 | **1.45** |
| 18 | NCAAF         | Add SP+ / FPI feed + transfer-portal injury layer | 50 | 0.40 | 14 | **1.4** |
| 19 | entertainment | Add Box Office Mojo + RT critics-vs-audience drift signal | 30 | 0.55 | 14 | **1.2** |
| 20 | (all)         | Add `brier_after`-based per-vertical calibrator auto-promote on the daily report | 25 | 0.60 | 10 | **1.5** |

(#20 ranks above #19 by score; ordering above is the operator's
suggested execution order, with prop builds front-loaded because they
multiply downstream Kelly-sized bet flow and they unlock Tier-2 NBA/NFL
coverage that the current `h2h,spreads,totals`-only sweep ignores
entirely — see `app/feeds/odds_api.py:43`.)

---

## 2. Per-Vertical Reports

### 2.1 NFL (`league=NFL`)

**Executive summary.** NFL is the largest dollar-flow vertical on Kalshi
and the operator already polls Sportradar injuries and NFL snap counts
(`app/feeds/nfl_snap_counts.py`, `app/feeds/sportradar.py:1`). However,
**no player-prop predictor exists for NFL** — the entire prop universe
is invisible to the cycle. The TheOddsAPI sweep is hard-coded to
`h2h,spreads,totals` (`app/feeds/odds_api.py:43`), so the prop event
endpoint isn't even called.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | 1 | No player-props feed; hook is missing. |
| live_depth      | 2 | Sportradar live state present, but no live calibrator. |
| cal_freshness   | 3 | Daily honesty report covers calibrator drift (`app/notifications/daily_honesty_report.py:411`). |
| feed_redundancy | 4 | TheOddsAPI + Sportradar + ESPN scoreboard. |
| vertical_edge   | 3 | Snap counts ingested but only as a feature, no rusher/receiver share model. |

**Next best build:** Mirror the WNBA player-props scaffold
(`app/feeds/wnba_player_props.py`, `app/wnba_player_props/`) for NFL —
add `player_pass_yds, player_rush_yds, player_reception_yds,
player_anytime_td, player_pass_tds` to a NFL-specific event-odds poller,
register a `predict_nfl_player_props` hook in `cycle.py:1175-1200`, and
ship raw-mode-only at Kelly 0.5 for the first month.

**Why this might NOT work.**
- TheOddsAPI per-event prop endpoints are *expensive* (one call per event
  per market refresh) and the WNBA poller already burns 5-min cadence
  (`app/feeds/wnba_player_props.py:38`); NFL on Sundays would be ~14 ×
  more expensive on game day.
- NFL props are heavily juiced (>10% vig); a naive ensemble has to clear
  ~55% to break even.
- Snap-count predictions require *projected* snaps, which Sportradar
  doesn't ship. Without a depth-chart model the rusher/receiver share
  prior is naive.

---

### 2.2 NBA (`league=NBA`)

**Executive summary.** NBA is the easiest player-props expansion: the
WNBA scaffold (#823) ports almost 1:1 — same TheOddsAPI markets, same
`player_props_quotes` schema, same baseline injuries module
(`app/player_props/injuries.py`). NBA also has a much deeper public
betting + injury report cadence than WNBA, so calibration converges
faster.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | 1 | No NBA-specific predictor; only the WNBA prototype ships. |
| live_depth      | 2 | Sportradar live state present; no live calibrator. |
| cal_freshness   | 3 | Standard daily Brier; no in-game retune. |
| feed_redundancy | 4 | TheOddsAPI + Sportradar + ESPN. |
| vertical_edge   | 2 | No pace / matchup-defence priors, no usage-rate model. |

**Next best build:** Lift `app/wnba_player_props/` → `app/nba_player_props/`,
swap the `_SPORT_KEY` to `basketball_nba` and `_LEAGUE` to `NBA`
(`app/feeds/wnba_player_props.py:42-43`), wire a new vertical hook
identical to `wnba_player_props` in `cycle.py:1191`, and ship
disabled-by-default with raw-mode override required.

**Why this might NOT work.**
- NBA player-props markets are far more efficient than WNBA — the books
  spend more on them, so the residual edge is smaller.
- Injury announcements are unpredictable and arrive within 90 min of
  tip-off; the 5-min poller cadence is borderline.
- Pace and matchup adjustments matter much more in NBA than WNBA, so a
  pure "lift-and-shift" of the WNBA model under-fits.

---

### 2.3 MLB (`league=MLB`)

**Executive summary.** MLB has the *richest* prop universe of any
vertical (batter HR, pitcher Ks, anytime hit, NRFI/YRFI, etc.), but the
codebase ships only the standard market sweep. Park factors, weather
(already polled in `app/feeds/weather.py`), and pitcher-handedness are
all powerful priors that the current pipeline ignores.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | 1 | No MLB props predictor. |
| live_depth      | 2 | No inning-by-inning live model. |
| cal_freshness   | 3 | Standard daily. |
| feed_redundancy | 3 | TheOddsAPI + Sportradar + ESPN. |
| vertical_edge   | 2 | Weather feed is in place but *not joined* to MLB park lat/lon. |

**Next best build:** Ship a `mlb_player_props` vertical focused on
`batter_home_runs` + `pitcher_strikeouts`, joining `weather_observations`
on each park's nearest city code (extension to
`app/feeds/weather.py:85-93`).

**Why this might NOT work.**
- MLB props clear settlement well after each event; back-test cycle is
  slow and Brier convergence takes a full season.
- The seven Kalshi-canonical cities in `DEFAULT_CITIES` don't cover all
  30 MLB parks — operator would need to extend the city list rather
  than join.
- Pitcher Ks depend strongly on opposing-team K%, which requires a
  team-strength feature the codebase doesn't ingest.

---

### 2.4 NHL (`league=NHL`)

**Executive summary.** NHL is currently in the standard sports sweep
(TheOddsAPI `h2h,spreads,totals`) with no goalie-confirmation gate. The
single biggest NHL-specific edge — knowing whether the listed starting
goalie is confirmed — is missing.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | 1 | No props (`player_shots_on_goal`, `player_goals`). |
| live_depth      | 1 | No period-by-period live model. |
| cal_freshness   | 3 | Standard daily. |
| feed_redundancy | 3 | TheOddsAPI + Sportradar; no MoneyPuck. |
| vertical_edge   | 1 | No xG, no goalie confirmation, no PP/PK splits. |

**Next best build:** Add a MoneyPuck or Natural Stat Trick xG poller
(both publish free CSVs) plus a `goalie_confirmed` boolean from the
Sportradar pre-game state we already pull (`app/feeds/sportradar.py:969+`).

**Why this might NOT work.**
- MoneyPuck CSVs update once per day; intra-day goalie scratches still
  blind us.
- xG signal half-life is short; without a usage-weighted prior the
  feature is noisy.
- NHL has chronic low-volume on Kalshi — even a real edge may not yield
  fillable size.

---

### 2.5 NCAAB / NCAAF

**Executive summary.** Both NCAA verticals are in the standard sweep
with **no college-specific data source**. KenPom (basketball) and SP+/FPI
(football) are the dominant public models the books themselves price
against; without ingesting at least one, the cycle is blind.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | 1 | None. |
| live_depth      | 2 | Sportradar live state. |
| cal_freshness   | 3 | Standard daily. |
| feed_redundancy | 3 | TheOddsAPI + Sportradar + ESPN. |
| vertical_edge   | 1 | No KenPom (CBB), no SP+ / FPI (CFB), no transfer-portal layer. |

**Next best build (CBB):** KenPom adj-eff CSV poller + pace prior.
**Next best build (CFB):** SP+ JSON poller + week-1-recency damper.

**Why this might NOT work.**
- KenPom is paywalled; only the daily snapshot is on the public site
  and scraping risks the source. SP+ is free-ish but updates weekly.
- Both college models are *the* public anchor — beating them by enough
  to clear vig is a high bar.
- College sample sizes are small; per-team Brier converges only over
  multi-year windows.

---

### 2.6 Soccer (EPL, LaLiga, SerieA, Bundesliga, Ligue1, MLS, UCL)

**Executive summary.** All six top-flight leagues + UCL share the same
ESPN soccer poller (`app/feeds/espn_soccer.py`) and the standard
TheOddsAPI sweep. **No xG ingestion exists**, despite Understat and
FBref publishing free per-match xG that any halfway-modern soccer model
treats as table stakes.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | 1 | No goalscorer or shots-on-target props. |
| live_depth      | 2 | ESPN scoreboard only. |
| cal_freshness   | 3 | Daily; soccer-specific drift report exists at `app/notifications/soccer_settlement_drift.py`. |
| feed_redundancy | 3 | TheOddsAPI + ESPN + Sportradar (paid). |
| vertical_edge   | 1 | No xG, no possession, no lineup-confirmation gate. |

**Next best build:** Understat or FBref xG poller (rolling 10-match
team xG) + a Poisson model for `player_to_score_anytime`. UCL inherits
this trivially since it's the same player pool.

**Why this might NOT work.**
- Understat/FBref scrapers break on each site redesign; we'd own the
  fragility.
- Lineup confirmation arrives ~60 min before kickoff; without a
  late-news poller the Poisson goalscorer model fires on the wrong
  starting XI.
- MLS, Ligue1 and lower-half SerieA have thin Kalshi books — the edge
  may not size up.

---

### 2.7 UFC

**Executive summary.** UFC ships in the registry but the only signal
visible is moneyline / fight-result. No round-by-round simulator and no
prop markets (`fight_to_go_distance`, `method_of_victory`) — both
markets exist on TheOddsAPI but the sweep ignores them.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | 1 | Method-of-victory and round-betting absent. |
| live_depth      | 1 | No round-state ingestion. |
| cal_freshness   | 3 | Standard daily. |
| feed_redundancy | 2 | TheOddsAPI only; no UFCStats scrape. |
| vertical_edge   | 1 | No striking/grappling stats, no reach/age delta features. |

**Next best build:** UFCStats per-fighter scrape (free, structured) +
finish-rate prior + add the prop markets to a UFC-specific event sweep.

**Why this might NOT work.**
- 12-fight cards yield ~12 settlements per event — Brier converges very
  slowly; 50+ events for a single statistically-significant signal.
- Fight outcomes are heavy-tailed (one-punch KOs); the Poisson finish
  model over-states our edge.

---

### 2.8 ATP / WTA

**Executive summary.** Tennis verticals share the standard sweep with
no surface-specific or serve-speed feature. Jeff Sackmann's GitHub elo
+ match-by-match data is the de-facto public anchor (free, MIT-licensed)
and is not ingested.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | 1 | No `total_aces` / set-betting model. |
| live_depth      | 1 | No live-set state. |
| cal_freshness   | 3 | Standard daily. |
| feed_redundancy | 2 | TheOddsAPI only. |
| vertical_edge   | 1 | No surface elo, no fatigue (back-to-back days), no H2H. |

**Next best build:** Sackmann elo CSV poller (daily) + surface
adjustment + a back-to-back fatigue penalty.

**Why this might NOT work.**
- Tennis has very stable closing-line vig (~5%) but very small
  Kalshi-side liquidity outside of slams.
- Sackmann data updates 2–3 days post-match; live elo requires our own
  in-tournament update.

---

### 2.9 F1

**Executive summary.** F1 is in the registry but has no practice-pace
ingestion — winning F1 markets without practice times is essentially
guessing. Weather already polled, but not joined to the race-circuit
city/lat/lon.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | 1 | No podium/H2H/qualifying-pole props. |
| live_depth      | 1 | No lap-time stream. |
| cal_freshness   | 3 | Standard daily. |
| feed_redundancy | 1 | TheOddsAPI only. |
| vertical_edge   | 1 | No practice-pace, no tyre strategy, no DRS-train model. |

**Next best build:** Ergast / FastF1 practice-session timing + a
qualifying-to-race delta prior. Join the race-week city to
`weather_observations` for wet-race regimes.

**Why this might NOT work.**
- Only ~24 races/year — Brier convergence takes multiple seasons.
- Tyre strategy is opaque; teams reveal it ~30 min before lights-out.
- Kalshi F1 liquidity is shallow.

---

### 2.10 PGA

**Executive summary.** Currently no Strokes-Gained or course-fit
ingestion. Top-10 / top-20 / make-cut markets are TheOddsAPI's
`outrights` family — not in the standard sweep.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | 1 | No `top_10`, `top_20`, `make_cut`. |
| live_depth      | 2 | DataGolf shotlink-style live exists publicly; not ingested. |
| cal_freshness   | 3 | Standard daily. |
| feed_redundancy | 1 | TheOddsAPI only. |
| vertical_edge   | 1 | No Strokes-Gained, no course-fit, no putting-distance feature. |

**Next best build:** DataGolf or PGA Tour stat ingestion (SG: total +
putt) + a course-fit prior, plus a top-N outright model (Sasha Lee
Bayesian-update style).

**Why this might NOT work.**
- DataGolf premium is paywalled; the free tier is delayed and
  rate-limited.
- 156-player fields with Plackett-Luce sampling is computationally
  heavy and edge is diluted across so many tickets.

---

### 2.11 Crypto (`league=CRYPTO`)

**Executive summary.** Crypto has the most mature predictor in the
codebase: timeframe-specific LGB+XGB stack, BTC-specific GRU directional
blend, calibrator champion-challenger A/B already wired
(`app/crypto/predictor.py:198`, `task #755`). The biggest remaining gap
is the **baseline class fall-through**: `_resolve_model` falls back to
`BaselineLGBXGBStack` whenever the catalog doesn't carry the canonical
artifact (`app/crypto/predictor.py:73-87`). In practice on a fresh
checkout, every cycle uses untrained baselines.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | n/a | Not applicable. |
| live_depth      | 4 | 5m / 15m timeframes already feed live. |
| cal_freshness   | 5 | Champion-challenger applied (`app/crypto/predictor.py:194-201`). |
| feed_redundancy | 3 | OHLCV from Coinbase + Kraken + Kalshi crypto markets feed. |
| vertical_edge   | 3 | Directional GRU only used for BTC short-horizon. |

**Next best build:** Train + ship the canonical LGB+XGB stack artifacts
referenced in `CANONICAL_PATHS` so the baseline fall-through path is
*never* used in production. Add a hard health-check fail when
`models_loader.get_state().predictors` is missing any of
`crypto/lgbxgb/{5m,15m,1h}`.

**Why this might NOT work.**
- The training pipeline (`app/crypto/training.py` and friends) requires
  a sustained OHLCV history that the on-disk store may have gaps in
  (operator should validate `crypto_ohlcv_retention.py`'s actual
  retention vs. training window).
- The 65/35 stack/GRU blend is hand-tuned (`app/crypto/predictor.py:130`);
  a learned blender on hold-out data may move the needle more.
- Crypto vol regime shifts faster than the calibrator's daily window;
  Brier may spike during regime change before the calibrator catches up.

---

### 2.12 Commodities (`league=COMMODITIES`)

**Executive summary.** Predictor mirrors crypto with a single LGB+XGB
stack per timeframe (`app/commodities/predictor.py:21,104-107`). No
fundamental-data feed (EIA inventories, USDA WASDE, COT positioning) —
the model is **purely technical** despite commodities being the most
fundamentals-driven of all the asset classes.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | n/a | Not applicable. |
| live_depth      | 3 | Timeframe stack runs live. |
| cal_freshness   | 4 | `calibrators.apply` per league (`app/commodities/predictor.py:115`). |
| feed_redundancy | 2 | OHLCV only. |
| vertical_edge   | 1 | No EIA, no COT, no WASDE, no OPEC calendar. |

**Next best build:** Wire the EIA Open Data API (free, key-only) — at
minimum WPSR weekly petroleum status + STEO short-term outlook — into a
new `commodities_fundamentals` cache table and add as features to the
LGBXGB stack. Add COT positioning (CFTC ships free Friday CSVs) for
managed-money skew as a separate prior.

**Why this might NOT work.**
- EIA WPSR is a Wednesday 10:30 ET print; intraweek the feature is
  stale and adds noise.
- COT data is delayed (Tuesday-snapshot, Friday-publish); positioning
  edge has been arbitraged away in deep markets like CL.
- WASDE / OPEC events cause discontinuities the stack can't model;
  upgrading the model without a regime-aware overlay just spreads the
  loss more evenly.

---

### 2.13 Elections (`league=ELECTIONS`)

**Executive summary.** Poll-of-polls model (`app/elections/predictor.py`)
plus four pollers (538 RSS, Cook RSS, RCP RSS, Polymarket xref —
`app/feeds/election_polls.py:54-61,303`). The Cook → support-% mapping
is a **regex** (`app/feeds/election_polls.py:267-282`) and the scope
router is a **keyword regex list** (`app/feeds/election_polls.py:176-184`)
— both are brittle and silently miss races they don't recognise.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | n/a | Not applicable. |
| live_depth      | 4 | Polymarket xref polls every 5 min (`app/feeds/election_polls.py:67`). |
| cal_freshness   | 3 | Per-league calibrator (`app/elections/predictor.py:40`). |
| feed_redundancy | 4 | 538 + Cook + RCP + Polymarket. |
| vertical_edge   | 2 | No structured pollster ratings, no state effects, no fundraising (placeholder at `app/feeds/election_polls.py:386-396`). |

**Next best build:** Replace the Cook regex + keyword scope router with a
structured race-ID resolver (manually-curated mapping table), then layer
a state-effects prior (state PVI). The poll-of-polls model already
takes a `state_bias_pp` (`app/elections/predictor.py:61`); it's just
fed `0` today.

**Why this might NOT work.**
- 538 stopped publishing its model post-acquisition; the RSS feed
  quality is degraded relative to the 2020 baseline.
- Polymarket cross-reference is a self-fulfilling prophecy if every arb
  trader reads the same number — using it as a *poll* dilutes the
  edge.
- OpenSecrets fundraising pipeline is documented as a placeholder
  (`app/feeds/election_polls.py:386-396`) — landing it requires manual
  ops work for the API key.

---

### 2.14 events_econ (`league=EVENTSECON`)

**Executive summary.** Calendar resolver
(`app/events_econ/calendar.py`) maps tickers to release windows; the
predictor uses a consensus-prior model (`predictor_hook =
events_econ_consensus_prior`). What's missing: a **nowcast** input. The
Atlanta Fed GDPNow and NY Fed Nowcast both publish free numbers that
move CPI/NFP/GDP markets daily — the codebase doesn't ingest either.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | n/a | Not applicable. |
| live_depth      | 2 | No live update between consensus-print and release. |
| cal_freshness   | 3 | Standard. |
| feed_redundancy | 2 | Econ calendar only. |
| vertical_edge   | 2 | No nowcast, no surprise-index, no Fed-funds futures cross-reference. |

**Next best build:** Atlanta Fed GDPNow + NY Fed Nowcast pollers (both
RSS / CSV, no key) + a CME FedWatch-style implied-rate poller for FOMC
markets.

**Why this might NOT work.**
- Nowcasts are themselves consensus — adding them as a feature may just
  duplicate the already-priced expectation.
- Surprise-index on a single release is single-event; Brier convergence
  is glacial.

---

### 2.15 entertainment

**Executive summary.** Hybrid predictor with backfill module shipping
(`app/entertainment/predictor.py`, `backfill.py`, `promotion.py`). The
weakest link is *what counts as evidence* — there is no critic-vs-audience
divergence signal (Rotten Tomatoes / Metacritic) and no box-office
opening-weekend feed.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | n/a | Not applicable. |
| live_depth      | 1 | None. |
| cal_freshness   | 3 | Standard. |
| feed_redundancy | 2 | Internal entertainment_signals feed only. |
| vertical_edge   | 2 | Award-show priors only. |

**Next best build:** Box Office Mojo daily scrape + Rotten Tomatoes
critic/audience deltas, fed as features into the existing hybrid
predictor.

**Why this might NOT work.**
- BOM HTML changes frequently; scraper is fragile.
- Most entertainment markets resolve once (Oscars, Emmys); Brier never
  converges.

---

### 2.16 tech_ai (`league=TECHAI`)

**Executive summary.** Evidence cache + resolver shipping
(`app/tech_ai/evidence.py`). The cache is populated from
`app/feeds/tech_evidence.py` and Unusual Whales options-flow
(`app/feeds/unusual_whales.py`). What's missing: **structured corporate
filings** — SEC EDGAR 8-K and earnings transcripts both have free APIs
and are the highest-signal text source available.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | n/a | Not applicable. |
| live_depth      | 3 | Unusual Whales options flow streams. |
| cal_freshness   | 3 | Standard. |
| feed_redundancy | 3 | tech_evidence + Unusual Whales. |
| vertical_edge   | 3 | Options flow is strong, but no fundamentals. |

**Next best build:** SEC EDGAR 8-K poller (free, no key) keyed by
ticker → `tech_evidence` table with `source='sec_8k'`, plus a
`finnhub_transcripts` poller for earnings calls.

**Why this might NOT work.**
- 8-K text is structured but the *signal extraction* is the hard part —
  needs an LLM or careful regex pipeline.
- Earnings-call transcripts arrive 24h+ after the call; markets have
  already moved.

---

### 2.17 weather (`league=WEATHER`)

**Executive summary.** Most mature weather pipeline of any prediction
shop: HRRR direct ingestion (`app/feeds/weather.py:1-33`), Open-Meteo +
NWS fallbacks, GRU+EMOS predictor (`app/weather/predictor.py`,
`precip_emos.py`). Missing: **ensemble forecasts** — HRRR is single-run
deterministic. ECMWF (paid) or GEFS (free, NOAA) ensemble blends would
give us a *distribution* not a point-estimate.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | n/a | Not applicable. |
| live_depth      | 5 | Per-hour HRRR cycle, every cycle. |
| cal_freshness   | 5 | EMOS retrains regularly. |
| feed_redundancy | 4 | Open-Meteo + NWS + HRRR (NOMADS direct). |
| vertical_edge   | 4 | Already best-in-class for the temp predictor. |

**Next best build:** GEFS (NOAA Global Ensemble Forecast System) ingestion
on the same NOMADS-Range-GET pattern already used for HRRR — gives a
21-member ensemble, perfect for EMOS variance shrinkage.

**Why this might NOT work.**
- GEFS resolution is coarser (0.5°) than HRRR (3km) — for KXTEMP/KXHIGH
  inside a metro the ensemble may add bias as well as variance.
- NOMADS ensemble files are large; bandwidth + parse cost rises sharply.

---

### 2.18 WNBA (`league=WNBA`, prototype from #823)

**Executive summary.** Just-merged player-props prototype. Disabled by
default, Kelly 0.5, raw-mode required. Documented in
`docs/wnba_player_props_runbook.md`. The scaffold is the cleanest
template for NBA / NFL / MLB prop builds.

| Dim. | Score | Why |
|------|------:|-----|
| prop_depth      | 4 | 12 markets registered (`app/feeds/wnba_player_props.py:46-58`). |
| live_depth      | 2 | 5-min cadence; no in-game update. |
| cal_freshness   | 2 | New vertical, no hold-out data yet. |
| feed_redundancy | 1 | TheOddsAPI only — no second source. |
| vertical_edge   | 2 | Injuries module exists at `app/player_props/injuries.py`. |

**Next best build:** Add a second prop source (Pinnacle scrape via the
Sportradar paid feed, or a manual ESPN tile parse) so we can detect
TheOddsAPI line-feed errors. Then graduate from Kelly 0.5 → Kelly 1.0
once 60 days of Brier are in the can.

**Why this might NOT work.**
- WNBA volume on Kalshi is too thin to size into; the model may be
  right but unfillable.
- Single-source TheOddsAPI lines can move the line *because* we trade
  on them (latency arb against ourselves).

---

## 3. Cross-Cutting Observations

These don't belong to a single vertical but show up in multiple matrices
above and are worth funding once for everyone.

### 3.1 `app/models/live_calibrator.py` is dormant

`models/live_calibrator.py` exists, but `rg "live_calibrator" app/orchestrator/`
returns no production dispatch. We have a built calibrator that we never
call. Wiring it into `app/orchestrator/cycle.py` immediately after the
champion calibrator (`app/crypto/predictor.py:198`,
`app/elections/predictor.py:40`, etc.) would be a single-PR upgrade
that touches every vertical.

**Why this might NOT work.** A live calibrator that retunes intra-day
can over-fit to the most-recent batch; the daily Brier check
(`app/notifications/daily_honesty_report.py:411`) would have to be
extended to gate live-calibrator promotion.

### 3.2 TheOddsAPI default markets exclude every prop universe

`DEFAULT_ODDS_MARKETS = "h2h,spreads,totals"` (`app/feeds/odds_api.py:43`).
Every prop build above is blocked on either (a) the operator overriding
this env, which is per-process global, or (b) a per-vertical event-odds
poller mirroring the WNBA scaffold. The latter is the right pattern —
but it needs to be the first thing to land for *each* prop vertical.

### 3.3 The Cook / RCP / 538 RSS routes are silent failures

Any election story whose title doesn't match the keyword regex
(`app/feeds/election_polls.py:176-184`) is silently dropped. There is no
counter for "RSS items received but unrouted". Adding one to the
existing `feeds/status` endpoint would make the failure visible.

### 3.4 Sportradar live-state is ingested but unused

`app/feeds/sportradar.py:969+` populates `sportradar_live_state` but I
found no consumer in `app/orchestrator/` for that table. If we are
paying for the live data we should be using it — start with NFL/NBA
in-game line modelling.

### 3.5 Calibrator drift table covers "drift" but not "edge erosion"

`collect_calibrator_drift_status` (`app/notifications/daily_honesty_report.py:411-470`)
flags Brier-after > 0.20 / 0.25. It doesn't flag *Brier deteriorating
relative to closing line*. CLV (closing-line-value) is the canonical
edge metric and the codebase doesn't compute it for sports verticals.

---

## 4. Estimation Methodology (read this before disagreeing)

- `edge_bps` is the operator's honest first-cycle estimate, **not** a
  back-tested number. None of the numbers above are derived from a real
  back-test — there is no time to run one inside an audit.
- `confidence` reflects how well-understood the build is. Mature
  templates (NBA mirroring WNBA) get 0.55. Speculative
  fundamental-data integrations get 0.40.
- `effort_person_days` assumes one focused engineer with read access to
  this repo, no operator approvals needed mid-PR, and no novel
  infrastructure (databases, queues, secrets). Wall-clock is typically
  2–3× higher.
- `score` is a triage tool, not a forecast. **A score of 5.5 is not
  half-again as good as 3.7** — it just means the operator should look
  at it first.

---

## 5. Next Steps (no tasks created)

The operator should:

1. Read §1, then read the per-vertical executive summary for any row
   they're tempted to fund.
2. Read the **"Why this might NOT work"** counter for that same row.
3. Open a single task — using the standard project-task workflow — for
   the chosen upgrade. Do **not** open more than one at a time.
4. After the chosen upgrade ships and hits the daily Brier report for
   30 days, re-open this audit and demote / reorder remaining rows
   based on what was learned.

The top-10 rows from §1 — repeated here for convenience —
represent the operator's best risk-adjusted next moves:

1. NBA player props (mirror WNBA scaffold)
2. NFL player props
3. Crypto: ship trained champion artifacts, kill baseline fall-through
4. Commodities: EIA + COT fundamentals
5. Weather: GEFS ensemble blend
6. MLB player props
7. Elections: structured race resolver + state effects
8. tech_ai: SEC 8-K + earnings transcripts
9. UFC: round simulator + prop markets
10. (cross-cutting) Wire `live_calibrator.py` into orchestrator dispatch

— end of audit —
