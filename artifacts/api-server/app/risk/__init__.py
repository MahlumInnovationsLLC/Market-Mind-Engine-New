"""12-gate risk module.

Each gate is a pure async function that takes a `RiskContext` and returns
a `GateResult`.  Gates are stateless w.r.t. each other: a gate's result
must not depend on the result of a sibling gate.  The orchestrator runs
them in declared order and short-circuits on the first FAIL — every
gate either passes or returns a structured reason for failure that gets
streamed on `WS /ws/pipeline` and written into `trading_cycles.errors`.

Failure of a gate is NEVER an exception — gates that genuinely cannot
evaluate (e.g. missing operator override row) return PASS with a
"degraded" detail rather than killing the loop.
"""
from .gates import (
    GATES,
    GateResult,
    RiskContext,
    run_all_gates,
)


__all__ = ["GATES", "GateResult", "RiskContext", "run_all_gates"]
