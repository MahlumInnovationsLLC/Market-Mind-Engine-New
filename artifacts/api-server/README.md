# Axiom-Predict API Server

FastAPI backend for the Axiom-Predict prediction-market trading platform.
Talks to Supabase Postgres + Storage and exposes:

- `/api/healthz`, `/api/health` — liveness and full readiness (DB, Storage, Kalshi).
- `/api/dashboard/*` — read-only telemetry against the production schema.
- `/api/storage/*`, `/api/models/*` — Supabase Storage browsing + artifact inventory.
- `/api/kalshi/*` — Kalshi exchange client + order lifecycle (see below).
- `WS /ws/kalshi` — in-process event bus for orderbook / fill / lifecycle events.

## Run

```bash
pnpm --filter @workspace/api-server dev
```

## Kalshi exchange client

Required environment:

| Var | Purpose |
| --- | --- |
| `KALSHI_API_KEY_ID` | Operator API key id (UUID). |
| `KALSHI_API_PRIVATE_KEY` | RSA private key in PEM (literal `\n` in Replit Secrets is tolerated). |
| `KALSHI_ENV` | `demo` (default) or `prod`. Selects REST + WS hosts. |
| `KALSHI_API_BASE_URL` / `KALSHI_WS_URL` | Optional overrides. |
| `AXIOM_TRADING_MODE` | `paper` (default) or `live`. |
| `AXIOM_API_TOKEN` | Bearer / `X-API-Token` header for protected routes. |

### Paper mode is the default

`AXIOM_TRADING_MODE=paper` is the default and means **every** order is
intercepted before reaching Kalshi: the lifecycle records `submitted` and
`filled` rows in `order_lifecycle` with `metadata.paper=true`, and broadcasts
the events on the in-process bus. No bytes ever hit the wire.

Flipping `AXIOM_TRADING_MODE=live` enables the real Kalshi REST submission
path, **but the trading orchestrator is not wired to use it yet** — that gate
ships with the 12-gate risk system task. Until then, live mode only fires
when an operator explicitly calls the lifecycle (e.g. via a script with
`force_paper=False`).

### Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| `GET`  | `/api/kalshi/balance` | Operator portfolio balance (cents + dollars). |
| `GET`  | `/api/kalshi/positions` | Open positions with exposure / pnl / fees. |
| `GET`  | `/api/kalshi/markets` | Pass-through market listing. |
| `GET`  | `/api/kalshi/orders/resting` | In-process resting-order book. |
| `POST` | `/api/kalshi/paper-order` | Always-paper order submission via the lifecycle. |
| `POST` | `/api/kalshi/halt` | **Kill switch — no auth deps.** Stops all new submissions. |
| `POST` | `/api/kalshi/resume` | Reset the kill switch (operator). |
| `GET`  | `/api/kalshi/kill-switch` | Current kill-switch state. |
| `WS`   | `/ws/kalshi?token=…` | Streams every `bus` message as JSON. |

### Submit a $1 paper order

```bash
cd artifacts/api-server
python scripts/submit_paper_order.py KXBTC15M-26APR201715-15 --side yes --price 50
```

Writes a `submitted` then `filled` row to `order_lifecycle`, publishes both
events on the bus, and prints the resulting record. Kalshi is never called.

### Kill switch

`POST /api/kalshi/halt` flips an in-process flag that:

1. Rejects every new `submit_order` call with `OrderRejected("kill switch active…")`.
2. Records the rejection in `order_lifecycle` with `metadata.reason="kill_switch"`.
3. Is intentionally exposed without `require_api_token` so an operator can hit
   it even when downstream auth is broken.

Resume with `POST /api/kalshi/resume` (token-gated).

## Seed team Elo ratings from 5 years of history

`app.elo` ships with `seed_from_history()` which replays a chronological
stream of finished games into `team_elo` / `team_elo_history`. Until that
runs, every team starts at the default 1500 and `get_team_features()`
returns identical numbers for both sides — i.e. the Elo signal is noise.

`scripts/seed_team_elo.py` is the one-shot loader. It accepts any
combination of input sources, de-duplicates them by `(league, game_id)`,
sorts chronologically, and replays through `seed_from_history`. Re-runs
are safe — duplicates are short-circuited by the unique index on
`team_elo_history`.

```bash
cd artifacts/api-server

# Primary path: 5 years of operator-supplied results
python scripts/seed_team_elo.py --csv historical_games.csv

# Top-up from finished rows already cached locally in `games`
python scripts/seed_team_elo.py --from-db

# Top-up from The Odds API /scores (free tier returns up to 3 days;
# paid plans extend the window — pass any --days value)
python scripts/seed_team_elo.py --from-the-odds-api --days 3
```

CSV columns (header required):
`league,home_team,away_team,home_score,away_score,game_id,played_at`.
Use league codes that match the K-factor table (`NBA`, `NFL`, `NHL`,
`MLB`, `NCAAB`, `MLS`, `EPL`, `LALIGA`, `SERIEA`, `BUNDES`, `LIGUE1`,
`UCL`, or generic `Soccer`).

After the script exits, `team_elo` and `team_elo_history` are populated
and `app.elo.get_team_features()` returns non-default ratings.

### Recurring top-up

`app/feeds/team_elo_seed.py` registers a `team_elo_seed` feed in the
scheduler that replays newly-finished rows from the local `games` table
into the same Elo tables. It's disabled by default — opt in with:

| Var | Default | Purpose |
| --- | --- | --- |
| `AXIOM_TEAM_ELO_AUTOSEED` | unset | Set to `1` to enable the periodic top-up. |
| `AXIOM_TEAM_ELO_AUTOSEED_CADENCE` | `86400` | Seconds between runs (min 60). |
| `AXIOM_TEAM_ELO_AUTOSEED_HOURS` | `48` | Look-back window for `games.scheduled_start`. |

When disabled the feed still appears in `/api/feeds/status` with
`skipped_reason="AXIOM_TEAM_ELO_AUTOSEED not set"` so operators can see
the gap.

## Database migrations (Alembic)

All schema is owned by Alembic revisions under `migrations/versions/`.
The FastAPI lifespan runs `alembic upgrade head` on startup (see
`app/migrations.py`) before any request is served, so request-time
`CREATE TABLE` / `ALTER TABLE` patterns are not allowed.

To add a new schema change:

1. Create a new revision file `migrations/versions/NNNN_<short_slug>.py`,
   numbered after the current head (`alembic heads` to check).
2. Set `revision = "NNNN_<slug>"` and `down_revision = "<previous head>"`.
3. Put the DDL in an `upgrade()` function — prefer `op.execute(...)` with
   plain SQL using `IF (NOT) EXISTS` guards so the migration is safe to
   re-run against environments where part of the schema already exists.
4. Leave `downgrade()` raising `NotImplementedError` unless the change
   is intentionally reversible (matches the project policy on
   destructive down-migrations).
5. Run `pytest` — the migration will be applied to the test DB on the
   next boot of the app.

Do **not** introduce new `ensure_schema` / `ensure_table` helpers in the
app code; the lazy-DDL pattern was retired in task #134.
