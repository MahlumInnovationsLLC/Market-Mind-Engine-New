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
- `/api/healthz` still returns `200` (liveness probe is DB-free), but
  `/api/health` and `/api/health/ready` return `503` with
  `checks.postgres.ok = false`.
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
and sleeps for the next cadence tick. The kill-switch gate
(`gate_kill_switch`) reads `risk.trading_mode` from `system_settings`
on **every** candidate — when the lookup raises it fail-closes to
`halt`. Net effect while the DB is down:

- No new orders are submitted (gate is fail-closed).
- Resting Kalshi orders stay where they are — the bus and the
  exchange state are unaffected by a Supabase outage.
- Fills that arrive over the Kalshi WebSocket are buffered in-process
  until the DB is back, then flushed to `orders` / `order_fills`.

Do **nothing** during the outage except watch — the system is
correctly parked. Pulling the global kill switch now just means
extra work later to un-park.

### Drain in-flight resting orders safely once Postgres is back

1. Wait for `/system → health → Postgres` to return to `ok`.
2. Confirm the pipeline is firing again: `/pipeline` shows new
   candidate rows within one cycle cadence.
3. Check `/system → controls` for the **Trading mode / kill-switch**
   card. If it is still showing `HALTED` because the daily-loss gate
   tripped during the outage, decide whether to resume (§3b) or keep
   it halted.
4. Reconcile Kalshi positions against our local state — the
   reconciler loop does this automatically every 5 minutes in live
   mode, but you can force it with:

   ```bash
   curl -X POST https://YOUR_HOST/api/kalshi/reconcile \
     -H "Authorization: Bearer $CLERK_ADMIN_JWT"
   ```

   A non-zero `drift_count` in the response surfaces as a
   `model_health_alerts` row of type `kalshi_reconcile_drift` — treat
   that as a trade-by-trade audit, not a "fix it now" alarm.
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
- `/api/me/access` returns `503` with `auth provider unreachable`.
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
    - `/api/notifications/status` shows every channel `ok` within
      the last hour.

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
