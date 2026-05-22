# Niche-sport calibration runbook (task #831)

Scaffold for the four individual-sport verticals competitor
edgeterminal.ai prominently features: Tennis (ATP/WTA), F1, PGA, UFC.

## What ships in #831

| Layer | Module | Status |
| --- | --- | --- |
| Settled-outcomes table | migration `0053_niche_sport_outcomes` | schema only — rows backfilled in follow-up |
| Per-sport features | `app/niche_sports/features.py` | null-safe stubs registered per league |
| Calibrator seed | `app/niche_sports/calibrator_scaffold.py` | operator-invoked identity Platt seed |
| Honesty report | `daily_honesty_report.collect_niche_sports_performance` | §10 row per league |
| Dashboard helper | `dashboard.get_niche_sports_tile` | per-vertical pick volume + Brier |

## Per-sport feature set

### Tennis (ATP / WTA) — `build_tennis`
Court surface (hard/clay/grass/carpet, one-hot), surface-conditional
Elo differential over the last 12 months, head-to-head win rate,
recent-tournament-round depth, 3-month retirement rate.

### Formula 1 — `build_f1`
Circuit category (street/permanent/hybrid, one-hot), grid-vs-qualifying
delta (positive = penalty), FP3 long-run pace z-score, wet-race win
rate, high-DRS circuit flag.

### PGA — `build_pga`
Green type (bent/bermuda/poa/other, one-hot), course history over the
last 5 years (made-cut rate, top-10 rate), strokes-gained breakdown
over last 8 events (total / putting / approach), normalised course
length.

### UFC — `build_ufc`
Weight class (one-hot across 9 divisions), reach differential
(inches, clamped ±12), takedown defense rate, takedown attempt rate
per 15 minutes, last-fight outcome, days-since-last-fight normalised
to a 2-year cap.

## Seeding the calibrator snapshot

The `uncalibrated-vertical` gate blocks every candidate routed against
ATP/WTA/F1/PGA/UFC until a per-league snapshot exists in
`calibration_snapshots`.  The seed helper writes a **two-point identity
isotonic** (`x=[0,1] → y=[0,1]`) so the raw probability passes through
unchanged — this satisfies the gate without silently mis-pricing
trades.  (An identity Platt with `a=1, b=0` would actually run
`sigmoid(p)`, which is NOT a pass-through; isotonic is the only seed
shape that's truly identity under the runtime `apply()` path.)

```python
from app.niche_sports.calibrator_scaffold import seed_all
await seed_all()  # one snapshot row per ATP/WTA/F1/PGA/UFC
```

The helper is **not** wired into the boot sequence or the nightly
refit loop.  Seeding is a deliberate operator decision until the
backfill job (follow-up) populates `niche_sport_outcomes` with real
settled outcomes and the standard refit pipeline replaces the seed
with a real fit.

## Out of scope for #831
- Real historical backfill (5 years of settled outcomes per sport
  from Sportradar archives + public sources).
- Tennis/F1 player props (per-game serve counts, fastest-lap markets).
- Golf player props (hole-by-hole birdies).
- Boxing, NASCAR, IndyCar.
- 14-day live-volume verification (gated on the backfill landing
  first so the calibrator has real signal).
