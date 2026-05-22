# Top Edges Today + Bookmaker Metrics tiles

Operator dashboard tiles introduced by **task #832**. Both render on the
web (`/dashboard`) and mobile (`(tabs)/index`) consoles, refetch every
30 seconds, and surface a "computed at" timestamp.

## Endpoints

| Method | Path | Notes |
| ------ | ---- | ----- |
| `GET`  | `/api/dashboard/top-edges?limit=25`            | Pending candidates ranked by `|edge|` desc. |
| `GET`  | `/api/dashboard/bookmaker-metrics?window=7d\|30d\|ytd` | Profit-factor / equiv-unit P&L / hit rate / today's top conf+edge / picks-by-vertical. |
| `POST` | `/api/dashboard/candidates/{prediction_id}/snooze` | Body `{hours, reason?}`. Suppresses the candidate from the executor + tile for `hours` (cap 30 days). |
| `POST` | `/api/dashboard/candidates/{prediction_id}/fire`   | Clears any active snooze (sets `snoozed_until = now()`). Next executor cycle is free to fire it. |

All four routes are gated by `require_approved_user`, same as the rest
of `/api/dashboard/*`.

## Data sources

* **`predictions`** (existing) — candidate pool for Top-Edges + today's
  `top_confidence_today` / `highest_edge_today` / `picks_today`.
* **`trades`** (existing) — settled-only rows feed profit-factor,
  equivalent-unit P&L, hit rate.
* **`system_settings.risk.vertical_halted.*`** (existing) — read via
  `runtime_settings.get_vertical_halts()`; rows whose vertical is
  halted are returned with `vertical_halted=true` so the tile greys
  them rather than hiding them.
* **`prediction_snoozes`** (new, migration `0054`) — single write-side
  table: `(prediction_id PK, snoozed_until, by_user, by_email,
  reason, created_at, updated_at)`. Active snoozes are excluded from
  the Top-Edges tile **and** from the executor's `_candidate_predictions`
  selector in `app/orchestrator/cycle.py`.

## "Equivalent units" label

Our live position sizing is fractional-Kelly, so raw "units" don't
mean what the bookmaker world expects. The tile reports an
**equivalent-unit P&L** computed as `sum(pnl) / unit_dollars` with a
reference stake `unit_dollars = $100` (echoed in the response so the
UI can show "1u = $100"). This makes the headline number directly
comparable to industry "+18.71u" callouts without misrepresenting our
sizing model.

## Halt UX

Halts greyed, not hidden:

* Web: `data-halted` attribute on each row, plus a `HALTED` chip.
* Mobile: 50% opacity on the row, plus `· HALTED` in the meta line.
* Fire and Snooze buttons are disabled on halted rows.
* Footer line: `Halted: <comma-separated list>`.

## Snooze contract

* Default snooze is **1 hour** (web + mobile buttons).
* Cap is **30 days (720h)**; over-cap requests are silently clamped.
* `ON CONFLICT (prediction_id) DO UPDATE` — latest write wins, so
  re-snoozing extends/refreshes the cutoff intuitively.
* "Fire" calls the same writer with `hours=0`, so `snoozed_until =
  now()`. The unique constraint prevents accidental duplicates.

## Runbook

* **Tile shows "Failed to load"** → check the API server logs for
  `top_edges_today` / `bookmaker_metrics` warnings; both helpers swallow
  DB errors into an `error` field but log a `warning:` line first.
* **Stale "computed at" timestamp** → 30s refetch is client-driven via
  TanStack Query. If you see drift, confirm the tab is foregrounded
  and that React-Query refetch isn't paused via `enabled=false`.
* **Snoozed candidate still firing** → confirm `prediction_snoozes`
  has a row with `snoozed_until > now()`. The executor checkpoint
  lives in `app/orchestrator/cycle.py::_candidate_predictions` — both
  the league-filtered and unfiltered branches LEFT JOIN the snooze
  table.
* **Equivalent-unit number looks wrong** → confirm `unit_dollars`
  matches the constant `_BOOKMAKER_UNIT_DOLLARS = 100.0` in
  `app/dashboard.py`. Changing the denominator there changes the
  reference stake — leave it at $100 unless the operator team agrees.

## Tests

* `artifacts/api-server/tests/test_dashboard_top_edges_and_bookmaker.py`
  — empty / multi-row / halt-greyout / error pass-through / window
  mapping / PF math / snooze writer.
* `artifacts/web/src/components/dashboard/top-edges-tile.test.tsx`
  + `bookmaker-metrics-tile.test.tsx` — vitest snapshot + halt
  greyout + window-tab interaction.
* `artifacts/mobile/components/__tests__/TopEdgesAndBookmakerTiles.test.tsx`
  — Jest + RTL render coverage for both tiles.
