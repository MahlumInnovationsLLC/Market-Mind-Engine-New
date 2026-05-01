# Axiom-Predict — Operator Runbook

Concise procedures for the on-call operator. Each section is
self-contained: the commands, the dashboard surface that confirms
the change, and the rollback path if you need to undo it.

## Table of contents

1. [Switch paper ↔ live trading](#1-switch-paper--live-trading)
2. [Rotate Kalshi API keys](#2-rotate-kalshi-api-keys)
3. [Emergency stop](#3-emergency-stop)
   - [3b. Daily-loss kill switch](#3b-daily-loss-kill-switch)
4. [Read the pipeline log](#4-read-the-pipeline-log)
5. [Roll a new model version into the bucket](#5-roll-a-new-model-version-into-the-bucket)
6. [Drift-driven auto-rollback](#6-drift-driven-auto-rollback)
7. [Postgres outage](#7-postgres-outage)
8. [Clerk / auth provider outage](#8-clerk--auth-provider-outage)
9. [Halt a single vertical](#9-halt-a-single-vertical)
10. [Cutover go/no-go checklist (paper → tiny live → full live)](#10-cutover-gono-go-checklist-paper--tiny-live--full-live)
11. [First 24 hours live](#11-first-24-hours-live)
12. [Tune WebSocket subscriber queue sizes](#12-tune-websocket-subscriber-queue-sizes)
13. [Order-submit dead-letter triage](#13-order-submit-dead-letter-triage)
14. [Stale-cancel anomaly triage](#14-stale-cancel-anomaly-triage)
15. [Per-vertical gatekeeper-knob tuning](#15-per-vertical-gatekeeper-knob-tuning)
16. [Tail-risk gate knobs (auto-armed)](#16-tail-risk-gate-knobs-auto-armed)
17. [Honest paper-fill baseline (orderbook-walk simulator)](#17-honest-paper-fill-baseline-orderbook-walk-simulator)

---

## 1. Switch paper ↔ live trading

The trading mode is controlled by `risk.trading_mode` in the
`system_settings` table. Two surfaces toggle it:

### Dashboard (preferred)

1. Sign in as an Elite-tier operator.
2. Open `/system → controls`.
3. Toggle **Live execute**. The change is persisted to the DB and
   the next orchestrator cycle reads the new mode from the
   kill-switch gate.

The state is reflected at `GET /api/orchestrator/live-execute`:

```json
{ "enabled": true, "effective_mode": "live", "source": "db" }
```

### Manual SQL (when the dashboard is unreachable)

```sql
INSERT INTO system_settings (key, value, updated_at)
VALUES ('risk.trading_mode', 'live', now())
ON CONFLICT (key) DO UPDATE
  SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at;
```

Set `'paper'` to revert. The kill-switch gate honours the DB value
over `AXIOM_TRADING_MODE` env, so an SQL flip takes effect on the
next cycle without a process restart.

### Rollback

Either toggle the dashboard back, or:

```sql
UPDATE system_settings SET value = 'paper'
 WHERE key = 'risk.trading_mode';
```

---

## 2. Rotate Kalshi API keys

Kalshi keys live in env: `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PEM`.

1. **Provision** the new key in the Kalshi web console (Settings →
   API). Capture the key id and the PEM private key.
2. **Update secrets** in Replit (Tools → Secrets):
   - `KALSHI_API_KEY_ID` → new id
   - `KALSHI_PRIVATE_KEY_PEM` → full PEM contents (BEGIN/END lines
     included). Keep the existing newlines intact — the PEM parser
     is whitespace-sensitive.
3. **Restart** the API workflow (`artifacts/api-server: API Server`).
   The Kalshi client + WebSocket subscriber rebuild on lifespan
   start; the next `/api/kalshi/balance` probe will use the new key.
4. **Verify** at `/system → health`:
   - `Kalshi REST` card: `ok`
   - `Kalshi WebSocket` card: `connected`
5. **Revoke** the old key in the Kalshi console once you have at
   least one green cycle on the new one.

### Rollback

If the new key fails, the dashboard surfaces the JWT/HTTP error in
the `Kalshi REST` card. Either restore the old key id+PEM in
secrets and restart, or keep the workflow halted while you debug
upstream.

---

## 3. Emergency stop

Two independent paths trip the kill switch — use whichever is
fastest from where you are.

### A. Dashboard

`/system → controls → EMERGENCY STOP`. Admin-only. Cancels every
resting Kalshi order, persists `risk.trading_mode = 'halt'`, writes
an `emergency_stop` audit row, and emails the on-call.

### B. cURL (when the dashboard is down)

```bash
curl -X POST https://YOUR_HOST/api/admin/emergency-stop \
  -H "Authorization: Bearer $CLERK_ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"reason":"manual e-stop"}'
```

### C. Bareback halt (auth-free, last-ditch)

```bash
curl -X POST 'https://YOUR_HOST/api/kalshi/halt?reason=panic'
```

`POST /api/kalshi/halt` intentionally has no auth dependencies so
you can halt trading even when downstream auth is broken.

### Resume

Once the cause is fixed:

```sql
UPDATE system_settings SET value = 'paper'
 WHERE key = 'risk.trading_mode';
```

Then `POST /api/kalshi/resume` (Clerk auth required) and confirm at
`/system → health` that the `Trading mode / kill-switch` card
returns to `mode=paper` (or `live`) instead of `HALTED`.

---

## 3b. Daily-loss kill switch

The `gate_daily_loss` risk gate watches today's realized + unrealized
P&L and auto-halts trading when either of two caps is breached:

- `AXIOM_DAILY_LOSS_HALT_USD` — absolute dollar cap (e.g. `500`).
- `AXIOM_DAILY_LOSS_HALT_PCT` — percent of **start-of-day** bankroll
  (e.g. `2.5` = 2.5%). The denominator is the last `bankroll_events`
  row before today in the trading-day timezone.

Either / both / neither may be set. When neither is configured the
gate is a no-op and logs a warning on every cycle so operators know
the safety net is unarmed.

### Trading-day timezone

The "today" boundary is UTC by default but is configurable so
markets can halt on a local-calendar basis (e.g. NY close at 17:00
ET for US-market deployments):

- `AXIOM_DAILY_LOSS_TZ` — any IANA zone name (e.g.
  `America/New_York`, `Asia/Tokyo`). Default: `UTC`.
- `risk.daily_loss_tz` operator override in `system_settings`
  beats the env var.

Both the realized-PnL SQL filter and the start-of-day bankroll
boundary use this zone, so a non-UTC setting shifts both ends of
the window consistently. A typo in the zone name falls back to UTC
with a warning — a mis-set boundary will never silently shift by
hours. The System → Controls card renders "Today (&lt;tz&gt;)" so
operators can see which zone is in effect at a glance.

Note: the per-day alert dedupe key is always UTC-dated
(`daily_loss_halt:<utc_date>`), independent of the display
timezone. This keeps the "one operator alert per day" invariant
stable across timezone configuration changes and prevents an
operator in a non-UTC zone from receiving two alerts that bridge
the UTC-midnight rollover. The halt **state transition**, however,
always re-runs whenever the budget is blown and the runtime mode
is not already `halt` — so if an operator manually flips back to
`paper` or `live` while still underwater, the next gate evaluation
immediately forces mode back to `halt` with no alert spam.

### Operator override (preferred)

Set per-deployment via `system_settings` so thresholds survive a
redeploy and take effect on the next cycle (no restart):

```sql
INSERT INTO system_settings (key, value, updated_at)
VALUES ('risk.daily_loss_halt_usd', '500', now())
ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now();

INSERT INTO system_settings (key, value, updated_at)
VALUES ('risk.daily_loss_halt_pct', '2.5', now())
ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now();
```

Overrides beat env vars; delete the row (or set the value to `0`) to
fall back to the env var.

### What happens when it trips

1. `system_settings.risk.trading_mode` is flipped to `halt` (same end
   state as a manual EMERGENCY STOP — `gate_kill_switch` will keep
   rejecting candidates until an operator re-arms).
2. The in-process `exchange_kill_switch.halt()` is tripped so the
   current cycle stops submitting orders immediately.
3. One `daily-loss-halt` operator email is dispatched (deduped by UTC
   date — a burst of tripping candidates only fires one alert).

### Re-enable trading (operator action)

Once you've acknowledged the loss and decided to resume:

1. Confirm the snapshot on `/system → controls` shows what you expect.
2. Flip the runtime mode back to `paper` (safer) or `live`:
   ```sql
   UPDATE system_settings SET value = 'paper', updated_at = now()
    WHERE key = 'risk.trading_mode';
   ```
3. Clear the in-process kill switch via `POST /api/kalshi/resume`
   (Clerk auth required). A cycle later the dashboard card flips
   from `TRIPPED` back to `ARMED`.

The gate does **not** auto-untrip on P&L recovery — re-enable is
always an explicit operator action.

---

## 4. Read the pipeline log

The orchestrator emits one event per stage of every candidate. They
fan out on three surfaces:

| Surface | Use when… |
|---|---|
| `/pipeline` page (web) | You want the live ticker — each trade train flips green/red across the 14 stages. |
| `WS /api/ws/pipeline` (multiplexer) | You're scripting an external listener that needs every event type. |
| `WS /api/ws/{positions,trades,alerts,temperature/{city},orderbook/{ticker}}` | You only want one event family — fewer messages, simpler clients. |
| `GET /api/orchestrator/recent-events?cycles=5` | You missed a recent cycle and want the buffered tail. |

Each event carries `cycle_id`, `market_id`, `side`, `stage`, and a
monotonic `_seq` so a client can dedupe between the snapshot REST
endpoint and the live WebSocket.

Common debugging recipes:

* **A trade that didn't place** — filter the WS for
  `type == "candidate_complete"` rows where `placed == false`. The
  `reason` field names the gate that rejected (e.g. `kelly_min`,
  `liquidity_thin`, `corr_cap`).
* **A stuck cycle** — `/system → health → orchestrator` shows the
  last completed timestamp; > 5 × cadence is `degraded`.
* **Persona disagreement** — `WS /api/ws/llm-logs` (Pro+ only)
  shows persona deltas as they stream; the moderator's verdict
  arrives last with the consensus probability.

---

## 5. Roll a new model version into the bucket

Models live in Supabase Storage under `models/<vertical>/<version>/…`.
The active version is the row in `model_versions` with
`is_active = true`; the loader picks it up on lifespan start.

1. **Upload** the new artifact via the storage CLI or SQL helper:
   ```bash
   ./scripts/upload_model.sh \
     --vertical sports \
     --version 2026-04-23 \
     --path /tmp/sports-model.bin
   ```
   The uploader streams the file in 50 MB parts and writes
   `manifest.json` (sha256 + part count) so the loader's
   re-assembler can verify integrity.
2. **Register** the new version (still inactive):
   ```sql
   INSERT INTO model_versions (vertical, version, manifest_path,
                               is_active, created_at)
   VALUES ('sports', '2026-04-23',
           'models/sports/2026-04-23/manifest.json',
           false, now());
   ```
3. **Promote** when ready:
   ```sql
   UPDATE model_versions SET is_active = false
    WHERE vertical = 'sports' AND is_active = true;
   UPDATE model_versions SET is_active = true
    WHERE vertical = 'sports' AND version = '2026-04-23';
   ```
4. **Reload** by restarting the API workflow. Confirm at
   `/system → health → Model artifacts`:
   - `manifest_ok = true`
   - `predictor_count` matches your expected verticals
5. **Watch** for drift over the next N settlement cycles at
   `/calibration` and on the `/system → health → Calibration
   freshness` card. The retrain loop will auto-rollback (write a
   `model_rollback_log` row, raise an alert, refuse to promote the
   new calibrator) if the retrained Brier is worse than the
   pre-fit raw model — see step 6 below.

### Rollback

```sql
UPDATE model_versions SET is_active = false
 WHERE vertical = 'sports' AND version = '2026-04-23';
UPDATE model_versions SET is_active = true
 WHERE vertical = 'sports' AND version = '<previous>';
```

Then restart the API workflow. The loader's manifest check will
refuse to start serving predictions if the bucket contents
disagree with the row, so a corrupted upload halts the rollback
rather than serving garbage.

---

## 6. Drift-driven auto-rollback

The settlement retrain loop fits a fresh isotonic calibrator on
buffered outcomes. If the post-fit Brier is **worse** than the
pre-fit raw Brier (i.e. the calibrator made things worse), the
loop:

1. Writes a `model_rollback_log` row capturing both Briers, the
   triggering snapshot id, and the pinned previous
   `AXIOM_MODEL_VERSION`.
2. Raises a `model_health_alerts` row with
   `alert_type = 'retrain_rollback'` (severity `warning`).
3. Tags the snapshot's `drift_status.status = 'rolled_back'` so the
   pipeline's calibrator-loader skips it.
4. **Does not** mark the consumed buffer rows processed — the
   training signal stays available for the next cycle which may
   succeed with a larger sample.

Operator action: review at `/research` (or query
`SELECT * FROM model_rollback_log WHERE acknowledged = false`).
Acknowledge with:

```sql
UPDATE model_rollback_log
   SET acknowledged = true,
       acknowledged_at = now(),
       acknowledged_by = '<your email>'
 WHERE id = <id>;
```

---

## 7. Postgres outage

Everything persistent runs through Supabase Postgres: `system_settings`
(kill switch, trading mode, daily-loss caps, vertical halts),
`app_users` (admin + approval state), `admin_audit_log`,
`bankroll_events`, `model_versions`, every trade record. When the DB
is unreachable the orchestrator parks safely — it will not submit new
orders with stale state — but you still have to drive the recovery.

### Symptoms

- `/system → health` shows the **Postgres** card as `error` with a
  `connection refused` / `timed out` / `server closed connection`
  detail string.
- `/api/healthz` still returns `200` (liveness probe is DB-free).
- `/api/health` returns `200` with `status: "degraded"` and
  `postgres.ok = false` in the body — the endpoint does not 5xx so
  approved operators can still load the dashboard and see which
  dependency is red.
- `/api/health/ready` returns `503` while the readiness gate's
  Postgres check is failing.
- API workflow logs show
  `asyncpg.exceptions.ConnectionDoesNotExistError` or
  `alembic migrations failed` in a restart loop.
- Pipeline WebSocket (`/pipeline` page) goes quiet — no new
  `candidate_complete` events for longer than the cycle cadence
  (default 60 s).
- `/api/orchestrator/status` reports a stale `last_completed_at`.

### Confirm the outage is Postgres (not auth / not network)

```bash
# Liveness — must still return 200.
curl -i https://YOUR_HOST/api/healthz

# Readiness — will go 503 when DB is down.
curl -s https://YOUR_HOST/api/health/ready | jq

# Supabase side: the project dashboard
#   https://app.supabase.com/project/frwhxftngijwcujqhltf/settings/database
# shows connection health, current PG version, and the PITR window.
```

If `/api/healthz` is also down, the outage is the API workflow itself,
not Postgres — jump to the Kalshi kill-switch (§3) and restart the
workflow.

### Where the orchestrator parks

The cycle scheduler runs `run_cycle()` inside a `try/except` so a DB
error never kills the loop; it logs `orchestrator cycle failed: …`
and sleeps for the next cadence tick. The override loader
(`_load_operator_overrides`) that feeds the risk-gate chain is
**best-effort** — when the `SELECT … FROM system_settings` raises it
returns an empty dict and the gates fall back to env vars and
defaults (most importantly `AXIOM_TRADING_MODE`, typically `paper`).

This is an important nuance for live deployments: a Postgres outage
does **not** by itself flip the kill switch to halt. If
`AXIOM_TRADING_MODE=live` and you had previously set
`risk.trading_mode = 'halt'` via SQL, that halt is effectively lost
until Postgres is back. Many gates further down the chain
(bankroll-floor, daily-loss, open-position limits) do need DB reads
and will raise during the outage, which causes their individual
candidates to error out and the cycle to log a failure — so in
practice very few candidates make it to placement — but do not rely
on that as your kill switch.

**Recommended action while Postgres is down:** pull the bareback
halt (§3C, auth-free) if you are in `live` mode. In paper mode the
worst case is a cycle that logs errors and places nothing, so you
can wait it out.

- Resting Kalshi orders stay where they are — the bus and the
  exchange state are unaffected by a Supabase outage.
- Fills that arrive over the Kalshi WebSocket are written directly
  to the DB by `_record_fill_idempotent` — if the DB write raises,
  the error is logged and **the fill is dropped** (no in-process
  buffer, no guaranteed replay). The reconciler loop below will
  pick up any missed fills from `/portfolio/fills` once the DB is
  back, which is how we stay consistent.

### Drain in-flight resting orders safely once Postgres is back

1. Wait for `/system → health → Postgres` to return to `ok`.
2. Confirm the pipeline is firing again: `/pipeline` shows new
   candidate rows within one cycle cadence.
3. Check `/system → controls` for the **Trading mode / kill-switch**
   card. If it is still showing `HALTED` because the daily-loss gate
   tripped during the outage, decide whether to resume (§3b) or keep
   it halted.
4. Reconcile Kalshi positions against our local state. The
   reconciler background loop runs on a `AXIOM_RECONCILER_INTERVAL_SECS`
   cadence (default **60 s**, minimum 10 s) in live mode
   (`kalshi/reconciler.py`, started from the lifespan hook). There
   is no manual trigger endpoint; wait one reconciler tick and then
   check `model_health_alerts` for rows of type
   `kalshi_reconcile_drift`. Any drift row surfaces on
   `/system → health → Alerts` — treat it as a trade-by-trade audit,
   not a "fix it now" alarm.

   If you need to force a reconcile immediately, restart the API
   workflow — the reconciler runs once on startup before entering
   its interval loop.
5. Only after the health card is green for a full cycle should you
   consider re-arming live trading (§10 step 7 if you were mid-cutover).

### Recovering from corrupted `system_settings` or `app_users`

If a bad write (or a prior-to-Task-#271 manual SQL edit gone wrong)
has left `system_settings` inconsistent:

1. **Read replica first** — the Supabase dashboard
   (`Database → Read Replicas`) has a read-only endpoint. Point
   `psql` at it and run:

   ```sql
   SELECT key, value, updated_at FROM system_settings ORDER BY updated_at DESC LIMIT 50;
   SELECT key, count(*) FROM system_settings GROUP BY key HAVING count(*) > 1;
   ```

   Duplicate keys are the usual symptom; the primary key is
   `system_settings.key` so this should always be zero rows.

2. **PITR restore** (Supabase point-in-time recovery, 7-day window on
   the default plan): dashboard → `Database → Backups → Point in
   Time`. Pick a timestamp **before** the bad write. Supabase will
   clone the whole database to a new project — it will not overwrite
   production. From there you can either:

   - Promote the restored clone to primary (disruptive — new
     connection string, all workflows must be restarted with the new
     `SUPABASE_DB_PASSWORD`), or
   - Export just the affected table and copy the good rows back:

     ```bash
     # From the restored clone:
     pg_dump --data-only --table=system_settings \
       "$RESTORED_DB_URL" > /tmp/system_settings.sql
     # Into prod (review the file first — it's an INSERT script):
     psql "$PROD_DB_URL" -f /tmp/system_settings.sql
     ```

3. **`app_users` corruption** — this one is painful because every
   signed-in user re-upserts themselves on the next `/api/me/access`
   call. Usually it's enough to let the natural self-heal happen:
   delete the obviously-bad rows, let users hit the dashboard, and
   they'll be recreated with correct `approved` / `is_admin` state
   from Clerk metadata. A bootstrap admin email in
   `OPERATOR_ADMIN_EMAILS` guarantees at least one user can always
   sign in and promote others from the Admin → Users panel.

### Rollback

Postgres outages rarely have a rollback — the recovery is the
rollback. The one thing you must do is confirm, once the DB is back,
that `risk.trading_mode` has the value you expect (not `halt` from a
daily-loss trip during the outage). §3 has the SQL.

---

## 8. Clerk / auth provider outage

Clerk is the identity provider for the operator console — every
`Authorization: Bearer` token on every `/api/*` endpoint (except the
explicitly public liveness and webhook routes) is a Clerk session
JWT. When Clerk is degraded the dashboard goes dark for everyone
but the trading loop itself is unaffected.

### Symptoms

- Operators see the Clerk-hosted sign-in widget return 5xx, or the
  dashboard flashes "Awaiting approval" immediately after login even
  for known-good accounts.
- API workflow logs show bursts of
  `clerk jwks fetch failed` / `clerk user lookup failed` — the JWKS
  cache is 5 minutes, so symptoms lag the upstream outage by up to
  that window.
- `/api/me/access` starts returning `401` once the JWKS cache (5 min) expires and the backend can no longer verify tokens. While the cache is still warm, individual calls may succeed intermittently — do not rely on a single green response to declare the incident over.
- Status page: <https://status.clerk.com/>.

### What still works while Clerk is down

- **Trading loop** — the orchestrator and risk gates don't call
  Clerk. Cycles keep firing, fills keep settling.
- **Kalshi kill switch** — `POST /api/kalshi/halt` is deliberately
  auth-free (see §3C "bareback halt"). You can stop trading even
  when Clerk is 100% down.
- **Operator alerts** — Postmark and Telegram fan-out from
  `notifications/alerts.py` do not depend on Clerk. If a gate trips,
  or daily-loss blows past its cap, or a settlement rollback
  triggers, you will still get email + Telegram even with the
  dashboard down.
- **Webhooks** — Stripe + Clerk webhooks authenticate via HMAC/svix,
  not Bearer tokens, so billing events keep flowing.

### Operating in "bareback halt" mode

Use this when Clerk is down and you want to stop trading *right now*:

```bash
# No auth — this endpoint exists for exactly this reason.
curl -X POST 'https://YOUR_HOST/api/kalshi/halt?reason=clerk_outage'
```

The exchange-side kill switch is tripped in-process immediately and
`risk.trading_mode` is flipped to `halt` on the next cycle (via a
DB-free in-memory flag; the DB write happens opportunistically).
Resting orders are **not** auto-cancelled by this path — if you need
that too, and the dashboard is down, use the per-vertical halt
endpoint (§9) which does cancel resting orders scoped to one
vertical, or fall back to the Kalshi web console to cancel manually.

### Verify ops alerts are still landing

During a Clerk outage, the dashboard can't tell you if the alert
pipeline is healthy. Confirm out-of-band:

1. **Postmark** — log in directly at <https://postmarkapp.com/>
   (not Clerk-gated) and check the `Activity` tab for the configured
   `POSTMARK_FROM_EMAIL` stream. Operator alerts should appear there
   within ~10 s of a trigger.
2. **Telegram** — send any message to the concierge bot; if it
   replies with the help menu, the bot's long-poll loop is alive
   (it uses a bot token, not Clerk). Pipeline events route through
   `telegram/bot.py`, not the dashboard.
3. **Fire a test alert** via the Postmark webhook endpoint (no
   Clerk): if you can reach Supabase Postgres, insert a row into
   `model_health_alerts` and the next scheduler tick (15 s) will
   dispatch.

### Recovery

1. Wait for Clerk status to return to green.
2. Hard-refresh the dashboard (Clerk's client SDK caches the failing
   JWKS for a bit — a reload forces a fresh fetch).
3. If sessions still fail to validate after Clerk is green, restart
   the API workflow — that drops the server-side JWKS cache and
   forces a fresh download on the next request.
4. Check `/api/me/access` returns `200 {approved: …}` before
   un-halting trading (§3 "Resume").

### Rollback

Not applicable — Clerk outages are upstream and the recovery is
time-based. The only local action worth reverting is the bareback
halt: after Clerk is back, re-arm via the dashboard
(`/system → controls`) or the authenticated `POST /api/kalshi/resume`.

---

## 9. Halt a single vertical

New in task #271: you can sit out one vertical (e.g. weather) while
the rest of the portfolio keeps trading. The halt is picked up by
the risk-gate chain on the very next cycle (no restart) **and**
cancels every resting Kalshi order attached to that vertical.

### Dashboard (preferred)

1. Open `/system → controls` as an admin.
2. Scroll to the **Vertical halts** card.
3. Each vertical shows a Live / Halted badge, the operator who last
   halted it, and when. Click **Halt** to sit it out, **Resume** to
   put it back on the grid.
4. Every click writes an `admin_audit_log` row (`halt_vertical` /
   `resume_vertical`). The halt response shows how many resting
   orders were cancelled — a non-zero `failed` count means one or
   more cancels were rejected by Kalshi and you should confirm in
   the Kalshi web console.

### cURL (admin JWT required)

```bash
# Halt weather with a reason captured in the audit log.
curl -X POST https://YOUR_HOST/api/admin/verticals/weather/halt \
  -H "Authorization: Bearer $CLERK_ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"reason":"NWS outage — polling stale"}'

# Resume.
curl -X POST https://YOUR_HOST/api/admin/verticals/weather/resume \
  -H "Authorization: Bearer $CLERK_ADMIN_JWT"

# List what's halted right now.
curl -s https://YOUR_HOST/api/admin/verticals/halts \
  -H "Authorization: Bearer $CLERK_ADMIN_JWT" | jq
```

### SQL fallback (dashboard + API both down)

The halt is stored as a single row in `system_settings`:

```sql
-- Halt weather.
INSERT INTO system_settings (key, value, updated_at)
VALUES (
  'risk.vertical_halted.weather',
  '{"by_email":"you@example.com","at":"2026-04-23T00:00:00+00:00","reason":"manual"}',
  now()
)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at;

-- Resume weather.
DELETE FROM system_settings WHERE key = 'risk.vertical_halted.weather';
```

The SQL path does **not** cancel resting orders — do that manually
in the Kalshi web console if you need to. The SQL path also
bypasses the audit log; reconcile by inserting an
`admin_audit_log` row after the fact for the paper trail.

### Worked example — halt weather, keep baseball live

You notice every weather candidate over the last three cycles has
been rejected by `gate_drift` because the NWS feed hasn't refreshed.
Baseball is trading normally.

```bash
# 1. Halt just weather.
curl -X POST https://YOUR_HOST/api/admin/verticals/weather/halt \
  -H "Authorization: Bearer $CLERK_ADMIN_JWT" \
  -d '{"reason":"NWS feed stale, 3 cycles of drift rejects"}'
# -> {"halted": true, "changed": true,
#     "resting_orders": {"scanned": 14, "matched": 2, "cancelled": 2, "failed": 0}}

# 2. Confirm the pipeline picks it up on the next cycle:
#    /pipeline should show baseball trade trains firing,
#    any weather candidates rejected at gate `vertical_halted`
#    with reason "vertical 'weather' is halted".

# 3. Once the NWS feed is healthy again (see /api/feeds/status),
#    resume.
curl -X POST https://YOUR_HOST/api/admin/verticals/weather/resume \
  -H "Authorization: Bearer $CLERK_ADMIN_JWT"
```

### Rollback

`POST /api/admin/verticals/{vertical}/resume` (or the UI button) is
the rollback. Both halt and resume are idempotent — a second click
is a no-op and does not double-log.

---

## 9b. Recurring halt schedules + TTL auto-resume

Task #713 layered three things on top of section 9:

1. A **TTL** (`ttl_secs`, 1 second to 7 days) you can attach to any
   manual halt. The background sweeper auto-resumes the vertical
   when the TTL expires and pages the operator with
   `vertical_halt_ttl_expired`. Leave the TTL blank for a permanent
   halt that only a human can clear.
2. **Cron schedules** (`vertical_halt_schedules`, 5-field UTC cron)
   that fire automatically — e.g. halt `crypto` every Friday 14:30
   UTC for 30 minutes around the EIA print. Each fire pages with
   `vertical_halt_scheduled_fired`.
3. A unified **history table** (`vertical_halt_history`, mirrored to
   `admin_audit_log`) that ties every halt / resume / TTL-expiry to
   the rule that fired it.

The background loop lives in `app.vertical_halt_scheduler`. It
ticks every `AXIOM_VERTICAL_HALT_TICK_SECS` seconds (default 30,
minimum 5) and can be disabled wholesale with
`AXIOM_VERTICAL_HALT_SCHEDULER_ENABLED=false` — useful in tests
or during a controlled outage. Manual halts and resumes always
work even if the loop is off; you just lose the auto-resume.

### Dashboard

`/system → controls` now has three cards:

* **Vertical halts** — adds an **Expires** column and a TTL
  (minutes) input next to the reason field on the halt prompt.
  The badge tooltip on the dashboard cards shows
  `auto-resumes at <iso>` when a TTL is set, plus `· scheduled
  rule` when the halt was rule-driven.
* **Vertical halt schedules** — add / list / delete cron rules.
  Validation rejects bad cron expressions, durations outside
  1..10080 minutes, and empty reasons before they ever leave the
  browser. The 90 s dedupe window stops a 1-minute cron from
  re-firing on the next tick.
* **Vertical halt history** — chronological feed (newest first)
  with a vertical filter. Events are colour-coded: red `halted`,
  green `resumed`, cyan `ttl_expired`. The `source` column shows
  the schedule ID when a rule was the trigger.

### cURL (admin JWT required)

```bash
# Halt weather for one hour with a TTL.
curl -X POST https://YOUR_HOST/api/admin/verticals/weather/halt \
  -H "Authorization: Bearer $CLERK_ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"reason":"NWS feed flapping","ttl_secs":3600}'
# -> halts immediately; sweeper auto-resumes ~3600s later

# Add a recurring rule: halt crypto every Fri 14:30 UTC for 30 min.
curl -X POST https://YOUR_HOST/api/admin/verticals/halts/schedule \
  -H "Authorization: Bearer $CLERK_ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"vertical":"crypto","cron_expr":"30 14 * * 5",
       "duration_secs":1800,"reason":"EIA print"}'

# List rules + history.
curl -s https://YOUR_HOST/api/admin/verticals/halts/schedule \
  -H "Authorization: Bearer $CLERK_ADMIN_JWT" | jq
curl -s 'https://YOUR_HOST/api/admin/verticals/halts/history?vertical=crypto&limit=50' \
  -H "Authorization: Bearer $CLERK_ADMIN_JWT" | jq

# Delete a rule (idempotent — second delete is a no-op 404).
curl -X DELETE https://YOUR_HOST/api/admin/verticals/halts/schedule/42 \
  -H "Authorization: Bearer $CLERK_ADMIN_JWT"
```

### Cron expression cheat sheet

5-field Vixie cron, **always UTC**:

| Expression       | Meaning                                       |
|------------------|-----------------------------------------------|
| `*/5 * * * *`    | Every 5 minutes                               |
| `0 13 * * *`     | Daily at 13:00 UTC                            |
| `30 14 * * 5`    | Every Friday at 14:30 UTC (EIA print)         |
| `0 0 1 * *`      | Midnight UTC on the 1st of every month        |
| `0 22 * * 1-5`   | 22:00 UTC every weekday                       |
| `@daily`         | Equivalent to `0 0 * * *`                     |
| `@hourly`        | Equivalent to `0 * * * *`                     |

Vixie OR semantics: when both day-of-month and day-of-week are
restricted, **either** match wins (matches `cron(8)` exactly).

### SQL fallback (sweeper down, can't reach the API)

```sql
-- See live halts and which ones have a TTL attached.
SELECT key, value, updated_at
FROM system_settings
WHERE key LIKE 'risk.vertical_halted.%'
ORDER BY key;

-- See the most recent 50 history rows (any vertical).
SELECT effective_at, vertical, event, source,
       actor_email, reason, expires_at, schedule_id
FROM vertical_halt_history
ORDER BY effective_at DESC
LIMIT 50;

-- Manually clear an expired TTL halt the sweeper missed
-- (operator paged but loop was down).
DELETE FROM system_settings
WHERE key = 'risk.vertical_halted.weather';
INSERT INTO vertical_halt_history
       (vertical, event, source, actor_email, reason)
VALUES ('weather', 'ttl_expired', 'ttl_expiry',
        'you@example.com', 'manual sweep — scheduler was down');

-- Disable a misbehaving schedule without deleting it.
UPDATE vertical_halt_schedules SET enabled = false WHERE id = 42;
```

### Rollback

* **Disable just the loop**: set `AXIOM_VERTICAL_HALT_SCHEDULER_ENABLED=false`
  and restart the api-server. All existing halts stay in place,
  but TTLs stop expiring and cron rules stop firing.
* **Drop a single rule**: `DELETE … WHERE id = …` on
  `vertical_halt_schedules`, or hit the **Delete** button.
* **Roll back the migration**: `alembic downgrade -1` from
  `artifacts/api-server`. The downgrade drops the two new tables
  and is safe — no other code reads from them.

---

## 10. Cutover go/no-go checklist (paper → tiny live → full live)

Ten steps, three phases. Do not skip a step to "save time" — each
one catches a specific failure class that has cost us money before.
The values below are the recommended starting points; adjust per
deployment but keep the **direction** (tighter → looser) constant.

### Phase A — Paper shakedown (T-7 days to T-0)

**Goal:** prove the full pipeline (feeds → predict → swarm → gates →
order placement simulation) is green for 48 hours continuously.

1. **Confirm environment is paper.**
   ```sql
   SELECT value FROM system_settings WHERE key = 'risk.trading_mode';
   -- Expected: 'paper'
   ```
   If missing, insert `'paper'` per §1. `AXIOM_TRADING_MODE=paper` is
   a belt-and-braces fallback but the DB value wins.

2. **Tighten risk knobs** via `/system → risk`:
   - `min_edge` = **0.05** (5% edge floor; live will relax later)
   - `kelly_fraction_cap` = **0.10** (10% Kelly — very conservative)
   - `agreement_rate` = **0.70** (3 of 5 personas must agree)
   - `bankroll_floor_usd` = **$100** (stop-trading buffer)

3. **Arm daily-loss caps** (§3b) — even in paper, so you test the
   alert path:
   ```sql
   INSERT INTO system_settings (key, value, updated_at)
   VALUES ('risk.daily_loss_halt_usd', '50', now())
   ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now();
   INSERT INTO system_settings (key, value, updated_at)
   VALUES ('risk.daily_loss_halt_pct', '1.0', now())
   ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now();
   ```

4. **Watch for 48 hours.** Minimum green-light criteria:
   - `/system → health` all cards green (Postgres, Kalshi REST,
     Kalshi WS, Models, Feeds, Orchestrator, Settlement).
   - `/pipeline` shows sustained cycle activity — at least one
     candidate per cycle reaches `candidate_complete`.
   - `/calibration` shows fresh snapshots for every vertical you
     intend to trade live (freshness card `ok`).
   - Zero un-acknowledged `model_rollback_log` rows (§6).
   - Zero un-acknowledged severity=`critical` `model_health_alerts`.

### Phase B — Tiny live (T-0, first 24 hours)

**Goal:** one real order placed per cycle at a bankroll a mis-fire
cannot materially hurt.

5. **Bankroll fence.** Fund the Kalshi account with the amount you
   are willing to lose in 24 hours — treat this as the upper bound
   on losses, not a target. Confirm `bankroll_events` reflects it:
   ```sql
   SELECT * FROM bankroll_events ORDER BY observed_at DESC LIMIT 1;
   ```

6. **Flip `trading_mode` to `live`.** Dashboard (§1) or SQL — does
   not matter. Do **not** relax the risk knobs yet.

7. **First cycle watch.** Park on `/pipeline` for the next cycle.
   Expected sequence, start to finish, in < 30 s:
   - One or more `candidate_complete` events with `placed == true`.
   - Matching `order_placed` event in `WS /api/ws/trades`.
   - A new row in `orders` table within ~2 s of the placement event.
   - `/system → health → Kalshi REST` stays green.

   If the first live order does not land, halt immediately
   (§3C bareback) and debug. Do not try again until root cause is
   known.

8. **24-hour soak.** Same green-light criteria as Phase A step 4,
   plus:
   - Fill rate on live orders matches paper within ±20%.
   - `kalshi_reconciler` reports zero drift.
   - Daily-loss P&L within the cap (the `/system → controls` card
     shows progress toward both caps).

### Phase C — Full live (T+24 hours onward)

**Goal:** relax to production risk envelope once the system has
earned it.

9. **Relax risk knobs** at `/system → risk` (one knob per cycle,
   never in a batch — that way you can blame a regression on exactly
   one change):
   - `min_edge` 0.05 → **0.03**
   - `kelly_fraction_cap` 0.10 → **0.25**
   - `agreement_rate` 0.70 → **0.60**
   - Daily-loss absolute cap: scale with bankroll (target ~2% of
     start-of-day equity).

10. **Arm the paging alerts.** Confirm Postmark + Telegram are
    receiving for every severity. Create a PagerDuty (or equivalent)
    integration hook into the `operator-alerts` Postmark stream so
    `critical` alerts wake the on-call. Final go-live checklist:
    - `OPERATOR_ALERT_EMAILS` includes the on-call rotation.
    - `TELEGRAM_BOT_TOKEN` is set and bindings in
      `user_telegram_bindings` cover the on-call rotation.
    - `/api/notifications/status` shows the Postmark send status
      and recent-sends tail healthy within the last hour (the endpoint
      does not aggregate Telegram — test that channel separately by
      pinging the concierge bot).

### Abort criteria (any phase)

Pull the global kill switch (§3) immediately if any of:

- `model_health_alerts` severity=`critical` unacknowledged > 5 min.
- `kalshi_reconciler` drift_count > 3 positions.
- Realized daily loss crosses 50% of the configured cap with cycle
  activity still firing (the auto-halt should catch 100%; 50% is
  your manual margin).
- Any risk-gate name starts rejecting 100% of candidates for two
  consecutive cycles — this usually means the upstream data feeding
  that gate has drifted.

---

## 11. First 24 hours live

Dashboards the on-call should keep open:

- `/system → health` — refreshed every 10 s; first place you notice
  a feed/DB/Kalshi regression.
- `/pipeline` — the live trade-train ticker. Catches placement
  failures faster than reading logs.
- `/positions` — you should see open positions accrue and settle in
  real time. A stuck position older than its market's scheduled
  settlement window is a settlement-loop regression (§6).
- `/calibration` — watch Brier trend daily. An unexplained
  day-over-day Brier regression on a vertical ≥ 0.02 is worth a halt
  (§9) until you have a hypothesis.
- `/admin → audit log` — sanity-check who did what; a halt you did
  not perform is a signal someone else needs to coordinate with you.

Alerts that must page the on-call (Postmark stream + Telegram
echo), paraphrased from `notifications/alerts.py`:

- `trading_halted` — any path that ends with
  `risk.trading_mode = 'halt'` including daily-loss, emergency-stop,
  and bareback halt.
- `daily_loss_halt` — deduped one per UTC day; the body has realized
  + unrealized components and which cap tripped.
- `kalshi_reconcile_drift` — any drift > tolerance between our
  `positions` table and the Kalshi portfolio endpoint.
- `retrain_rollback` — calibrator auto-rollback (§6).
- `kalshi_ws_stale` — WebSocket gap > 5 × cadence.
- `model_artifact_mismatch` — bucket ↔ manifest hash disagreement
  detected by the loader on startup or periodic re-check.

Do **not** page on:

- `info`-severity alerts — those collect in
  `/system → health → Alerts` for end-of-day review, not the
  middle-of-the-night pager.

---

## 12. Tune WebSocket subscriber queue sizes

Every in-process fan-out hub (`pipeline`, `settlement`, `kalshi`,
`polymarket`, `llm-logs`) hands each WebSocket subscriber its own
bounded `asyncio.Queue` so a slow / stuck client cannot pin
unbounded memory on the publisher (see
`artifacts/api-server/app/ws_backpressure.py`). The per-subscriber
ceiling is configurable per-deployment without a code change.

### Env vars

| Var | Scope | Default |
| --- | --- | --- |
| `AXIOM_WS_QUEUE_MAXSIZE` | Global default for every hub. | `256` (legacy hard-coded ceiling) |
| `AXIOM_WS_QUEUE_MAXSIZE_PIPELINE` | Per-hub override for `pipeline_event_hub` (`/api/ws/pipeline`; also one of the two hubs behind `/api/ws/positions`, `/api/ws/trades`, `/api/ws/alerts`). | unset |
| `AXIOM_WS_QUEUE_MAXSIZE_SETTLEMENT` | Per-hub override for `settlement_event_hub` (`/api/ws/settlement`; also the second hub behind `/api/ws/positions`, `/api/ws/trades`, `/api/ws/alerts`). | unset |
| `AXIOM_WS_QUEUE_MAXSIZE_KALSHI` | Per-hub override for the Kalshi market-data bus (`/api/ws/kalshi` and the per-ticker `/api/ws/orderbook/{ticker}` stream — orderbook clients subscribe to this hub, not `pipeline`). | unset |
| `AXIOM_WS_QUEUE_MAXSIZE_POLYMARKET` | Per-hub override for the Polymarket market-data bus (`/api/ws/polymarket`). | unset |
| `AXIOM_WS_QUEUE_MAXSIZE_LLM_LOGS` | Per-hub override for `/api/ws/llm-logs` (Pro+ only). | unset |

`/api/ws/temperature/{city}` is a polling loop on the WS thread, not
a hub fan-out, so none of these knobs apply to it.

**Precedence:** per-hub override > global > legacy default of `256`.
A value < 1 or non-integer is logged once and ignored (the hub
falls back to the next level), so a typo can't take the API down at
import time.

### Setting a value

The variables are read on startup. Update them in Replit Secrets
and **restart** the API workflow (`artifacts/api-server: API Server`)
— each hub captures its ceiling on construction during the
lifespan hook, so changes do **not** take effect on existing
subscribers until restart. New WebSocket connections after restart
use the new value.

```bash
# Example: bump the orderbook (kalshi/polymarket) hubs to 1024 slots,
# leave the rest at the 256 default.
# In Replit: Tools -> Secrets
AXIOM_WS_QUEUE_MAXSIZE_KALSHI=1024
AXIOM_WS_QUEUE_MAXSIZE_POLYMARKET=1024
```

### Verify the override took effect

`/api/health/snapshot` exposes the resolved per-hub ceiling under
the `WebSocket subscribers` card → `extras.queue_config`:

```bash
curl -s "https://YOUR_HOST/api/health/snapshot" \
  | jq '.cards[] | select(.key=="ws_backpressure").extras.queue_config'
# {
#   "default": 256,
#   "global_env": "AXIOM_WS_QUEUE_MAXSIZE",
#   "global_env_set": false,
#   "by_hub": {
#     "pipeline":   {"maxsize": 256,  "env": "AXIOM_WS_QUEUE_MAXSIZE_PIPELINE",   "env_set": false},
#     "settlement": {"maxsize": 256,  "env": "AXIOM_WS_QUEUE_MAXSIZE_SETTLEMENT", "env_set": false},
#     "kalshi":     {"maxsize": 1024, "env": "AXIOM_WS_QUEUE_MAXSIZE_KALSHI",     "env_set": true},
#     "polymarket": {"maxsize": 1024, "env": "AXIOM_WS_QUEUE_MAXSIZE_POLYMARKET", "env_set": true},
#     "llm-logs":   {"maxsize": 256,  "env": "AXIOM_WS_QUEUE_MAXSIZE_LLM_LOGS",   "env_set": false}
#   }
# }
```

If `env_set` is `true` but `maxsize` is the legacy default, the
value failed validation (not an integer, or < 1) — re-check the
secret and look for a `ignoring AXIOM_WS_QUEUE_MAXSIZE_*` warning
in the API workflow logs.

### When to tune this

The default of 256 is sized for a steady human-operator audience.
Bump it when `/api/health/snapshot` shows the
`WebSocket subscribers` card going `degraded` for either of these
reasons under normal load (i.e. not because one client is actually
broken):

- **Near-full queue depth** — the card detail reads
  `N subscriber(s) >=90% full (largest=…)`. Look at
  `extras.subscribers[]` for the offending hub: if multiple healthy
  clients on the same hub hover near the ceiling during bursty
  publish traffic (e.g. a fast-moving Kalshi orderbook), the hub
  needs more headroom.
- **Overflow closes** — the card detail reads
  `N subscriber(s) closed on overflow`. On `policy: "close"` hubs
  (live orderbook channels, where stale events are worse than no
  events) an overflow tears the connection down with close code
  `1011 / subscriber_backlog_overflow`. A trickle is normal during
  a publisher burst; a steady stream means the ceiling is too tight
  for the publish rate and clients are flapping.

Suggested starting points when bumping: double the current value
(256 → 512 → 1024). Keep an eye on API memory after the restart —
worst case is `subscribers × maxsize × avg_event_bytes` per hub.
Reverse the change (delete the secret, restart) if memory pressure
shows up on the API workflow.

Don't bump it as a reflex when a single subscriber is pinned at
`overflowed=true` or `qsize=maxsize` — that is the symptom of one
broken client, not a misconfiguration. Identify the client from
`extras.subscribers[].label` and disconnect it (or wait for the
silent-subscriber alert to fire and follow the alert's runbook
link).

### Rollback

Delete the per-hub override (or `AXIOM_WS_QUEUE_MAXSIZE`) from
Replit Secrets and restart the API workflow. The next snapshot
will show the resolved `maxsize` falling back through the
precedence chain to `256`.

---
## 13. Resolve a stuck position drift

Symptom: the Kalshi reconciler keeps re-firing
`[axiom][POSITION-DRIFT]` alerts for the same ticker every cycle —
either because the exchange is correct and the local DB is wrong,
or because the drift is known-benign (e.g. a settled market the
exchange hasn't propagated yet) and the noise is masking real
alerts. Drift detection lives in `app.kalshi.reconciler` and is
audited via the `position_drift_resolutions` table (migration
`0031`). The operator console for it is `/system → Reconciler`
(admin-only).

### Enable the resolution workflow

The schema lands disabled. Set the env flag to start writing rows:

```
AXIOM_RECONCILER_RESOLUTION_ENABLED=1
```

Restart the API workflow. With the flag off, the queue UI still
loads (it just shows "no rows") and operator actions still work on
any pre-existing rows — the flag only gates the reconciler's
periodic `INSERT/UPDATE` into `position_drift_resolutions`.

### State model

`position_drift_resolutions.state` is a closed enum — the queue UI,
admin endpoints, and self-heal sweep all switch on these exact
values. Two are **active** (the row blocks future alerts and the
reconciler keeps re-checking it); four are **terminal** (the row is
archived for audit and the reconciler ignores it):

| State                       | Active? | How it gets set                                                    |
|-----------------------------|:-------:|--------------------------------------------------------------------|
| `open`                      |   yes   | reconciler detects a drift with no live row                        |
| `snoozed`                   |   yes   | operator snoozes via `/admin/reconciler/drifts/{ticker}/snooze`    |
| `resolved_accept_exchange`  |   no    | operator accepts exchange truth                                    |
| `resolved_orphan_skeleton`  |   no    | reconciler auto-skeletons a fully-orphan exchange position         |
| `resolved_self_healed`      |   no    | reconciler self-heal sweep clears a drift that fixed itself        |
| `resolved_manual`           |   no    | reserved for future manual-close action (admin endpoint TBD)       |

Indexed via the partial unique index
`position_drift_resolutions_active_uidx ON (ticker)
WHERE state IN ('open','snoozed')` so an active row is unique per
ticker. All terminal states are filtered out of the default
`open`/`snoozed` queue tabs but remain visible under
`/system → Reconciler → resolved`.

### Triage the queue

1. Open `/system → Reconciler`. The default tab is the **open**
   queue — drifts the reconciler has detected but no operator has
   resolved. Each row shows `local_qty` vs `exchange_qty` and how
   long it has been outstanding (`first_seen_at`).
2. Scan `/system → Health` for the matching `position_drift`
   `model_health_alerts` entry to see the alert message dispatched
   to operators.
3. Cross-check against Kalshi's `/portfolio/positions` (or the
   exchange UI) to confirm the exchange's number is correct.

### Action: accept exchange truth

For drifts where the exchange is right and the local DB is wrong
(missed-fill, manual close, recovery edge case), click **Accept
truth**. This:

- Closes every open `positions` row for the ticker
  (`status='closed'`, history preserved).
- Inserts one synthetic row tagged `recon-accept-truth-<ticker>`
  carrying the exchange's signed quantity (side derived from the
  sign).
- Acknowledges the open `model_health_alerts` rows for the ticker.
- Marks the drift resolution row `resolved_accept_exchange` with
  the operator's email and reason.
- Sends a `[axiom][POSITION-DRIFT][RESOLVED]` confirmation email
  (subject includes the ticker and `accept_exchange_truth`).

The action is idempotent — if the drift has already been resolved
(e.g. a teammate clicked first, or the reconciler self-healed it),
re-clicks return `changed=False` without touching `positions`.

### Action: snooze

For known-benign drifts (e.g. the exchange will propagate the
correction once the next settlement cycle runs), click **Snooze**.
Choose a duration in hours (1–720, default 24); the UI converts
this into the absolute UTC ISO timestamp the API expects in the
`{ "until": "...", "reason": "..." }` body. The reconciler
validates `until` is between **+5 minutes** and **+30 days** from
now and rejects anything outside that window.

- Marks the row `state='snoozed'` with `snoozed_until = body.until`.
- Suppresses the operator alert path for the ticker until the
  snooze expires.
- Does NOT touch `positions` — this is purely a noise filter.
- Auto-expires: the next reconciler cycle after `snoozed_until`
  passes flips the row back to `open` and the alert fires again.
- Re-snoozing replaces `snoozed_until` on the same row (new
  operator email + new reason recorded).

### Auto-skeleton (orphan exchange positions)

When the exchange reports a position for a ticker we have NO local
row for at all (open or closed), the reconciler creates a synthetic
`positions` row tagged `recon-orphan-<ticker>` and writes the drift
resolution row directly to `resolved_orphan_skeleton`. The event is
logged (`reconciler.orphan_skeleton ticker=… side=… qty=…`) and the
new resolution row is the audit record — orphan auto-skeleton does
**not** raise a `model_health_alerts` row, by design (it is a
non-paging, automatic correction). This stops the diff from
re-firing on the same orphan every cycle without waking anyone —
the operator can review the synth row in `/system → Reconciler`
(under the **resolved** tab) and either close it or accept-truth it
as needed. To audit recent orphan skeletons in SQL:

```sql
SELECT * FROM position_drift_resolutions
WHERE state = 'resolved_orphan_skeleton'
ORDER BY resolved_at DESC LIMIT 50;
```

### Self-heal sweep

At the end of every reconciler cycle, any `open` resolution row
whose ticker is no longer drifting (e.g. a delayed WebSocket fill
arrived) gets marked `resolved_self_healed`. **Snoozed rows are
left alone** — a transient zero-drift cycle does not cancel the
operator's deliberate snooze.

### Disable the workflow (rollback)

Unset (or set to `0`) `AXIOM_RECONCILER_RESOLUTION_ENABLED` and
restart the API workflow. The reconciler stops writing new rows.
Existing rows stay in `position_drift_resolutions` and remain
actionable from `/system → Reconciler` so an in-progress triage
isn't lost. Migration `0031` (the `position_drift_resolutions`
table) is purely additive — no rollback of the schema is needed
unless you want to drop it outright (`alembic downgrade -1`). No
existing tables (including `positions`) are mutated by this
workflow; the synthetic correction rows the operator action inserts
are tagged via the `order_id` prefix `recon-accept-truth-<ticker>`
/ `recon-orphan-<ticker>` so audit views can filter them by
`positions.order_id LIKE 'recon-%'` without a dedicated column.

### API-down SQL fallback

If `/system → Reconciler` is unreachable (web outage, JWT misconfig,
Clerk down) and a drift alert is keeping the on-call operator awake,
both operator actions can be performed directly against Postgres
with the same effect as the admin API. Always paste the operator's
email into `resolver_email` for audit.

**Inspect the open queue:**

```sql
SELECT id, ticker, local_qty, exchange_qty, drift_qty,
       first_seen_at, last_seen_at
  FROM position_drift_resolutions
 WHERE state = 'open'
 ORDER BY first_seen_at;
```

**Snooze a drift (no `positions` change).** Use an absolute UTC
timestamp — this matches the API contract (`{until, reason}`) and
makes the snooze easy to audit later:

```sql
UPDATE position_drift_resolutions
   SET state = 'snoozed',
       snoozed_until = TIMESTAMP WITH TIME ZONE '<YYYY-MM-DDTHH:MM:SSZ>',
       resolver_email = '<operator>@axiom-predict.com',
       resolution_reason = 'API down — manual SQL snooze',
       updated_at = now()
 WHERE ticker = '<TICKER>'
   AND state IN ('open', 'snoozed');
```

**Accept exchange truth (closes locals + inserts synthetic).** The
SQL below mirrors `reconciler.accept_exchange_truth` exactly — same
`coalesce` defaults on `exchange` / `status` (so historical rows
with NULL columns are still caught) and the same `quantity = 0`
zero-out on the close step (the synthetic row carries the new
quantity, the closed rows must show zero so PnL roll-ups don't
double-count):

```sql
BEGIN;

-- 1. Close every open local row for the ticker. coalesce() matches
--    the reconciler's filter — historical rows have NULL exchange
--    or NULL status and would be missed by strict equality.
UPDATE positions
   SET status = 'closed',
       quantity = 0
 WHERE coalesce(exchange, 'kalshi') = 'kalshi'
   AND market_id = '<TICKER>'
   AND coalesce(status, 'open') = 'open';

-- 2. Insert the synthetic correction row matching the exchange.
--    Replace <SIDE> with 'yes' (positive exchange qty) or 'no'
--    (negative exchange qty) and <ABSQTY> with abs(exchange_qty).
--    The 'recon-accept-truth-' prefix on order_id is the audit
--    signal — it's how we tell synthetic correction rows apart
--    from real fills without a dedicated source column.
INSERT INTO positions (
    order_id, market_id, side, vertical, exchange,
    quantity, avg_price, status, filled_count, over_filled
) VALUES (
    'recon-accept-truth-<TICKER>', '<TICKER>', '<SIDE>', NULL, 'kalshi',
    <ABSQTY>, 0.0, 'open', <ABSQTY>, false
)
ON CONFLICT (order_id) DO UPDATE
   SET quantity = EXCLUDED.quantity,
       filled_count = EXCLUDED.filled_count,
       side = EXCLUDED.side,
       status = 'open';

-- 3. Mark the drift resolution row terminal.
UPDATE position_drift_resolutions
   SET state = 'resolved_accept_exchange',
       resolved_at = now(),
       resolver_email = '<operator>@axiom-predict.com',
       resolution_reason = 'API down — manual SQL accept-truth',
       updated_at = now()
 WHERE ticker = '<TICKER>'
   AND state IN ('open', 'snoozed');

COMMIT;
```

If `<ABSQTY>` is zero (the exchange says "no position") skip step 2
— `accept_exchange_truth` skips the synthetic insert in that
branch, and so should you.

After the API recovers, the next reconciler cycle confirms the
drift is gone. Audit views can list operator-corrected rows with
`SELECT * FROM positions WHERE order_id LIKE 'recon-%'`.

---

## 13. Order-submit dead-letter triage

When a `client_order_id` (coid) fails the WS-place → REST-confirm
recovery loop more than `AXIOM_ORDER_RECOVERY_ATTEMPT_THRESHOLD`
times (default **5**), the operator gets an
**`order_submit_stuck`** alert.  If the same coid is still failing
`AXIOM_ORDER_DEAD_LETTER_AFTER_SECS` after the first failure
(default **600s**), the dead-letter sweeper promotes it into
`order_recovery_attempts.dead_lettered_at` and the operator gets a
second alert: **`order_dead_lettered`**.

Per task #710. Both alerts route to the same `email_halt` operator
preference and dedupe on the coid (7-day TTL) so a single sustained
outage pages once, not once per sweep.

### Surface (preferred)

`/system → Stuck orders` lists every open dead-letter row with the
original intent (market, side, action, contracts, limit price), the
recovery counters, the last error, and three resolve buttons.

| Action | Use when | Side-effects |
|---|---|---|
| `force_retry` | Upstream blip cleared; you want the orchestrator to try again | Marks the row resolved. The next failure for the same coid is treated as a **fresh start** — `recovery_attempts` resets to 1 and `dead_lettered_at` clears, so a healthy retry won't re-page the operator immediately. **No** fill written. |
| `mark_cancelled` | Venue confirmed the order was rejected/cancelled and no fill exists | Marks the row resolved. **No** fill written, **no** position rollup change. |
| `claim_filled` | Venue confirmed by phone/Slack that the order DID fill at price `P` | Inserts an `order_fills` row with `source='operator_manual'` and `fill_id=manual:<coid>`, marks the row resolved, then updates the position rollup at `P`. |

`claim_filled` writes the fill **before** the resolution UPDATE — a
DB blip mid-flight leaves the row dead-lettered (caller can retry),
never resolved-with-no-fill.

### Behavior nuances

* **Dead-lettered rows do NOT auto-resolve on natural recovery.**  Once
  the operator has been paged, a later orchestrator pass that recovers
  the order naturally (`recovered_filled`, `recovered_canceled`,
  `recovered_resting`) will record the outcome in
  `last_recovery_outcome` but leaves `resolved_at IS NULL` and the row
  on the operator console.  Pick **`mark_cancelled`** if the natural
  outcome was a cancel (the WS path already wrote any fill that
  exists), or **`claim_filled`** if you want to back-fill a manual
  ledger row.  This avoids the "row vanished while I was investigating"
  UX and prevents a duplicate manual fill on a row that already
  reconciled itself.
* **Non-dead-lettered rows still auto-resolve.**  The "flapping coid
  never reaches the dead-letter queue" invariant only fires before the
  sweeper promotes the row.

### Dashboard surface

Both promotions also write a row to **`model_health_alerts`** so the
existing operator dashboard surfaces the event alongside other model-
health signals (Brier drift, position drift, settlement drift, etc.):

| When | `alert_type` | `severity` | `metric_name` | `metric_value` |
|---|---|---|---|---|
| Threshold first crossed | `order_submit_stuck` | `warn` | `<coid>` | recovery attempts so far |
| Sweeper promotes the row | `order_dead_lettered` | `high` | `<coid>` | recovery attempts at promotion |

Dedupe is 1 hour for `order_submit_stuck` (so a sustained outage
doesn't flood the table) and per-promotion for `order_dead_lettered`
(every promotion is a discrete event).  The email/paging path is the
source of truth for operator notification — these rows are a redundant
dashboard surface, so a DB blip on the INSERT is logged-and-swallowed
rather than blocking the alert.

### cURL (when the dashboard is down)

```bash
# List the open queue.
curl -s -H "Authorization: Bearer $ADMIN_BEARER" \
  "$API/api/admin/orders/dead-lettered?state=open" | jq '.items[]'

# Resolve one row (force retry).
curl -s -X POST -H "Authorization: Bearer $ADMIN_BEARER" \
  -H "content-type: application/json" \
  -d '{"action":"force_retry","reason":"upstream cleared"}' \
  "$API/api/admin/orders/<COID>/resolve"

# Resolve as a manual fill at 47¢.
curl -s -X POST -H "Authorization: Bearer $ADMIN_BEARER" \
  -H "content-type: application/json" \
  -d '{"action":"claim_filled","fill_price_cents":47,"reason":"venue confirmed by phone"}' \
  "$API/api/admin/orders/<COID>/resolve"
```

`?state=` accepts `open` (default), `resolved`, `failing`
(pre-promotion view), or `all`.

### Operator confirmation alert

Every successful resolution fires an
**`order_dead_letter_resolved`** email to the operator rotation,
deduped on `f"{coid}:{action}"` (7-day TTL) so a rapid double-click
doesn't double-page.  The audit row in `admin_audit_log` carries the
full before/after JSON keyed on `order_dead_letter_resolve:<action>`
— that's the source of truth for "who claimed which fill" if there
is ever a downstream reconciliation question.

### Tuning knobs

| Env var | Default | Effect |
|---|---|---|
| `AXIOM_ORDER_RECOVERY_ATTEMPT_THRESHOLD` | `5` | Recovery attempts on one coid before `order_submit_stuck` fires. |
| `AXIOM_ORDER_DEAD_LETTER_AFTER_SECS` | `600` | Age of `first_failed_at` before the sweeper promotes a row. |
| `AXIOM_ORDER_DEAD_LETTER_SWEEP_SECS` | `60` | How often the sweeper scans. |

Skipped entirely in paper mode — paper submits never go through the
recovery path.

### Rollback

The migration is purely additive; rollback is the standard alembic
downgrade (`alembic downgrade 0031_position_drift_resolutions`),
which drops `order_recovery_attempts` and the `order_fills.source`
column.  Old recovery code paths continue to work uninstrumented.

---

## 14. Stale-cancel anomaly triage

The cycle-start sweep (§4 "stale_resting_cancelled" health card)
cancels resting Kalshi orders older than the per-vertical TTL before
new candidates are evaluated. Per task #711 the sweep also writes one
row per cycle to `stale_cancel_observations` and runs a rolling 24h
baseline + anomaly evaluator with two operator alerts.

### Volume anomaly: `[axiom][STALE-CANCEL-VOLUME]`

You were paged because this cycle's cancel count is `>=` the
configured σ multiple above the rolling 24h mean for the vertical
(default `>= 3σ`, with a hard floor of 10 cancellations to avoid
noise alerts). The sweep itself is functioning — we are
*successfully* cancelling many more resting orders than usual.

Triage steps:

1. **Check the `Stale-cancel baseline · <vertical>` card** at
   `/system → health`. The sparkline shows the trailing ~30 cycles
   with the same scale as the alert telemetry. A single tall bar
   against a flat history is usually a transient model thrash; a
   sustained climb is a persistent issue.
2. **Diff `/pipeline`** — has placement volume jumped in the same
   window? If yes, candidates are being created and immediately
   superseded by the next cycle's view.
3. **Check the line-snapshot freshness card** for the affected
   vertical. Stale odds will produce candidates that get repriced
   away within seconds and immediately become stale.
4. **Calibration drift** — look at `/calibration` for the vertical.
   A drifted model produces marginal-edge candidates that get
   cancelled when the next prediction recomputes the prob.

This alert (and the failure-spike alert below) is dedupe-keyed on
`vertical` with a single shared 30m TTL by default
(`AXIOM_STALE_CANCEL_ALERT_COOLDOWN_SECS=1800`), so each alert type
pages at most twice per hour per vertical. The two alert types
deliberately share one cooldown dial — earlier revisions split it
into separate volume/failure TTLs and operators kept miscalibrating
them; #711 collapsed them back to one knob.

### Failure spike: `[axiom][STALE-CANCEL-FAIL]`

You were paged because the venue-side cancel API has been failing in
each of the last `failure_window` cycles (default 2). The check is
strict per-cycle: every cycle in the rolling tail must independently
have `failed >= floor` (default 3) — a single clean cycle in the tail
suppresses the alert and a recovered cycle flips it false immediately
(no 24h-aggregate latch). Unlike the aggregate
`stale_resting_cancelled` health card (which surfaces a quiet
`degraded` badge but never pages), this fires only when the
swallow-and-continue path is masking a real venue outage. Resting
orders may be staying live past their TTL.

Triage steps:

1. **Check the Kalshi REST card** on `/system → health` — is it
   degraded? Are 5xx counts spiking on
   `/api/notifications/status → http retry counters`?
2. **Spot-check `cancel_order` failures** in the orchestrator log:
   ```
   grep "stale resting cancel failed" /tmp/logs/artifacts*api-server*.log | tail -50
   ```
   - All 503/504 → venue is the issue. Wait it out, monitor.
   - Mostly 4xx ("order not found") → ws-vs-rest race; the
     reconciler has likely already cancelled them. Investigate but
     not user-facing impact.
3. **Consider operator action**:
   - If venue is down for an extended period and resting orders are
     piling up, halt the affected vertical (§9) until cancels work
     again.
   - If the failure pattern is just one specific market, ack the
     alert (this dedupe-suppresses for 30m) and continue.

### Tuning knobs

All thresholds support the same `risk.<key>.<vertical>` >
`risk.<key>` > env > default precedence as the rest of the risk
gate chain. Set system_settings rows via the dashboard so changes
survive a redeploy.

| Setting key (env name) | Default | Effect |
|---|---|---|
| `risk.stale_cancel_volume_sigma` (`AXIOM_STALE_CANCEL_VOLUME_SIGMA`) | `3.0` | σ multiple above the 24h mean to fire the volume anomaly alert. |
| `risk.stale_cancel_volume_floor` (`AXIOM_STALE_CANCEL_VOLUME_FLOOR`) | `10` | Absolute minimum cancellations per cycle to even consider the volume alert. |
| `risk.stale_cancel_failure_window` (`AXIOM_STALE_CANCEL_FAILURE_WINDOW`) | `2` | Number of consecutive most-recent cycles that must each have `failed >= floor` to fire the failure spike alert. Strict-tail check — recovery flips false on the first clean cycle. |
| `risk.stale_cancel_failure_floor` (`AXIOM_STALE_CANCEL_FAILURE_FLOOR`) | `3` | Per-cycle minimum failed-cancel count. Each cycle in the rolling tail (size = `failure_window`) must clear this floor independently. |
| `risk.stale_cancel_alert_cooldown_secs` (`AXIOM_STALE_CANCEL_ALERT_COOLDOWN_SECS`) | `1800` | Single dedupe TTL shared by both volume and failure alerts per vertical. Originally split into separate `volume_ttl`/`failure_ttl` keys but operators consistently miscalibrated them — collapsed back to one dial in #711. Per-vertical override available (see below). |

Override per-vertical by appending `.<vertical>` to the key, e.g.
`risk.stale_cancel_volume_floor.crypto = 50` keeps the global default
but raises the bar for crypto where natural cancel volume is higher.

### Observation table retention

The `stale_cancel_observations` table is GC'd to a 7-day window —
the anomaly evaluator only needs 24h, but operators get a week of
history for after-the-fact triage and the UI sparkline. The retention
sweep is fail-open: if the GC fails, rows accumulate but no alert
behavior changes.

### Rollback

The migration is purely additive; rollback is the standard alembic
downgrade (`alembic downgrade 0032_order_recovery_attempts`), which
drops `stale_cancel_observations`. The cycle's alert path is wrapped
in a try/except so a missing table is logged but never blocks. Old
recovery code paths continue to work uninstrumented.

---

## 15. Per-vertical gatekeeper-knob tuning

The four core gatekeeper thresholds — `min_edge_pp`,
`min_agreement_rate`, `max_confidence_band`, `kelly_fraction_cap` —
default to the global aggression-mode profile (`MODE_PROFILES` in
`app/runtime_settings.py`) and can be overridden globally via the
`risk.<knob>` rows in `system_settings`. Task #714 layers a third
tier on top: a **per-vertical override** keyed
`risk.<knob>.<vertical>` (e.g. `risk.min_edge_pp.crypto`).

The read path in `app.risk.gates._resolve_per_vertical_override`
honours strict precedence:

1. **Per-vertical** override (`risk.<knob>.<vertical>`)
2. **Global** override (`risk.<knob>`)
3. **Aggression-mode default** from `MODE_PROFILES`

The Kelly multiplier (`risk.kelly_multiplier_<vertical>`) keeps its
existing per-vertical-only key for backward compatibility but is
surfaced read-only in the same UI so the operator sees one effective
state.

### Operator UI

Open `/system → Swarm tuning` (admin-only). The Scope selector picks
either GLOBAL (writes `risk.<knob>`) or one of the registered
verticals (writes `risk.<knob>.<vertical>`). Each row shows the mode
default, the global override (if any), the per-vertical override (if
the per-vertical scope is selected), and the resolved effective value
with its source.

To change a knob:

1. Move the slider or type a value in the number input.
2. Optionally add a one-line **reason** — it lands verbatim in the
   audit table and the operator alert.
3. Click **Save**. Out-of-range values are rejected at the API layer.
4. Click **Clear** to remove the override and fall back to the next
   tier.

Every real change writes a row to `gatekeeper_setting_history` and
fires the `gatekeeper-override-changed` operator alert. Idempotent
re-applies (saving the same value that's already effective) are
no-ops — neither an audit row nor an alert is emitted.

### Audit history

The bottom table on the panel shows the most recent 50 changes for
the selected scope. The same data is queryable from the API:

```bash
# Last 50 changes to min_edge_pp.crypto
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$API/api/admin/gatekeeper/history?knob=min_edge_pp&vertical=crypto&limit=50"

# Last 50 GLOBAL-scope changes (any knob, vertical=null)
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$API/api/admin/gatekeeper/history?vertical=__global__&limit=50"
```

### Emergency SQL fallback (UI down)

If the dashboard is unreachable but the database is up, the same
overrides can be applied with raw SQL. Pick the right key shape:

```sql
-- Per-vertical override: tighten crypto's min edge to 8pp.
INSERT INTO system_settings (key, value, updated_at)
VALUES ('risk.min_edge_pp.crypto', '8.0', now())
ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now();

-- Global override: raise the agreement floor everywhere to 0.65.
INSERT INTO system_settings (key, value, updated_at)
VALUES ('risk.min_agreement_rate', '0.65', now())
ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now();

-- Clear an override (falls back to the next tier).
DELETE FROM system_settings WHERE key = 'risk.min_edge_pp.crypto';
```

Direct SQL **bypasses the audit table and the operator alert** — log
the change in #ops-trading by hand and replay through the UI as soon
as it's reachable so the audit row + alert backfill cleanly.

### What it does NOT do

- Does not change the LLM gatekeeper prompt or any deterministic
  gate logic — only the threshold values the gates already read.
- Does not let you tune `kelly_multiplier` from this surface (the
  existing per-vertical promotion path remains the only writer; the
  UI surfaces it read-only).
- Does not provide a per-vertical aggression-mode override — that's
  a separate, larger task. Mode stays global.

### Rollback

The migration is additive (`gatekeeper_setting_history` only) and
the read-path change in `gates.py` is a pure fall-through. To
rollback the schema, run `alembic downgrade
0035_vertical_halt_schedule_and_history`, which drops the audit
table; the read path will continue to work because it never reads
the audit table directly. To revert all per-vertical overrides
without code changes, run:

```sql
DELETE FROM system_settings WHERE key LIKE 'risk.%.%';
```

(Note the two-dot pattern — single-dot keys like `risk.min_edge_pp`
are global overrides and must be preserved unless you also want to
clear those.)

---

## 16. Tail-risk gate knobs (auto-armed)

The forensic audit (Task #743) identified two failure modes in the
historical trade log: deep-OTM tail bets and jumbo-quantity orders.
Two defensive gates run on every candidate to cap exposure on those
patterns:

* `gate_tail_price_size_cap` — caps notional and side-aware max-loss
  on markets where one side trades near zero
  (`min(YES_price, 1 − YES_price) < tail_price_threshold`).
* `gate_jumbo_quantity_cap` — hard ceiling on contract count per
  candidate.

Both gates are armed by **four knobs** in `system_settings`:

| Knob (`risk.<key>`)        | Default  | Purpose                                                  |
| -------------------------- | -------- | -------------------------------------------------------- |
| `tail_price_threshold`     | `0.10`   | Trip the tail-price gate when min(price, 1−price) < this |
| `max_tail_loss_usd`        | `400.0`  | Cap on side-aware max-loss for tail-priced markets       |
| `max_tail_notional_usd`    | `400.0`  | Cap on intended notional for tail-priced markets         |
| `jumbo_quantity_cap`       | `750`    | Hard ceiling on contract count per candidate             |

### Auto-arm on startup

The lifespan startup hook in `app/main.py` calls
`runtime_settings.ensure_tail_risk_defaults_armed()` after the
Alembic migrations land. This idempotently inserts the four default
rows above into `system_settings` if they're missing.

* The gates *also* fall back to the same defaults via in-code
  values, so behaviour is unchanged whether or not the rows exist —
  the auto-arm just makes the configuration **visible on the
  operator dashboard** and routable through the per-vertical
  override panel (see §15).
* Existing operator overrides (any value, even `0` to disable) are
  **never overwritten**. The function only writes rows that don't
  already exist.
* Each auto-arm batch logs an `auto_arm_tail_risk_defaults` row to
  `admin_audit_log` so you can pinpoint when each environment first
  armed the gates.

To verify the rows are present in production:

```sql
SELECT key, value, updated_at
  FROM system_settings
 WHERE key IN ('risk.tail_price_threshold',
               'risk.max_tail_loss_usd',
               'risk.max_tail_notional_usd',
               'risk.jumbo_quantity_cap');
```

You should see four rows with the defaults above.

### Tuning per vertical

Each knob supports a per-vertical override via
`risk.<knob>.<vertical>` (e.g. `risk.jumbo_quantity_cap.crypto`).
Resolution order, from highest to lowest precedence:

1. `risk.<knob>.<vertical>` (per-vertical override)
2. `risk.<knob>` (global override — what auto-arm writes)
3. In-code fallback in `app/risk/gates.py`

Use the **Live risk knobs** panel on `/system` to set both global
and per-vertical overrides through the UI. Setting any knob to `0`
disables that gate (or sub-check) entirely — useful for paper-only
environments. The crypto vertical typically wants looser caps
because the audit found deep-OTM crypto markets were flat (not the
bleeder); sports is the vertical the gates are sized for.

### Trip alerts

When either gate vetoes a candidate, an info-severity broadcast
fires under the `tail-risk-gate-trip` tag (mapped to `email_drift`
opt-ins, fanned out via Postmark + per-user fanout). Dispatch is
deduped on `(gate_name, market_id)` for ~6 hours so a market that
keeps tripping the gate every cycle doesn't generate dozens of
identical emails in a single day.

These alerts are **not halts** — the gate has already done its job
by rejecting the order. The notification exists so operators can:

* spot a pattern (one market tripping the gate every cycle = retire
  the market or tighten the per-vertical override);
* confirm in production that the gates are armed and firing;
* surface the trip in the operator dashboard's recent-alerts feed.

### Rollback

To revert the auto-armed defaults (and let the gates fall back to
their in-code defaults), run:

```sql
DELETE FROM system_settings
 WHERE key IN ('risk.tail_price_threshold',
               'risk.max_tail_loss_usd',
               'risk.max_tail_notional_usd',
               'risk.jumbo_quantity_cap');
```

The next API server restart will re-arm them with the audit
defaults via the lifespan hook. To prevent re-arming, also remove
the call to `ensure_tail_risk_defaults_armed()` from
`app/main.py::lifespan`.

---

## 17. Honest paper-fill baseline (orderbook-walk simulator)

**TL;DR**: as of Task #751 the paper-trading branch in
`app/kalshi/lifecycle.py` no longer fills every order at the requested
limit price for 100 % of requested quantity. It now walks the live
orderbook cache and returns a realistic `(filled_qty, vwap_price,
residual)`, which means **paper Brier scores will permanently drop
1–2 %**. That is the intended honest baseline, NOT a regression.

### Why the change

The legacy paper short-circuit (single line at the top of
`_submit_after_dedupe`) gave every paper order:

* the *best* feasible price (the limit, never any slippage);
* the *full* requested size (no partial fills, no liquidity gates).

Live execution on Kalshi does neither. Result: paper Brier was inflated
by ~5–15 % vs reality, the `online_learning_buffer` trained calibrators
on optimistic numbers, and live performance under-shot every dashboard
benchmark on cutover. Round 11 audit, bug #5.

### What the simulator does

`app/kalshi/paper_fill_sim.py::simulate_paper_fill` is a pure function
that:

1. Picks the half-book the taker would actually cross (binary
   transform: BUY YES walks the no-book at `100 − no_bid_price`).
2. Walks levels best→worst, respecting the limit price as a per-level
   ceiling/floor.
3. Returns the volume-weighted average price across walked levels and
   the residual (unfilled) quantity.

The lifecycle helper `_simulate_paper_fill_and_record` then:

* writes a row to `order_fills` with `source='paper_sim'` (or
  `paper_sim_no_book_fallback` — see below) so dashboards can split
  honest paper fills from operator/legacy rows;
* persists a `filled` lifecycle event with the simulator outcome
  under `metadata.paper_sim` (status, levels walked, residual, etc.);
* records `state='rejected'` with `reason='paper_no_liquidity' |
  'paper_limit_unfillable' | 'paper_slippage_exceeded'` when the book
  has no usable depth.

### Cold-cache fallback

The orderbook cache is populated by the WS subscriber. On cold start
(or after a Kalshi-side outage) it may have no book for the requested
ticker. The simulator returns `status='no_book'` in that case and the
lifecycle layer falls back to the legacy limit-price fill, but tags the
audit row with `source='paper_sim_no_book_fallback'` so the divergence
is queryable. This mirrors the slippage-guard's existing degraded-allow
philosophy and keeps backtests / unit tests that don't run a WS feed
working unchanged.

To audit how often you're in fallback mode:

```sql
-- Last 24h: ratio of honest vs cold-cache paper fills.
SELECT source, COUNT(*) AS rows
  FROM order_fills
 WHERE source IN ('paper_sim', 'paper_sim_no_book_fallback')
   AND received_at > now() - interval '24 hours'
 GROUP BY source
 ORDER BY rows DESC;
```

A healthy production system should sit ≥ 95 % `paper_sim`. Sustained
dominance of the fallback variant means the WS orderbook cache is
under-fed for the tickers the orchestrator is trading — investigate
WS subscriptions before trusting any paper-Brier number from that
window.

### Expected Brier shift on rollout

The Round-11 audit estimated paper Brier was inflated by 5–15 % vs
live; operators should expect a **paper-Brier increase of roughly 1–2
percentage points per vertical** (higher Brier = worse calibration)
once the simulator is fed by a healthy WS orderbook cache. That puts
the new paper number squarely on top of where the live calibrator
typically settles after a few hundred fills, which is the whole point
of the change.

To confirm the rollout landed in that band on your environment, run a
side-by-side 24 h replay (per-vertical):

```sql
-- Honest paper-Brier window (last 24h). Compare against the same
-- query on a `received_at` window from BEFORE the simulator landed
-- to see the real per-vertical shift.
SELECT pv.vertical,
       AVG((pv.predicted_prob - sm.outcome) ^ 2) AS brier
  FROM paper_vertical_predictions pv
  JOIN settled_markets             sm USING (market_id)
 WHERE pv.created_at > now() - interval '24 hours'
 GROUP BY pv.vertical
 ORDER BY pv.vertical;
```

Record the per-vertical numbers in the next operator changelog entry.
**A jump larger than ~2 points per vertical is suspicious** — likely
the `online_learning_buffer` was being fed a heavy bias from one
ticker family pre-rollout, which is worth a separate investigation
rather than being absorbed into the new baseline.

### Reverting

The simulator has no kill switch (it's the new baseline). If you
genuinely need to revert — e.g. for a regression test against pre-#751
numbers — patch
`app/kalshi/lifecycle.py::_simulate_paper_fill_and_record` to return
the legacy short-circuit:

```python
fill_price = int(req.limit_price_cents or 50)
record.state = "filled"
record.filled_count = req.count
record.avg_fill_price_cents = fill_price
await _record(req, paper=True, state="filled", price=fill_price,
              extra={"simulated": True})
await _emit({"event": "filled", "order": record.to_dict()})
return record
```

Don't ship that to production — it re-contaminates the calibrator
training set within a few hours.

## 18. Trading-mode honesty: paper/live scoping, canary, pre-flight (Task #752)

The platform runs paper and live trading side-by-side on a shared
`trades` / `bankroll_events` / `positions` schema, distinguished by a
`trading_mode` column. Round-11 audit (May 2026) found four leakage
bugs where paper activity contaminated live signals (and vice-versa).
This section documents the post-#752 contract and the operator-facing
knobs the fixes added.

### 18.1 Dashboard scoping

Every read endpoint under `/api/dashboard/*` (overview, recent trades,
vertical perf, verticals overview, tail risk exposure, placed-by,
bankroll/recent, and `trades/{trade_ref}` deep-link) accepts an
optional `trading_mode` query parameter (`paper` | `live`) and pushes
the filter into the SQL `WHERE` clause via
`dashboard._combine_where_and`. **The two ledgers are never mixed in
a single query.**

Resolution rules (`_resolve_dashboard_trading_mode` in `app/main.py`):

* `trading_mode=paper` → paper-only
* `trading_mode=live`  → live-only
* omitted              → defaults to the current
  `runtime_settings.trading_mode`; when that is `halt` we read the
  **paper** ledger so a halted dashboard can never accidentally show
  live numbers
* anything else (typo, `all`) → HTTP 400 — the legacy `all` value is
  explicitly rejected so the dashboard cannot mix the two ledgers

The trade-detail route (`GET /api/dashboard/trades/{trade_ref}`)
applies the same resolver, so deep-linking from a paper-mode list
into a live trade ID returns 404 instead of cross-mode read.

The exposure routes (`GET /api/dashboard/nfl-exposure` and
`GET /api/dashboard/leagues-exposure`) accept the same
`trading_mode=paper|live` parameter and forward it through
`dashboard.get_*_exposure` into `_open_position_aggregates` /
`_latest_bankroll`. When omitted they fall through the same
`_resolve_dashboard_trading_mode` chain, so the cap-headroom panels
reflect whichever ledger the operator is actively trading. **The
underlying cycle helpers default to `live` for legacy caller safety
— always pass `trading_mode=` explicitly from new dashboard or
risk-gate code paths so paper mode does not silently read the live
ledger.**

To verify a route's filter is in effect on a degraded dashboard:

```bash
curl -s "$API/api/dashboard/overview?trading_mode=live" | jq '.kpis.bankroll_usd'
curl -s "$API/api/dashboard/overview?trading_mode=paper" | jq '.kpis.bankroll_usd'
```

The two numbers MUST differ when both modes have activity. If they
don't, check the route's call site for a missing `trading_mode=` kwarg
on the `dashboard.*` query helper.

### 18.2 Daily-loss circuit breaker scoping

`risk.daily_loss._fetch_snapshot_inputs` filters its 4 CTEs on
`trading_mode = $3`, where the breaker resolves the mode via
`_resolve_breaker_trading_mode(overrides)`. The resolver maps:

* `live`   → `live` (the common path)
* `paper`  → `paper`
* `halt`   → `live` (safer default — a halted system reads the live
  ledger so re-enabling doesn't sneak through with a paper-only
  loss budget)
* anything else (typo, missing key) → `live`

The snapshot cache key is `(date_local, tz, trading_mode)` so the
two modes don't share cached numbers. **Symptom of regression**: a
paper losing day "trips" the live breaker, or a live losing day is
masked because the operator was reviewing paper.

### 18.3 Bankroll ledger split + paper seed

`bankroll_events.trading_mode` (migration `0041`) is `NOT NULL` with
a CHECK constraint on `('paper','live')`. `bankroll_sync` calls
`_latest_event_balance_cents(exchange, trading_mode)` on every
read; the live polling loop hardcodes `trading_mode='live'` and the
new `ensure_paper_bankroll_seeded()` helper writes one
`reason='paper_seed'` row at startup per process.

* `AXIOM_PAPER_BANKROLL_USD` (default `1000`): the seed amount in
  USD. Set it before first boot if you want paper to start at a
  different bankroll.
* The seed is **idempotent** — once a paper row exists, subsequent
  boots no-op. To reset, delete the row manually:
  ```sql
  DELETE FROM bankroll_events WHERE trading_mode = 'paper';
  ```
  Then restart the API to re-seed at the current `AXIOM_PAPER_BANKROLL_USD`.

The pre-#752 bug: the latest-row lookup ignored `trading_mode`, so a
paper seed of $1000 made the very first live observation of $250 look
like a -$750 delta on the **live** ledger. Verify the fix is in place
with:

```sql
SELECT trading_mode, count(*), max(occurred_at) FROM bankroll_events
GROUP BY trading_mode;
```

Both ledgers should grow independently. Cross-mode contamination
shows up as a single `paper_seed` row interleaved with live deltas.

### 18.4 9-point pre-flight on `POST /api/orchestrator/live-execute`

Flipping `enabled=true` (paper → live) now runs `app/orchestrator/preflight.run_preflight()`
which evaluates 9 independent predicates **in this order**:

1. `trading_mode_target` — marker, always passes (we wouldn't be here otherwise)
2. `kill_switch` — process-wide kill switch is not tripped
3. `bankroll_sanity` — live bankroll ≥ `AXIOM_PREFLIGHT_BANKROLL_FLOOR_USD` (default $100)
4. `pipeline_health` — last `trading_cycles.started_at` ≤ `AXIOM_PREFLIGHT_CYCLE_MAX_MINUTES` (default 15) min ago
5. `calibrator_freshness` — newest `calibration_snapshots` row with `drift_status='retrained'` ≤ `AXIOM_PREFLIGHT_CALIBRATOR_MAX_HOURS` (default 24) old
6. `drift_status` — no calibrator with `drift_status='rolled_back'` in the last 6h
7. `daily_loss_headroom` — used % of cap < `AXIOM_PREFLIGHT_DAILY_LOSS_MAX_USED_PCT` (default 90)
8. `swarm_provider` — no primary LLM provider's 30-min fallback rate ≥ `AXIOM_PREFLIGHT_SWARM_FALLBACK_MAX_RATE` (default 0.30)
9. `market_cooldowns` — count of currently-active `watchdog_market_cooldowns` ≤ `AXIOM_PREFLIGHT_MARKET_COOLDOWN_MAX_ACTIVE` (default 25)

A single failure returns HTTP 409 with body:

```json
{
  "detail": {
    "code": "preflight_failed",
    "failed_check": "kill_switch",
    "hint": "Kill switch is TRIPPED — clear it via `POST /api/admin/kill-switch/reset` first.",
    "checks": [ ...all 9, with .passed/.hint/.details... ]
  }
}
```

**No DB write happens on a failed pre-flight.** The flip back to
paper (`enabled=false`) is **unconditional** — pre-flight does not
gate going home. That asymmetry is deliberate: you can always
rollback safely.

#### Failure triage

* `kill_switch`: `POST /api/admin/kill-switch/reset` (operator-only)
* `bankroll_sanity`: top up the Kalshi account, wait for the next
  `bankroll_sync` tick, retry
* `pipeline_health`: check `axiom.orchestrator.cycle` workflow logs;
  the scheduler is wedged
* `calibrator_freshness`: `POST /api/orchestrator/retrain` for any
  vertical, then retry
* `drift_status`: investigate the rolled-back calibrator (see §6);
  do **not** override
* `daily_loss_headroom`: wait until tomorrow's reset (TZ-aware via
  `risk.timezone`)
* `swarm_provider`: investigate primary provider outage
  (Anthropic/OpenAI status page)
* `market_cooldowns`: query `watchdog_market_cooldowns` for the
  ticker family driving the count; usually a Kalshi sectional outage

To temporarily relax a check during a known-degraded window without
a code change, override the env var on the API artifact and
restart. **Do not leave overrides in place.** Track the override in
the operator changelog with a clear "revert by" date.

### 18.5 Canary mode (first 5 live trades after cutover)

On a paper → live flip the route calls `risk.canary.ensure_initialized()`
which writes `system_settings.risk.canary_remaining = AXIOM_CANARY_TRADES`
(default `5`). While `remaining > 0`, every live trade in `place_order`
is forced to `quantity = 1` regardless of what the Kelly sizer wanted,
and stamped with `metadata.canary = true`.

The settlement pipeline calls `risk.canary.decrement_on_settle(trade)`
on every settled trade; the helper short-circuits unless
`metadata.canary == true`. Decrement is on **settlement**, not
placement — a trade rejected by the venue (slippage, dedupe, kill
switch) never gets a metadata stamp and so doesn't consume a canary
slot. The operator gets the full N-trade safety budget against actual
end-to-end exchange round-trips.

#### Operator knobs

* `AXIOM_CANARY_TRADES`: number of trades to canary (default 5,
  `0` disables canary, negative falls back to default)
* Reset mid-canary by calling `risk.canary.reset(count)` from a
  Python REPL on the API container, or by directly writing to
  `system_settings`:
  ```sql
  UPDATE system_settings SET value = '5'
   WHERE key = 'risk.canary_remaining';
  ```
* Verify canary state:
  ```bash
  curl -s "$API/api/orchestrator/live-execute" | jq .
  ```
  (the GET response surfaces effective_mode + db_mode; query
  `system_settings.risk.canary_remaining` directly for the counter)

#### Symptom of regression

* Full-size live trades land within seconds of cutover → check that
  `place_order` invokes `force_quantity_for_live` on the `not paper`
  branch, and that the trades carry `metadata.canary = true`
* Counter never decrements → check the settlement pipeline's
  `_settle_one_trade` is calling `decrement_on_settle`
* Counter goes negative → impossible by construction (the UPDATE
  uses `greatest(0, ...)`); if you see a negative value, someone
  manually wrote it


## 19. Shadow-prediction calibration loop (Task #753)

The orchestrator only persists a `predictions` row for markets one of
the discovery feeds materialises, and only ever assigns
`model_yes_prob` to markets the edge gates surface for trading.  The
per-league calibrator therefore trains on a biased sub-population —
the universe of *traded* markets, not the full universe of *active*
Kalshi markets.  The Round-13 audit estimated this bias at +1.5–2 %
win rate over 6 months once corrected.

The shadow loop closes the bias by running on every settlement cycle
(throttled to once per hour):

1. **Predict** — `app.learning.shadow_predictor.run_cycle()` snapshots
   the current calibrated YES probability for *every* active Kalshi
   market in the live `predictions` table whose `updated_at` is in
   the last 6 h.  Rows land in `shadow_predictions` keyed
   `(market_id, hour-floored predicted_at)` so re-runs are no-ops.
2. **Settle** — `app.learning.shadow_settler.run_cycle()` joins
   `shadow_predictions` (where `settled_at IS NULL`) to live
   `predictions` on `kalshi_market_id`, copies `outcome` /
   `settled_at` onto the shadow row, and inserts a matching
   `shadow_learning_buffer` row (idempotent on
   `shadow_prediction_id`).
3. **Drift parity** — `_calibration_and_drift_shadow(vertical)`
   computes 24 h Brier off `shadow_learning_buffer` per registered
   vertical, writes a `calibration_snapshots` row tagged
   `sub_category='24h_shadow'`, and raises `model_health_alerts`
   with `alert_type='brier_drift'` (message prefixed `[shadow]`) so
   the existing retrain consumer dedupes naturally.
4. **Calibrator UNION** — `calibrators._fetch_training_data` UNIONs
   `shadow_learning_buffer` on top of the live trades x postmortems
   join and the v1 archive, so the per-league nightly refit learns
   from the unbiased universe.

### Tables

| Table | Purpose | Primary index |
| --- | --- | --- |
| `shadow_predictions` | Snapshot store, hourly inserts | `(market_id, predicted_at)` unique |
| `shadow_learning_buffer` | Settled snapshots fed into calibrator UNION | `(shadow_prediction_id)` unique partial |

Both migrations (0042, 0043) are `CREATE TABLE IF NOT EXISTS` so
re-runs on a fork are no-ops.

### Env kill switches

| Variable | Default | Effect when off |
| --- | --- | --- |
| `AXIOM_USE_SHADOW_BUFFER_FOR_CALIBRATION` | `1` | Calibrator UNION skips `shadow_learning_buffer`, falling back to live + v1 only |

The shadow predictor / settler themselves are not gated by env (they
write rows; the calibrator decides whether to consume them).  To
fully halt the loop, comment out `_run_shadow_cycle` invocation in
`app/settlement/scheduler.py::_loop`.

### Verifying a cycle ran

```sql
-- Shadow snapshots written in the last 24 h, by hour
SELECT date_trunc('hour', predicted_at) AS hr, count(*)
  FROM shadow_predictions
 WHERE predicted_at > now() - interval '24 hours'
 GROUP BY hr ORDER BY hr;

-- Shadow rows already settled and buffered for the next refit
SELECT vertical, count(*)
  FROM shadow_learning_buffer
 WHERE settled_at > now() - interval '24 hours'
 GROUP BY vertical ORDER BY 2 DESC;

-- Per-league refit's source breakdown (shadow vs live vs v1)
SELECT sub_category,
       config->'source_breakdown' AS sources,
       sample_count, brier_score, created_at
  FROM calibration_snapshots
 WHERE sub_category LIKE 'per_league:%'
 ORDER BY created_at DESC LIMIT 20;

-- Shadow-driven drift alerts in the last 24 h
SELECT vertical, severity, message, created_at
  FROM model_health_alerts
 WHERE alert_type = 'brier_drift'
   AND message LIKE '[shadow]%'
   AND created_at > now() - interval '24 hours'
 ORDER BY created_at DESC;
```

The settlement scheduler's `last_cycle.shadow` field also exposes the
per-cycle counts on the operator console — `predictor.snapshots_written`,
`settler.buffer_inserted`, `drift.alerts_raised`.

### Rollback

* **Disable calibrator consumption** —
  `replit deploy env set AXIOM_USE_SHADOW_BUFFER_FOR_CALIBRATION=0`
  then trigger a nightly refit (or wait for the next 24 h cadence).
  The next per-league fit will exclude shadow rows; existing
  calibrators stay in service via `apply()` until re-fit.
* **Halt the writer** — comment out the `_run_shadow_cycle` call in
  `app/settlement/scheduler.py::_loop` and redeploy.  Existing
  rows in `shadow_predictions` / `shadow_learning_buffer` stay
  intact for forensics.
* **Drop the tables** — both migrations have `downgrade()` paths
  that drop indexes + table cleanly.  Only do this if you've also
  flipped the env kill switch (otherwise the next calibrator refit
  will raise on the missing table — actually it won't: the
  `to_regclass` preflight gates the CTE OUT entirely on missing
  tables, so a drop is safe even with the kill switch on, but the
  loop in `_run_shadow_cycle` will keep trying to write and log
  warnings.  Belt-and-braces is to comment out the call too).

### Symptom of regression

* Calibrator refit's `source_breakdown` shows `shadow: 0` while
  `shadow_learning_buffer` has rows → check the
  `AXIOM_USE_SHADOW_BUFFER_FOR_CALIBRATION` env, then the preflight
  log line `[calibrators] shadow_learning_buffer preflight failed`.
* `shadow_predictions` keeps growing but `shadow_learning_buffer`
  stays small → settler can't find matching settled `predictions`
  rows; check that the upstream Kalshi settler is stamping
  `predictions.settled_at` / `predictions.outcome` (it should via
  `app/feeds/kalshi_crypto_markets.py` for crypto and
  `app/settlement/pipeline.py::settle_market` for sports).
* Shadow drift alerts firing but trade-driven drift quiet → the
  shadow universe is detecting model drift before the trade gates
  surface a bad bet — by design, this is the **early warning** the
  loop was built for.  Check the affected vertical's
  per-league calibrator snapshot breakdown to confirm shadow rows
  outnumber live rows by a reasonable margin (≥ 5 :1 typical).
