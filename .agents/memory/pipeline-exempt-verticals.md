---
name: pipeline-exempt verticals (run a vertical while global input_mode=edge_terminal)
description: How one vertical (e.g. weather) trades via the full LLM pipeline while everything else stays on Edge Terminal; why the global READY gate parks everything; and why input_mode must fail closed.
---

# Pipeline-exempt verticals

`AXIOM_PIPELINE_VERTICALS` (→ `Settings.pipeline_verticals`) is a comma-sep
subset of the scheduled verticals that run the FULL LLM pipeline and PLACE
trades even while the global Edge Terminal `input_mode == "edge_terminal"`.
Used to turn ONE vertical's auto-trader back on without disturbing the rest
(Edge Terminal scraper produces no signals for it, so it would otherwise never
trade). Validated in config: must be known verticals AND a subset of
`orchestrator_verticals`, else boot fails loudly.

## Two independent blockers had to be cleared — not just input mode

1. **Boot-once global READY gate parks the WHOLE orchestrator.** The gate flips
   once at boot and requires EVERY enabled realtime feed to have a first poll
   outcome. If any one feed misses, the gate never flips and ALL verticals park
   (no trading_cycles at all). There is no retry. Pipeline-exempt verticals
   therefore use `scheduler._vertical_ready()` instead: it waits only on the
   core checks (`migrations`/`secrets`/`models`) plus the vertical's OWN
   discovery feed, fails closed on a failed core check, and proceeds after
   `AXIOM_READY_FEEDS_TIMEOUT_SECONDS` if its feed never polls. Non-exempt
   verticals still wait on the global gate.
   **Why:** a single flaky feed must not silently freeze the whole trader.

2. **`input_mode == "edge_terminal"` suppresses pipeline placement.** A vertical
   in `pipeline_verticals` is exempt from both the fresh-debate skip and the
   placement skip in `run_cycle`.

## input_mode must be read ONCE per cycle and fail CLOSED

`run_cycle` previously read `get_input_mode()` twice and BOTH reads defaulted
fail-OPEN to `"pipeline"` on exception. A transient control-plane error on the
second read could flip a NON-exempt vertical's placement path open and let it
place LIVE trades while the operator was actually in edge_terminal mode.
Fix: a single `cycle._resolve_input_mode()` resolves once and returns
`"edge_terminal"` on error (fail CLOSED).
**Why:** live money — a control-plane blip must never open placement for a
vertical that should be suppressed; failing closed only ever holds a non-exempt
vertical back, never the reverse (exempt verticals ignore the value).
**How to apply:** any new placement/suppression decision keyed on input_mode
must reuse the single resolved value, not re-read; keep the default closed.

## Operational

This is config + code; it is INERT until deployed to the Reserved VM. Setting
`AXIOM_PIPELINE_VERTICALS` in dev/shared changes nothing live until a publish.
api-server has no hot reload — restart the workflow after editing.
