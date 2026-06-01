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
0.30 pick became 0.70 and traded). Live prod showed zero trades placed in 24h; an
UNDER row with `model_prob=0.61` and scraped `edge_pct=+13.9` only makes sense if 0.61
is the Under's win prob, while YES picks have `model_prob ≈ implied(avg_odds)` — proving
model_prob is pick-relative.

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

**Matchup integrity — BOTH teams + date, never one-team.** A resolver (heuristic
*or* LLM) will latch a line onto the WRONG game: a totals number onto an unrelated
fixture (UNDER 2.5 India@Zimbabwe → a Ligue 1 total), a right-pick-team/wrong-opponent
market, or a stale market days off the signal date. One-team overlap is NOT enough.
The accepted market must satisfy ALL of: (1) date within ±1 day of the signal; (2)
BOTH matchup sides present — enforced identically at heuristic ranking AND LLM
acceptance, not just ranking.

**Side identity is the subtle part (shared cities/words):**
- Match short codes only inside the ticker's *team-code segment* (`…26MAY30PSGARS-2`
  → `PSGARS`), never the whole ticker — series prefix/outcome suffix cause trigram
  collisions.
- City/place words shared by co-located rivals (new, york, los, angeles, san,
  chicago) must NOT prove a side: don't expand their broad aliases (`york`→every NY
  team code), and the ordered acronym (NYY, side-distinct from NYM) does the work.
- A single shared *non-place* token must not prove a side either (Manchester City vs
  Manchester United) — the name-token path requires ALL distinctive tokens present;
  abbreviated names (PSG) are rescued by the team-code-segment match.

**Why:** a bet on the wrong game is strictly worse than no bet. The hallucination
guard (ticker∈candidates) is insufficient — the wrong market is often a real
candidate when same-line totals or same-city/same-day fixtures coexist. Over-rejection
(no trade) is the acceptable failure direction; a wrong-game trade is not.

**How to apply:** keep BOTH-teams + date gating in lockstep with any widening of
candidate breadth (more series, looser scoring). Prefer side-distinct evidence
(acronym, nickname, code in the team segment) over city/shared tokens.

---

# Signed spreads: resolver owns BOTH the target market and the side

Kalshi only lists "<team> wins by over X.5" markets — there is no native
"covers the spread" market. So a handicap pick must be remapped:
- **"+X.5"** (underdog cover) → resolve to the **OPPONENT's** over-X.5 market and
  recommend **NO** (i.e. "opponent does NOT win by over X.5" ⇔ pick covers).
- **"-X.5"** (favourite) → keep the **PICK team's** over-X.5 market, recommend **YES**.
- **no sign** → leave side unset; honour the scraper.

`market_resolver.resolve_match` carries this as `ResolveResult.side`; for "+" it
swaps the picked/other team evidence so the scorer targets the opponent market.
The executor overrides the tile side with `rr.side` when set, and re-quotes the
resolved ticker live (the scraped price belongs to the wrong tile's coordinates).

**Why:** the scraper resolves a signed spread to the FAVOURITE's own market with a
YES tile — betting the exact opposite of the pick with live money. A scraper-supplied
`kalshi_ticker` is normally authoritative, so the direct-ticker short-circuit in
`resolve_match` re-introduced the bug for signed spreads.

**How to apply:** for `pick_type=="spread"` with a non-None `_spread_sign`, DO NOT
honour the scraper's direct ticker — force full sign-aware resolution so the target
market and side are derived in the resolver. If resolution fails, return no_match
(skip the trade) rather than falling back to the scraper ticker — a wrong-side spread
trade is strictly worse than no trade. Unsigned spreads still honour the direct ticker.
`model_prob` stays pick-relative (the NO side already represents the pick covering);
do not flip it for the "+" → NO remap.
