# Re-tuning safety thresholds after a meta-model retrain (task #814)

Task #732 shipped `scripts/tune_safety_thresholds.py` and a tuned set
of per-vertical defaults derived from the V1 settled-trade tape.
Every retrain of the meta-model shifts the realised return
distribution, so those recommended thresholds drift over time. Task
#814 wires the sweep into the retrain pipeline so the defaults stay
aligned with the live model.

## What runs automatically

1. **Inside the retrain CLI** —
   `scripts/retrain_meta_model.py` calls
   `app.notifications.safety_threshold_tuner.run_after_retrain(reason="retrain")`
   immediately after publishing a fresh `meta_model.pkl`. The sweep
   is best-effort: a failure logs a warning and prints
   `safety_tune.ok = false` in the JSON summary, but does NOT fail
   the retrain. The CLI flag `--skip-safety-tune` opts out (used for
   testing / dry runs).

2. **Daily fallback scheduler** —
   `app.notifications.safety_threshold_tuner.start()` runs at API
   boot and re-fires the sweep every
   `SAFETY_THRESHOLD_TUNER_INTERVAL_SECONDS` (default 86_400 = 1
   day). This catches any out-of-band retrain on a CLI host that
   never hits the in-process hook.

Each run writes a timestamped report to
`artifacts/api-server/scripts/reports/safety_threshold_tuning_<UTC>.json`.
Older reports are kept on disk so the operator console can compute a
drift column against the prior run.

## What operators see

The Health tab on the operator console gains a
**Safety-threshold tuning** panel that surfaces the latest sweep:

* One row per vertical (`__all__` first, then the per-vertical
  picks) with the recommended value for each of
  `sharpe_min_ratio`, `ruin_max_probability`, `segment_max_losses`.
* Each cell shows the delta vs the prior on-disk sweep. Cells whose
  recommendation changed are highlighted so a drift jumps off the
  page.
* The panel links to the raw JSON via
  `/api/health/safety-threshold-tuning/report.json` so an operator
  can pull the full sweep grid for triage.

## Promoting new recommendations

Recommendations are **not** auto-applied. To promote a new pick into
`app/runtime_settings.py::PER_VERTICAL_NEW_GATE_DEFAULTS` after
operator review:

1. Pull up the **Safety-threshold tuning** panel and confirm the
   delta is supported by a healthy sample count (`samples` column).
   A vertical whose `samples` collapsed is the most common reason
   for a spurious pick.
2. Download the underlying JSON via the panel's "full report" link
   and verify the sweep grid for the affected `(vertical, knob)`
   pair: the kept-PnL curve should peak at the new pick by more than
   the picker's `TIE_TOLERANCE_PCT` (1%), otherwise the launch
   default is fine.
3. Edit `PER_VERTICAL_NEW_GATE_DEFAULTS` in
   `artifacts/api-server/app/runtime_settings.py`. Only add the
   `(knob, vertical)` rows that **differ** from the global spec
   default in `RISK_KNOB_SPECS` — verticals that match the global
   pick are deliberately omitted so the table stays small.
4. Update the rationale comment block above the dict with a one-line
   note explaining why this vertical needs the override, citing the
   new report filename so a future audit can reproduce the picks.
5. Open a PR; CI runs `pytest tests/test_runtime_settings.py` and
   the gate-side tests so the override fans out correctly to
   `tuned_default` / `_resolve_per_vertical_override`.
6. After merge, the next orchestrator cycle picks the new defaults
   up. No DB write is needed — operators with an explicit override
   in `system_settings` still beat the new spec.

## Rolling back

If a freshly promoted default misbehaves in production:

* **Revert the `PER_VERTICAL_NEW_GATE_DEFAULTS` edit** — the
  predictor and gates immediately fall back to the spec default on
  the next cycle (no DB write needed).
* **Pin an explicit operator override** — set
  `risk.<knob>.<vertical>` in `system_settings` via the
  Live-risk-knobs panel. This wins over both
  `PER_VERTICAL_NEW_GATE_DEFAULTS` and the spec default.

## Environment knobs

| Variable                                    | Default | Purpose                                                  |
| ------------------------------------------- | ------- | -------------------------------------------------------- |
| `SAFETY_THRESHOLD_TUNER_ENABLED`            | `1`     | Set to `0` to disable the daily scheduler.               |
| `SAFETY_THRESHOLD_TUNER_INTERVAL_SECONDS`   | `86400` | Cron cadence for the fallback sweep.                     |
| `SAFETY_THRESHOLD_TUNER_BOOT_GRACE_SECONDS` | `120`   | Stagger the first run so we don't pile onto boot.        |
| `AXIOM_SAFETY_THRESHOLD_REPORTS_DIR`        | unset   | Override the reports dir (used by tests).                |
