"""AutoResearch — mine UW signals into shadow model features.

The package owns the nightly loop that correlates Unusual Whales
``uw_signals`` rows against ``trades.actual_outcome`` per vertical and
records the high-Sharpe candidates as **shadow** features. Shadow rows
are visible in the admin console; an operator promotes them to
production explicitly (no auto-promotion — see task spec).

Layout:

* :mod:`.stats` — pure numeric helpers (IC, Sharpe, monotonicity).
* :mod:`.store` — DB read/write for ``autoresearch_features``.
* :mod:`.mining` — orchestrator that runs the per-vertical job.
* :mod:`.scheduler` — wakes the mining job once per AXIOM_AUTORESEARCH_INTERVAL_SECS.
"""
from __future__ import annotations

from .scheduler import scheduler  # noqa: F401  (re-export)
from . import store  # noqa: F401
from . import mining  # noqa: F401
