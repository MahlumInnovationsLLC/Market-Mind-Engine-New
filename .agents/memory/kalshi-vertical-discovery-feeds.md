---
name: Kalshi vertical discovery feeds
description: Why each trading vertical needs its own Kalshi market-discovery feed or it silently never trades.
---

# Each Kalshi vertical needs a market-discovery feed

The orchestrator cycle selects candidates by reading `predictions` rows
filtered by `league` (scoped per vertical). A vertical with fully-wired
forecast feeds + predictor models will still trade **nothing** if no feed
upserts `predictions` rows for it — `_candidate_predictions(vertical)`
just returns zero rows, silently.

**Why:** weather had GRU/EMOS predictors and forecast feeds but no
discovery feed, so it never produced a single candidate. Crypto has
`kalshi_crypto_markets`, sports has `kalshi_sports_markets`; weather now
has `kalshi_weather_markets`.

**How to apply:** when adding/onboarding a new vertical, confirm a feed
exists that polls open Kalshi markets and upserts `predictions` rows with
the right `league` and `kalshi_market_id=ticker`. The predictor re-parses
the ticker from `kalshi_market_id`, so the row only needs
ticker + price hint + close_time. Mirror an existing discovery feed.

## Two easy footguns in the upsert
- `predictions.game_time` is `timestamp WITHOUT time zone`; asyncpg
  refuses tz-aware datetimes. Normalize Kalshi's tz-aware close time to
  naive UTC at the bind boundary.
- `predictions` has `metadata` (jsonb) but NO `game_context` / `updated_at`
  columns — a stray column ref sinks every row inside the per-row
  `except`, manifesting as zero candidates. Copy the working feed's
  INSERT column list exactly.
