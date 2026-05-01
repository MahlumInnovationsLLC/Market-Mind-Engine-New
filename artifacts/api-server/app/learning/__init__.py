"""Shadow-prediction calibration loop (task #753).

Two cooperating modules:

* :mod:`app.learning.shadow_predictor` — snapshots the model's
  calibrated YES probability for every active Kalshi market on an
  hourly cadence, irrespective of whether we traded the market.
* :mod:`app.learning.shadow_settler`  — closes the loop once the
  underlying market resolves: copies the public outcome onto the
  shadow snapshot and feeds the (predicted_prob, actual_outcome)
  pair into ``shadow_learning_buffer``.

The buffer is then UNION'd into the per-league nightly calibrator
refit (see :mod:`app.calibrators`) so the calibrator learns from
the unbiased universe of Kalshi markets, not just the
sub-population our edge gates surfaced for trading.
"""
from __future__ import annotations

from . import shadow_predictor, shadow_settler  # re-export


__all__ = ["shadow_predictor", "shadow_settler"]
