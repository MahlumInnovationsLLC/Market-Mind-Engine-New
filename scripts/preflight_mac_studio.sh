#!/usr/bin/env bash
# Mac-Studio cutover preflight.
#
# Runs the four mandatory gates in order. Any non-zero exit stops the
# script — operators must clear every gate before flipping
# AXIOM_TRADING_MODE from "paper" to "live".
#
#   Gate 1/4  verify_v155_artifacts.py        — model artifacts load
#   Gate 2/4  preflight_calibrators.py        — per-league Platt curves
#   Gate 3/4  preflight_swarm_smoke.py        — full swarm round-trip
#   Gate 4/4  preflight_paper_cycle.py        — one orchestrator cycle
#
# Usage (from repo root):
#   ./scripts/preflight_mac_studio.sh
#
# Override the python interpreter via the PYTHON env var (handy on Mac
# Studio where uv is the canonical runner: `PYTHON="uv run python"`).

set -euo pipefail

# Resolve repo root regardless of where the script was invoked from so
# the relative API-server paths below stay correct.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

API="artifacts/api-server"
PY="${PYTHON:-/home/runner/workspace/.pythonlibs/bin/python3}"

PASS="\033[32mPASS\033[0m"
FAIL="\033[31mFAIL\033[0m"

# ANSI sequences blow up "less" / log aggregators that don't speak
# colour. Silence them when stdout isn't a TTY.
if [[ ! -t 1 ]]; then
  PASS="PASS"
  FAIL="FAIL"
fi

run_gate() {
  local label="$1"
  shift
  echo
  echo "=== $label ==="
  echo "    \$ $*"
  # IMPORTANT: capture the gate's exit code BEFORE any control flow,
  # otherwise `$?` after `if "$@"; then ...; fi` reflects the if-
  # statement (always 0 on the false-no-else path), not the gate.
  # The `|| rc=$?` form also defuses `set -e` so the script doesn't
  # abort before we get to print the FAIL line.
  local rc=0
  "$@" || rc=$?
  if [[ $rc -eq 0 ]]; then
    echo -e "    -> $PASS  $label"
    return 0
  fi
  echo -e "    -> $FAIL  $label (exit $rc)" >&2
  return "$rc"
}

# ---------------------------------------------------------------------------
# Gate 1/4 — model artifacts load through models_loader.ensure_artifacts()
# ---------------------------------------------------------------------------
run_gate "Gate 1/4: verify_v155_artifacts" \
  $PY "$API/scripts/verify_v155_artifacts.py"

# ---------------------------------------------------------------------------
# Gate 2/4 — per-league calibrator snapshots are fitted (not cold-start)
# ---------------------------------------------------------------------------
run_gate "Gate 2/4: per-league calibrator-snapshots" \
  $PY "$API/scripts/preflight_calibrators.py"

# ---------------------------------------------------------------------------
# Gate 3/4 — full swarm round-trip in local mode (no hosted-LLM spend)
#
# We force AXIOM_LLM_MODE=local so the gate proves the Ollama path is
# actually wired end-to-end. Mac Studio operators who haven't yet
# pulled the local models will see this gate fail with a clear
# transport error from the runner.
# ---------------------------------------------------------------------------
AXIOM_LLM_MODE=local run_gate "Gate 3/4: swarm smoke (AXIOM_LLM_MODE=local)" \
  $PY "$API/scripts/preflight_swarm_smoke.py"

# ---------------------------------------------------------------------------
# Gate 4/4 — one full orchestrator cycle in paper mode
# ---------------------------------------------------------------------------
AXIOM_TRADING_MODE=paper run_gate "Gate 4/4: 1-cycle paper trade" \
  $PY "$API/scripts/preflight_paper_cycle.py"

echo
echo -e "$PASS  All four preflight gates passed. Safe to proceed with the"
echo "      paper-trade soak step in MIGRATION_MAC_STUDIO.md."
