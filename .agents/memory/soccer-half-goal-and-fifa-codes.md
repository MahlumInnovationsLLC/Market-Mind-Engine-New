---
name: Soccer half-goal handicap == moneyline + FIFA codes
description: Why soccer "+0.5/-0.5" picks must route to the winner market and why team codes need a FIFA map, in the Edge Terminal Kalshi resolver.
---

# Soccer half-goal handicap is a moneyline bet

A 0.5 Asian handicap can never push, so in soccer it is mathematically a 3-way
winner (moneyline) bet, NOT a spread:
- `<team> -0.5` == that team must win → YES on the team's winner market.
- `<team> +0.5` == that team wins OR draws == opponent does NOT win → NO on the
  OPPONENT's winner market.

Kalshi lists no 0.5 spread market (spread lines start at 1.5), so a `+0.5`
underdog like "Romania +0.5" had no spread candidate and died as
`skipped_unresolved`. The resolver must reroute soccer + spread + |line|==0.5 to
the GAME (winner) series and derive the side from the sign.

**Why:** the SPREAD series only carries 1.5/2.5 "wins by over X.5" lines; the
correct equivalent of a half-goal line lives in the winner market.
**How to apply:** scope the reroute to `vertical=="soccer"` (the draw outcome is
what makes 0.5 == moneyline). 3-way winner markets (GEO/ROU/TIE) share ONE
title, so only the ticker tail code disambiguates — the scorer's tail
pick/other hit is what keeps GEO above TIE; never let TIE win by accident.

# Kalshi national-team codes are FIFA, not first-three-letters

Kalshi abbreviates national sides with FIFA codes (Romania→ROU, Netherlands→NED,
Spain→ESP), which the generic first-three-letters heuristic gets wrong, so the
right team never matched in the ticker segment (e.g. `GEOROU`). A curated
nation→FIFA map is required; it also resolves collisions (Chile=CHI vs
China=CHN, India=IND vs Indonesia=IDN).

# Scorer must use the SAME code evidence as the both-teams gate

The matchup gate matched on rich evidence (alias + nation + trigram + acronym)
but the scorer scored on curated aliases ONLY, which are empty for soccer. So a
market that genuinely named both teams passed the gate then scored 0 and was
discarded. Keep the scorer's picked/other codes aligned with the gate's
`_side_evidence`, or non-aliased sports silently never trade.
