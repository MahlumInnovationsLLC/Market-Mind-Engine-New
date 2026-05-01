# Path to +50% Win Rate — Execution Plan

**Source:** `attached_assets/mme-round5-full-audit-and-50pct-roadmap_1777645433692.md`
(Round 5 audit, repo @ `1398a58`, 2026-04-30)

**Owner:** Replit Agent (executes here) and/or Mac Studio cutover (per gate)
**Target:** 53–55% trailing-30-day win rate vs closing-line implied probability
**Time budget:** ~16 days of focused work to live cutover

---

## TL;DR — The arithmetic

YTD 2026: 5,347 SYSTEM trades, **−$5,878 net.**
Forensic audit finding: **0.28% of trades (15) drove 163% of losses (−$9,604).**
Strip those 15 out → engine is **+$3,726 YTD** at $0.70/trade EV.
The two new tail-risk gates (already shipped in code: `cef7cf4`, `3660ff3`)
are designed to prevent that exact pattern. **The path to +50% win rate is
arithmetic, not architecture.**

---

## The 5 levers (from §4 of the audit)

| # | Lever | Effort | Win-rate impact |
|---|---|---|---|
| 1 | Tail-risk gates ON in production | 0 — confirm settings | **+5–8% on headline win rate** |
| 2 | Honest paper fills (orderbook walk) | 1 day | corrects paper baseline |
| 3 | Shadow-prediction loop | 4 days | +1.5–2% via cleaner calibration |
| 4 | Crypto calibrator on V1's 595k rows | 2–3 days | new high-volume edge stream |
| 5 | Champion-challenger A/B on calibrators | 2 days | prevents 1–5% regressions on each refit |

---

## Prioritized task list

> Tasks are ordered by **risk-adjusted value × independence**. Items marked
> **[do here]** are safe to execute in this Replit workspace. Items marked
> **[Mac Studio]** are gated on the cutover runbook and should land later.
> Effort estimates are person-days for an experienced engineer.

### Phase 0 — Confirm gates are live (today, 30 min) **[do here]**

