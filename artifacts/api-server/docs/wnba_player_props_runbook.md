# §22 — WNBA Player Props Vertical

> **Honest note up front.** A 21-2 opening-weekend run is almost
> certainly small-sample noise. The point of this vertical is to ship
> the **system** that produces calibrated player-prop edges over a
> full season, not to replicate one weekend. The calibration panel in
> the daily honesty report (§21) is the source of truth for whether
> the vertical is actually edged. Until you see ≥4 weeks of
> well-calibrated output (Brier ≤ 0.27, hit-rate within 5pp of
> implied), keep the vertical disabled.

## Status

The vertical ships **disabled by default**
(`VerticalSpec.enabled = False`) and is opt-in via:

```sh
AXIOM_ORCHESTRATOR_VERTICALS=...,wnba_player_props
```

Even when the env-list opt-in is set, candidates also have to clear
`gate_uncalibrated_vertical` — WNBA has no fitted calibrator
snapshot on day one, so live trading also requires the explicit
raw-mode override:

```sql
INSERT INTO system_settings (key, value)
VALUES ('risk.uncalibrated_raw_mode_wnba_player_props', 'true')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
```

This is intentional friction. Trip every override before live
trading, not silently.

## Environment variables

| Var | Purpose | Default |
|---|---|---|
| `THE_ODDS_API_KEY` | Required for both the WNBA game-odds sweep and the per-event player-props endpoint. Reuses the existing key. | (none) |
| `SPORTRADAR_WNBA_KEY` | Optional. Enables the Sportradar WNBA daily-schedule poller. ESPN public scoreboard backstop fills the gap when this key is absent. | (none) |
| `AXIOM_WNBA_PROPS_MIN_SAMPLE` | Minimum number of recent games a player must have in `player_stat_history` before the predictor will produce a probability. | `5` |
| `AXIOM_WNBA_PROPS_LINE_MOVE_BPS` | Line-move threshold (in basis points of the prop line) above which `gate_player_props` rejects the candidate as stale. | `300` |
| `AXIOM_WNBA_PROPS_MAX_MATCHUP_ADJ` | Hard cap on the combined pace + opponent-allowed multiplicative adjustment. Adjustment hitting this bound rejects the candidate. | `0.25` |
| `AXIOM_WNBA_PROPS_CADENCE_SECS` | Player-props poller cadence. | `300` (5 min) |
| `AXIOM_PLAYER_PROPS_BLOCKING_STATUSES` | Comma-separated injury statuses that block a prop. | `OUT,DOUBTFUL,QUESTIONABLE,GAME TIME DECISION,GTD` |

## Data feeds

| Feed | Cadence | Source | Notes |
|---|---|---|---|
| `the_odds_api` | 30s | TheOddsAPI `sports/basketball_wnba/odds` | Game-level h2h/spreads/totals; reuses the existing poller after `basketball_wnba` was added to `AXIOM_ODDS_SPORTS` defaults. |
| `wnba_player_props` | 5 min | TheOddsAPI per-event player-props endpoint | Polls each cached WNBA event for the 12 prop markets; persists to `player_props_quotes`. |
| `sportradar_wnba` | 4 hr | Sportradar `wnba/trial/v8` daily schedule | Optional — skipped without `SPORTRADAR_WNBA_KEY`. |
| `espn_scoreboard` (WNBA) | 10 min | ESPN public scoreboard JSON | Free fallback for the WNBA schedule when Sportradar key is absent. |

## What "RED" looks like in the daily report

The §21 daily honesty report adds a **WNBA player props** panel that
turns red when:

* **30-day Brier > 0.27** — the model is worse than a coin flip.
  Halt the vertical.
* **Hit-rate diverges from implied probability by > 5pp** — the
  calibrator is mis-fit. Pull the next week's tickets while the
  panel is red.
* **Gate trip count for `gate_player_props` > 50/day** — discovery
  is throwing tickets at the gate that are obviously stale or
  rostered-out. Investigate the props poller before re-enabling.

## Manual fire commands

```sh
# Backfill prior season (one-shot, idempotent)
python -m app.scripts.wnba_backfill_stats --season 2025

# Force a single player-props poll cycle (debugging the feed)
python -c "import asyncio; from app.feeds.wnba_player_props import _poll_wnba_player_props; \
  print(asyncio.run(_poll_wnba_player_props()))"

# Inspect today's tradable props (operator preview)
curl -s "$DASHBOARD_BASE_URL/api/dashboard/wnba/props/today" | jq '.props | length'
curl -s "$DASHBOARD_BASE_URL/api/dashboard/wnba/props/today?include_rejected=1" | jq .
```

## Vertical-specific halt

Halt the WNBA player-props vertical **without** touching other
sports:

```sql
INSERT INTO system_settings (key, value)
VALUES (
    'risk.vertical_halted.wnba_player_props',
    json_build_object(
        'by_email', 'ops@example.com',
        'at', now()::text,
        'reason', 'Brier red on daily report'
    )::text
)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
```

`gate_vertical_halted` rejects every WNBA candidate from the next
cycle on. The legacy 12 sports gates and every other vertical keep
trading.

## Re-enable

1. Wait until the calibration panel is green for 4 consecutive
   weeks.
2. Fit a calibrator: `python -m app.calibrators.refit --league WNBA`
3. Drop the raw-mode override:
   ```sql
   DELETE FROM system_settings
    WHERE key = 'risk.uncalibrated_raw_mode_wnba_player_props';
   ```
4. Drop the halt key (if set), then flip the vertical's
   `enabled=True` in `app/verticals/builtins.py` and redeploy.
