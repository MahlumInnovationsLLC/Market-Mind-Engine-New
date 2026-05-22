"""Niche individual-sport calibration scaffold (task #831).

Per-sport feature builders + calibrator seed helper for the four niche
verticals competitor edgeterminal.ai prominently features:

  * Tennis (ATP / WTA) — surface, surface-Elo, head-to-head.
  * Formula 1          — circuit category, grid-vs-qualifying delta,
                         wet-race history.
  * PGA                — course history, strokes-gained breakdown,
                         course-fit by length & green type.
  * UFC                — weight class, reach differential, takedown
                         defense vs takedown attempt rate.

The features are intentionally null-safe stubs — they emit a fixed-
width vector with deterministic defaults so the feature-builder
pipeline does NOT NaN on a cold start.  Real fit values land when the
follow-up backfill job populates ``niche_sport_outcomes`` (migration
0053) and the calibrator scaffold helper re-fits against real data.
"""
from . import calibrator_scaffold, features  # noqa: F401
