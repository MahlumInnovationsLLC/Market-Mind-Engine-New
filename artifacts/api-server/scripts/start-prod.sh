#!/bin/sh
# Production entrypoint for the api-server artifact.
#
# 1. Launch chromium install in the background so the runtime image
#    stays under the 8 GiB cap (baking it in pushed past the limit
#    and the publish failed).  The edge_terminal poller tolerates
#    ``ScraperUnavailable`` while the ~150 MB download completes
#    (10-30s on a warm VM).
# 2. ``exec`` the watchdog so it becomes pid 1's direct child and
#    receives SIGTERM cleanly on shutdown.
#
# Kept as a shell script (vs. an inline ``sh -c``) so the pid 1
# supervisor on Reserved VM (gce) doesn't trip on quoting / backgrounded
# subshell semantics that caused the promote step to hang.

set -eu

PY=/home/runner/workspace/.pythonlibs/bin/python3
APP_DIR=/home/runner/workspace/artifacts/api-server

(
  if "$PY" -m playwright install chromium >/tmp/playwright-install.log 2>&1; then
    echo "playwright chromium install OK"
  else
    echo "playwright chromium install FAILED (see /tmp/playwright-install.log)"
  fi
) &

exec "$PY" "$APP_DIR/scripts/watchdog.py" \
  --supervise \
  --target http://127.0.0.1:8080/api/healthz \
  -- \
  "$PY" -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8080 \
  --app-dir "$APP_DIR"
