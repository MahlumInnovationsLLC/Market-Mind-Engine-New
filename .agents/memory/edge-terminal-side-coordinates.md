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

---

# Soccer market resolution + LLM line-only false positives

**Kalshi has no live generic `KXSOCCER*` series.** `KXSOCCERGAME/SPREAD/TOTAL`
return empty. Soccer is listed per competition with full GAME/SPREAD/TOTAL:
`KXEPL*, KXLALIGA*, KXSERIEA*, KXLIGUE1*, KXMLS*, KXUCL*, KXUEL*, KXBUNDES*`.
Cross-league fixtures (e.g. PSG vs Arsenal) live under the cup series (KXUCL/KXUEL).
So `_SERIES_BY_VERTICAL_PICK[("soccer", spread/total/moneyline)]` must fan across
all competition series, not the empty generic ones. Cricket: only `KXIPL*` (club
IPL) exists — internationals like India@Zimbabwe have NO Kalshi market.

**LLM resolver guard — team overlap is mandatory.** `llm_pick` will happily latch
a totals/over-under line onto an UNRELATED fixture (saw `UNDER 2.5` for
India@Zimbabwe matched to a Ligue 1 Nantes–Toulouse total purely on the number).
`resolve_match` now requires `_has_team_overlap(chosen_row, ...)` — the LLM-picked
market's title/ticker must share ≥1 team token or alias code with the matchup, else
it's rejected as no_match.

**Why:** placing a bet on the wrong game is strictly worse than placing none. The
hallucination guard (ticker∈candidates) is not enough — the wrong market CAN be a
real candidate when many same-line totals exist across leagues.

**How to apply:** never accept an LLM ticker without verifying it names a signal
team. Any expansion of candidate breadth (more series) raises line-only collision
risk — keep the overlap guard in lockstep.
