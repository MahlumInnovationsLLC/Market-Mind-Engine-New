#!/bin/sh
# Production entrypoint for the api-server artifact.
#
# 1. Emit immediate diagnostic markers so we can prove this script
#    ran at all even if uvicorn never binds.  Stream logs through
#    ``tee`` to /tmp/start-prod.log too -- in some Reserved VM
#    failure modes stdout from pid 1 never reaches the platform log
#    pipeline, and the file lets the next redeploy/SSH inspect what
#    happened on the previous boot.
# 2. Launch chromium install in the background so the runtime image
#    stays under the 8 GiB cap (baking it in pushed past the limit
#    and the publish failed).  The edge_terminal poller tolerates
#    ``ScraperUnavailable`` while the ~150 MB download completes.
# 3. ``exec`` the watchdog so it becomes pid 1's direct child and
#    receives SIGTERM cleanly on shutdown.

set -u

LOG=/tmp/start-prod.log
: > "$LOG" 2>/dev/null || true

log() {
  # Always flush; pipe through tee so logs land in BOTH stdout
  # (platform log pipeline) and a local file for forensic recovery.
  printf '[start-prod %s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$LOG"
}

log "entrypoint executed (pid=$$, user=$(id -un 2>/dev/null || echo ?))"
log "PORT=${PORT:-<unset>}"
log "PWD=$(pwd)"

# Resolve python from PATH first (resilient to nix store path drift
# between dev and the deploy image), fall back to the dev container's
# .pythonlibs path so local sanity-checks still work.
if command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif [ -x /home/runner/workspace/.pythonlibs/bin/python3 ]; then
  PY=/home/runner/workspace/.pythonlibs/bin/python3
else
  log "FATAL: no python3 on PATH and .pythonlibs/bin/python3 missing"
  exit 127
fi
log "python: $PY ($($PY --version 2>&1))"

APP_DIR=/home/runner/workspace/artifacts/api-server
WATCHDOG="$APP_DIR/scripts/watchdog.py"
if [ ! -r "$WATCHDOG" ]; then
  log "FATAL: watchdog script not found at $WATCHDOG"
  exit 127
fi
log "watchdog: $WATCHDOG"
log "app dir: $APP_DIR"

# Background chromium install (~150 MB, 10-30s on warm VM).
(
  log "playwright install starting (background)"
  if "$PY" -m playwright install chromium >/tmp/playwright-install.log 2>&1; then
    log "playwright chromium install OK"
  else
    log "playwright chromium install FAILED (see /tmp/playwright-install.log)"
  fi
) &
log "playwright install pid=$!"

log "exec-ing watchdog -> uvicorn :8080"
exec "$PY" "$WATCHDOG" \
  --supervise \
  --target "http://127.0.0.1:${HEALTHZ_SIDECAR_PORT:-8081}/" \
  -- \
  "$PY" -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8080 \
  --app-dir "$APP_DIR"
