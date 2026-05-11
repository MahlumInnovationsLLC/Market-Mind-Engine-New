# Daily honesty report runbook (§21)

This runbook covers the daily honesty report scheduler added by the
G1–G4 implementation tasks.  The report fires once per local calendar
day, renders a PNG tail-risk chart and a multi-page PDF, and dispatches
both via Postmark email (operator + opted-in users) and Telegram
`sendDocument`.

---

## 1. Artifact locations

| Artifact | Path pattern |
|----------|-------------|
| PNG tail-risk tile | `artifacts/api-server/reports/tail_risk_tile_<YYYY-MM-DD>.png` |
| PDF daily report   | `artifacts/api-server/reports/daily_honesty_<YYYY-MM-DD>.pdf` |

`<YYYY-MM-DD>` is the **local-TZ date** at send time (see §3 below).
Both files persist across restarts — the `reports/` directory is
created automatically on first write.

---

## 2. Enabling / disabling

```
DAILY_HONESTY_REPORT_ENABLED=1   # default; set to 0 to suppress
```

When disabled the scheduler loop still starts but `_maybe_fire` returns
immediately, so no DB reads, no emails, no artifacts are produced.

---

## 3. Local-timezone scheduling (G1)

The scheduler fires at a configurable local wall-clock time.  All three
env vars below are **optional** with sensible defaults.

| Env var | Type | Default | Notes |
|---------|------|---------|-------|
| `DAILY_HONESTY_REPORT_TZ` | IANA tz name | `America/New_York` | Controls which timezone's date/time governs the fire window and the dedupe key. |
| `DAILY_HONESTY_REPORT_HOUR` | int 0–23 | derived (see below) | Local-TZ hour to fire. |
| `DAILY_HONESTY_REPORT_MINUTE` | int 0–59 | derived (see below) | Local-TZ minute to fire. |
| `DAILY_HONESTY_REPORT_HOUR_UTC` | int 0–23 | `13` (≈ 9 am ET) | **Legacy backcompat.** Used to derive `_HOUR`/`_MINUTE` when the new vars are absent. |
| `DAILY_HONESTY_REPORT_MINUTE_UTC` | int 0–59 | `0` | **Legacy backcompat.** |

### Derivation order

1. If `DAILY_HONESTY_REPORT_HOUR` is set → use it directly.
2. Otherwise convert `_HOUR_UTC`:`_MINUTE_UTC` through `_local_tz()` to
   produce the local-TZ equivalent.  This means existing deployments
   that only set `_HOUR_UTC` continue to fire at the same wall clock
   time after a redeploy.
3. `_send_minute()` follows the same priority.

### Dedupe key

The sent-marker is stored in the `notification_dedupe` table under key
`daily_honesty_report` with a value equal to today's **local-TZ date**
(`YYYY-MM-DD`).  A 21:00 PT send on May 10 will not re-fire at 00:05
ET on May 11 even though UTC has rolled over.

### DST behaviour

`zoneinfo` handles DST transparently.  On spring-forward and fall-back
days the local hour shifts by 1 h; the report fires at the correct
wall-clock time with no operator action needed.

### Invalid TZ fallback

If `DAILY_HONESTY_REPORT_TZ` is set to an unrecognised IANA name,
`_local_tz()` logs a warning and falls back to `UTC`.  No exception is
raised.

---

## 4. PNG tail-risk tile (G2)

`render_tail_risk_chart_png(payload, date_str, output_dir)` renders a
horizontal bar chart of per-position max_loss with a threshold line and
headline numbers in the chart title.

- **Requires**: `matplotlib` (soft dependency — omitting it causes the
  function to return `None` and log a warning; the rest of the report
  still fires).
- **Output**: `reports/tail_risk_tile_<date>.png` (~30–150 KB).
- **Skip condition**: returns `None` when `payload["tail_risk"]` has no
  bucket rows or contains an error key.

The path is attached to the outbound Postmark email and forwarded to
Telegram via `_broadcast_document`.

---

## 5. PDF artifact (G3)

`render_daily_report_pdf(payload, text_body, chart_path, output_dir)`
builds a multi-page PDF containing:

