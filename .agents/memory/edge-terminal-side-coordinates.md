---
name: Edge Terminal NO-side coordinates
description: model_prob is the pick's win-prob (not P(YES)); NO orders price/size/submit in NO coordinates, never flipped.
---

# Edge Terminal side coordinates (executor `_fire_one`)

**Rule:** The scraped `model_prob` is the win-probability of the *pick the tile shows*
(an "Under"/"No" tile's % is the model's confidence IN the Under, which already IS the
NO side). It is NOT a YES-coordinate probability. So for NO picks, `model_prob` must be
used **directly** — never flipped to `1 - model_prob` — for the confidence floor, the
Kelly win-prob, and `edge_signed = model_prob - pick_price`.

**Pick price:** `pick_price = price` for YES, `1 - price` for NO (where `price` is the
YES-quoted price). All gating/sizing/edge math runs against `pick_price`. The model_prob
inference fallbacks (edge_inferred / ev_inferred) must also recover against `pick_price`.

**Submission:** the Kalshi lifecycle stamps `limit_price_cents` **directly** as
`no_price` for NO orders (and `yes_price` for YES). So `RiskContext.current_price`,
`intended_notional_usd`, and `cand["kalshi_price"]` must all carry `pick_price` — a NO
order built with the YES quote bids e.g. 62¢ for a NO contract that should cost 38¢.

**Why:** the old code assumed `model_prob = P(YES)` and flipped for NO, which rejected
genuine +EV NO/Under picks as "low confidence" AND placed actually-EV-negative ones (a
0.30 pick became 0.70 and traded). Live prod (frwhxftngijwcujqhltf, table
`edge_terminal_signals`) showed zero trades placed in 24h; the UNDER 8.5 row with
`model_prob=0.61` and scraped `edge_pct=+13.9` only makes sense if 0.61 is the Under's
win prob, and YES picks have `model_prob ≈ implied(avg_odds)` — proving model_prob is
pick-relative.

**How to apply:** any future change to Edge Terminal sizing/pricing/gating must keep
model_prob and price in the *bought side's* coordinates end-to-end (floor → Kelly → edge
→ RiskContext → cand). Don't reintroduce a `1 - model_prob` flip.
