# Axiom-Predict — Operator Runbook

Concise procedures for the on-call operator. Each section is
self-contained: the commands, the dashboard surface that confirms
the change, and the rollback path if you need to undo it.

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