- **T-01 · Verify tail-risk knob defaults are persisted in `system_settings`** (lever #1)
  - Read `system_settings.risk_knob_overrides` for `tail_price_threshold`, `max_tail_notional_usd`, `max_tail_loss_usd`, `jumbo_quantity_cap`. If any are NULL, write defaults: `0.10 / $400 / $400 / 750`.
  - Open the new tail-risk-exposure tile on the dashboard and confirm zero deep-OTM open positions.
  - Acceptance: tile shows green for "Last 30 days · Manual + System · All verticals" and the four knobs have non-null overrides.

- **T-02 · Add a Telegram/email alert when a tail-risk gate trips** (lever #1, defensive)
  - Wire `gate_tail_price_size_cap` and `gate_jumbo_quantity_cap` into the existing `alert_messages` plumbing with severity `info` (not `error` — these are working as designed).
  - Acceptance: trip a gate in a paper cycle, confirm alert lands.

### Phase 1 — Honest paper fills (1 day) **[do here]**

- **T-03 · Ship `app/kalshi/paper_fill_sim.py` (Round 11 Bug #5)** (lever #2)
  - Replace the limit-price-100%-fill branch in `lifecycle.py:877` with an orderbook-walking fill simulator that consumes the live Kalshi order book and computes a realistic fill price + partial-fill quantity.
  - Add unit tests covering: full fill, partial fill, no-liquidity reject, slippage > tolerance reject.
  - Acceptance: paper Brier on a 24-h replay drops 1–2% (this is correct — honest baseline).

### Phase 2 — Paper/live honesty fixes (2 days) **[do here]**

- **T-04 · Filter dashboard P&L queries by `trading_mode` (Round 11 Bug #1)**
  - Audit every read-side query in `dashboard.py` that pulls trade rows and ensure paper trades cannot leak into a live equity curve and vice-versa.
  - Acceptance: toggle `trading_mode` in dev, confirm equity curve and KPI tiles re-scope cleanly.

- **T-05 · Filter daily-loss circuit breaker by `trading_mode` (Round 11 Bug #2)**
  - Same scope as T-04 but on the auto-halt side. A paper $1,000 loss must not halt live trading and vice-versa.
  - Acceptance: simulated paper loss > daily limit does not halt live; simulated live loss > daily limit does halt live.

- **T-06 · Split bankroll ledger: paper vs live (Round 11 Bug #4)**
  - Seed paper bankroll from `AXIOM_PAPER_BANKROLL_USD` env var; persist as a separate ledger keyed by `trading_mode`.
  - Acceptance: tests show two independent ledgers, no cross-contamination.

- **T-07 · Add 9-point pre-flight checklist to `POST /api/orchestrator/live-execute` (Round 11 §IV)**
  - Hard-block live execute if any of the 9 health checks fail (calibrator freshness, gate health, bankroll sanity, kill-switch state, etc.). Return `409 Conflict` with the failed check name.
  - Acceptance: integration test covers each check independently.

- **T-08 · Canary mode — first 5 live trades forced to qty=1**
  - Add a `live_trades_in_canary_window` counter; while < 5, override quantity to 1 regardless of Kelly output.
  - Acceptance: integration test on a fresh-cutover paper bankroll proves the cap.

### Phase 3 — Shadow-prediction loop (4 days) **[do here]**

- **T-09 · Migrations 054 (`shadow_predictions`) + 055 (`shadow_learning_buffer`)**
  - Schema mirrors `predictions` + `online_learning_buffer` but rows are written for **every active Kalshi market**, not just the ones we trade.
  - Acceptance: migrations apply cleanly, rollback works, schemas reviewed.

- **T-10 · `app/learning/shadow_predictor.py` + `shadow_settler.py`**
  - Predict on every active Kalshi contract on a schedule; settle against public outcomes; write to the shadow buffer.
  - Acceptance: 24-h dry run produces N predictions where N >> live trades, and settled rows match Kalshi's resolution.

- **T-11 · Extend `calibrators._fetch_training_set` to UNION shadow-settled**
  - Calibrator refit pulls from both `online_learning_buffer` and `shadow_learning_buffer`. Add a `source` column for audit.
  - Acceptance: refit on the unioned set; confirm Brier improves on out-of-sample slice.

- **T-12 · Drift detection on shadow buffer parallel to live buffer**
  - Wire shadow-buffer drift into the existing drift alert pipeline; tag alerts with `source=shadow` so operators can tell them apart.
  - Acceptance: synthetic drift on shadow rows triggers an alert.

### Phase 4 — Crypto calibrator (2–3 days) **[do here]**

- **T-13 · Add `crypto` to `SUPPORTED_LEAGUES` calibrator list** ✅ (Task #754)
  - Schema work: ensure `predictions.league` accepts `crypto` and the calibrator registry knows about it.
  - Acceptance: smoke test predicts on a crypto contract end-to-end.
  - Done: `CRYPTO` registered in `app/calibrators.py::SUPPORTED_LEAGUES`; `apply(p, league='CRYPTO')` no longer bypasses with `unsupported league` (forward predictor already calls `apply(p, league='CRYPTO')` at `app/crypto/predictor.py`).

- **T-14 · Bootstrap crypto calibrator from 595k V1-archive crypto rows** ✅ (Task #754)
  - Re-use the existing isotonic fit pattern; train on the V1 archive subset and persist as a `calibration_snapshots` row.
  - Acceptance: V1 backtest harness shows crypto Brier drops from "no-op" to a measurable improvement (target: ≥0.05 reduction on the V1 archive crypto rows).
  - Done: `scripts/bootstrap_crypto_calibrator.py` — pulls V1 crypto rows (`vertical='crypto'`), fits isotonic (Platt fallback under `ISOTONIC_MIN_SAMPLES`), persists with `source_breakdown={'v1_archive_bootstrap': n}`. `scripts/backtest_v1_archive.py` now derives `league='CRYPTO'` from `vertical='crypto'` so the harness report surfaces a CRYPTO entry. Runbook §20 documents invocation, expected Brier delta, and verification SQL.

- **T-15 · Wire `crypto/backtest_scheduler.py` (6-h cadence) to refit + score** ✅ (Task #754)
  - Cron the existing backtest scheduler to also write a fresh calibration snapshot every 6 h.
  - Acceptance: 24-h soak shows snapshots written on schedule and dashboard surfaces the freshness.
  - Done: `check_now()` calls `calibrators.refit_all()` after the per-pair sweep, gated by `CRYPTO_BACKTEST_REFIT_CALIBRATORS` (default ON, kill-switchable). Sweep payload + scheduler `_status` carry a `last_calibrator_refit` block (`fitted/skipped/errors/duration_seconds`) the dashboard surfaces; failure of the refit does not abort the sweep.

### Phase 5 — Champion-challenger A/B (2 days) **[do here]**

- **T-16 · `ALTER TABLE calibration_snapshots ADD COLUMN role text DEFAULT 'champion'`**
  - Migration + backfill all existing rows to `champion`. Seed first challenger from a held-out training slice.
  - Acceptance: migration up/down works; existing predictions unaffected.

- **T-17 · Predictor returns `(p_champion, p_challenger)` and only champion drives trades**
  - Update `app/predictions.py` to evaluate both, log both, but only feed champion downstream.
  - Acceptance: dashboard tile shows both probabilities side-by-side per league.

- **T-18 · Auto-promotion — challenger wins OOS Brier 2 snapshots in a row → swap roles**
  - Add temporal OOS holdout in `calibrators.py`; persist `brier_in_sample` + `brier_out_of_sample`.
  - Acceptance: synthetic challenger that's better wins promotion; one that's worse never promotes.

### Phase 6 — Mac Studio cutover (1 day) **[Mac Studio]**

- **T-19 · Run all 4 preflight scripts; flip `AXIOM_LLM_MODE=local`; cron the 6-h crypto backtest, daily V1 backtest, hourly shadow predict.**
  - Owner: Mac Studio runbook; this is a deploy task, not a code task.

### Phase 7 — Live cutover (1 day) **[Mac Studio]**

- **T-20 · 100 paper cycles end-to-end + 7 days clean tail-risk tile + flip `risk.trading_mode=live`**
  - Pre-flight 9-point checklist (T-07) gates the flip; canary mode (T-08) auto-engages.

### Phase 8 — Ongoing observability **[do here]**

- **T-21 · Daily automated "honesty report" emailed/Telegrammed to operator**
  - Bundles: shadow-vs-live divergence chart, calibrator drift status, tail-risk tile screenshot, gate-trip log.
  - Acceptance: report lands at 09:00 local; manual smoke shows all panels populated.

- **T-22 · Weekly V1-backtest harness re-run with `parity_gate_status` surfaced on dashboard**
  - Track until issue #731 (upstream feature snapshot import) lands and the gate flips to `PASS`.

- **T-23 · Monthly investor deck refresh — equity curve + per-league Brier delta chart**
  - Self-serve from the audit-report JSONs in `artifacts/api-server/reports/`. No new code, just a runbook entry + a `.pptx` template.

---

## Critical-path summary

| Sequence | Tasks | Cumulative days |
|---|---|---|
| Today | T-01, T-02 | 0.5 |
| Day 1 | T-03 | 1.5 |
| Days 2–3 | T-04, T-05, T-06, T-07, T-08 | 3.5 |
| Days 4–7 | T-09, T-10, T-11, T-12 | 7.5 |
| Days 8–10 | T-13, T-14, T-15 | 10 |
| Days 11–12 | T-16, T-17, T-18 | 12 |
| Days 13–14 | T-19 (Mac Studio cutover) | 14 |
| Day 15 | 100 paper cycles soak | 15 |
| Day 16 | T-20 (live flip) | 16 |
| Ongoing | T-21, T-22, T-23 | — |

**Total to live cutover: ~16 person-days.** Each phase is independently shippable
and provides incremental value; the order above maximises win-rate impact per day
of work invested.

---

## What to claim to investors today (verbatim from audit §6)

> Market-Mind-Engine is a 172,000-line monorepo with 4,574 passing backend tests,
> 50 database migrations, 22 fitted per-league calibrators, 19 institutional-grade
> risk gates, a 6-persona LLM swarm with provider-diverse failover, and 8 vertical
> markets in production.
>
> A formal paired-Brier delta on 5,000 V1 trades shows MME beats V1 on every
> league with sufficient data, with zero regressions: NBA Brier 0.60 → 0.10,
> NCAAB 0.54 → 0.17, NHL 0.45 → 0.12, MLB 0.26 → 0.08, NFL 0.73 → 0.61. The
> residual 598,000 rows are V1 crypto placeholders that have no per-league
> calibrator (a gap we're closing in 2–3 days — see T-13/T-14/T-15).
>
> A YTD forensic audit found that 0.28% of trades drove 163% of losses. We
> shipped two new risk gates this week that prevent that pattern in production
> (T-01 confirms they're armed). Without those 15 trades, the engine is
> +$3,726 YTD on $8,400 paper bankroll — a 44% return.
>
> We're 16 days from live cutover.
