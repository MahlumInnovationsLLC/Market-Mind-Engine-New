---
name: Edge Terminal poller silent stall
description: Why the single-task Edge Terminal poller can silently stop scraping/trading with no error and no restart, and the invariant that prevents it.
---

# Symptom
"Tons of fresh signals in Edge Terminal but almost nothing trading." The poller
status row (`edge_terminal_poller_status` id=1) freezes: `last_poll_at` stuck
hours in the past, `last_error_category = ok`, no exception logged, and the
deployment never auto-restarts.

# Root cause
The poller is a **single asyncio task**. Each tick `await`s the scrape and then
`await`s the executor drain. Any `await` inside the tick that hangs with no
wall-clock bound wedges the *entire* loop forever. The prime offender is
Playwright: `p.chromium.launch()` / `browser.close()` / driver IPC have no
asyncio timeout, so a wedged chromium call hangs the task indefinitely. Because
the rest of the process keeps running (the healthz sidecar still answers), the
prod watchdog sees a healthy app and never restarts it — so the stall is silent
and permanent until a manual redeploy.

# Invariant (how to apply)
**Every `await` on the single poller tick must be wall-clock bounded.** Wrap
each external call in `asyncio.wait_for(...)` so a hang becomes a recorded error
and the next tick recovers. Currently bounded: the scrape
(`SCRAPE_TICK_TIMEOUT_SECONDS`) and the drain (`DRAIN_TICK_TIMEOUT_SECONDS`),
plus `browser.close()` inside the scraper's finally. If you add a new `await` to
the tick (or a new unbounded network/DB call inside scrape/drain), bound it too.

**Why:** a per-navigation Playwright timeout is NOT enough — it doesn't cover
launch/close/IPC, and "the process is up" is not the same as "the poller is
advancing." Trust the status row's `last_poll_at` recency, not process liveness.

# Diagnosing
- `last_poll_at` far older than the 40–60s interval ⇒ stalled (not "quiet board").
- Deployment logs go silent while `getDeploymentInfo()` still reports
  `deploymentType: vm` + `hasSuccessfulBuild: true` ⇒ wedged task, not a crash.
- Recovery requires a **redeploy** (republish) — the wedged build won't self-heal
  until the code with the timeouts is live.
