---
name: Settlement scheduler heavy-maintenance cadence
description: Why the settlement loop staggers retrain + margin-refit off every-tick, and the monotonic cold-start gate gotcha.
---

# Settlement scheduler heavy-maintenance cadence

The api-server settlement loop (`app/settlement/scheduler.py`) settles trades
every ~180s tick. The two heaviest maintenance steps — online retrain +
isotonic refit, and per-league margin-regressor refit — are deliberately
staggered onto their own longer cadence (env-tunable, default 900s) instead of
running on every tick.

**Why:** running both heavy steps on every settlement tick, on top of ~1.2 GiB
resident models in a single-process Reserved VM, periodically starved the main
asyncio event loop. The platform health check hits the *main app port*; when it
timed out the platform recreated the VM (~40s outage, no traceback), producing
recurring downtime (~every 15 min). Most ML fits already use
`asyncio.to_thread`; the problem was concentration/frequency, not un-offloaded
work. Staggering reduces how often the loop is blocked.

**How to apply:**
- Cadence is controlled by `AXIOM_RETRAIN_INTERVAL_SECONDS` and
  `AXIOM_MARGIN_REFIT_INTERVAL_SECONDS`. `0` = opt out (run every tick).
- Core trade settlement (`run_cycle`) must keep running every tick — only the
  heavy maintenance is gated.
- `_last_*_at` is updated only when the gated block actually executes (incl.
  failed attempts) so a failure doesn't trigger a retry storm.

**Monotonic cold-start gotcha:** interval gates use `time.monotonic()`, which on
Linux is host uptime. A freshly recreated VM can have uptime < interval, so a
naive `now - last >= interval` gate with `last == 0.0` would *skip* the intended
warm-once on cold start. The gate must treat `last == 0.0` ("never run this
process") as explicitly due. Same pattern applies to the shadow-cycle gate.

**Packaging gotcha:** `app.settlement` re-exports the `scheduler` *instance*,
which shadows the `scheduler` submodule attribute on the package. `import
app.settlement.scheduler as x` (and `from app.settlement import scheduler`) both
bind to the instance, not the module — use `importlib.import_module(...)` to get
the real module (e.g. in tests that need the module-level helpers).