1. Cover page — date, overall tone, section count.
2. Divergence section — per-vertical table (days / items).
3. Calibrator drift — per-vertical delta table.
4. Tail risk — embedded PNG (if `chart_path` is provided) + numeric
   table.
5. Gate trips (24 h) — count and per-gate list.
6. Investor deck link (if configured).
7. Uncalibrated live trades — per-vertical raw-mode count table.
8. Missing-league candidates — per-market count table.

- **Requires**: `reportlab` (soft dependency — returns `None` on
  `ImportError`; the text+HTML email still dispatches without the PDF).
- **Output**: `reports/daily_honesty_<date>.pdf` (~15–150 KB depending
  on section lengths and PNG embed size).
- **Chart embed**: when `chart_path` is `None` (matplotlib missing or
  no tail-risk rows), the PDF renders without an image in §4.

The path is attached to the outbound Postmark email and forwarded to
Telegram via `sendDocument`.

---

## 6. Attachment dispatch (G4)

`alerts._dispatch(subject, body, tag, dry_run, attachments)` now
accepts an `attachments` list of `Path` objects.

- At dispatch time the bytes of each file are **materialised in memory**
  so a future `reports/*` pruner cannot strand the async user-preference
  fanout with a missing file.
- Both the operator Postmark send and the user-pref fanout receive
  identical `(filename, bytes, content_type)` tuples.
- Dry-run mode echoes string filenames in the return value without
  reading any bytes.

---

## 7. What RED looks like

Each section carries a `tone` field: `"green"`, `"yellow"`, or `"red"`.

| Section | RED trigger |
|---------|-------------|
| **Divergence** | Any vertical has `|model_prob − market_prob| > 0.15` for 3+ days |
| **Calibrator drift** | Any vertical's calibrator intercept has drifted by > 0.05 from baseline |
| **Tail risk** | Any open position's `max_loss` exceeds the configured threshold |
| **Gate trips** | Any gate fired in the last 24 h |
| **Uncalibrated live trades** | Any trade placed while the calibrator is missing/disabled (raw mode) |
| **Missing-league candidates** | Any candidate that reached the gates without a `league` field |

The email subject line starts with `[RED]`, `[YELLOW]`, or `[GREEN]`
reflecting the worst tone across all sections.  The PDF cover page and
HTML email header use the same colour coding (`#b00000` for red,
`#b08000` for yellow, `#006000` for green).

---

## 8. Manual fire

```bash
# Dry-run (no email sent, artifacts still written):
DAILY_HONESTY_REPORT_ENABLED=1 \
  python -c "
import asyncio
from app.notifications.daily_honesty_report import fire_now
asyncio.run(fire_now(dry_run=True))
"

# Live fire:
DAILY_HONESTY_REPORT_ENABLED=1 \
  python -c "
import asyncio
from app.notifications.daily_honesty_report import fire_now
asyncio.run(fire_now(dry_run=False))
"
```

Artifacts are written to `reports/` regardless of `dry_run` flag.
Check `reports/daily_honesty_<today>.pdf` and
`reports/tail_risk_tile_<today>.png` after a successful dry-run.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No email sent, log says `daily-honesty-report already sent` | Dedupe key already set for today | Delete the row from `notification_dedupe` where `key = 'daily_honesty_report'` and re-fire. |
| `PDF skipped: reportlab unavailable` | reportlab not installed | `pip install reportlab` or add to `requirements.txt`. |
| `tail-risk chart skipped: matplotlib unavailable` | matplotlib not installed | `pip install matplotlib` or add to `requirements.txt`. |
| PDF/PNG present but not received | Postmark `POSTMARK_SERVER_TOKEN` missing | Confirm env var set; dry-run mode will still write the files. |
| Wrong send time | `_HOUR_UTC` → local conversion wrong | Set `DAILY_HONESTY_REPORT_HOUR` directly in local TZ to bypass conversion. |
| TZ warning in logs | Invalid IANA name in `DAILY_HONESTY_REPORT_TZ` | Correct the env var; fallback is UTC. |
