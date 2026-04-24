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

## Hosted log search (request_id traces)

Every log line is already JSON with a `request_id` field on stdout (see
`app/observability.py`). To make those traces searchable from a saved
query — paste a `request_id`, get the full top-to-bottom trace — set the
shipper env vars and the API will forward each line to a hosted backend
in batches via a non-blocking background thread. With no env vars set,
no shipper is installed and no network calls happen.

| Var | Default | Purpose |
| --- | --- | --- |
| `AXIOM_LOG_SHIPPER_URL` | unset | Intake URL. Setting it enables the shipper. |
| `AXIOM_LOG_SHIPPER_TOKEN` | unset | Sent as `Authorization: Bearer <token>`. |
| `AXIOM_LOG_SHIPPER_PROVIDER` | `betterstack` | `betterstack` (JSON array) or `loki` (Grafana Loki push). |
| `AXIOM_LOG_SHIPPER_BATCH_SIZE` | `100` | Max records per HTTP POST. |
| `AXIOM_LOG_SHIPPER_FLUSH_MS` | `2000` | Max ms a record waits before being flushed. |
| `AXIOM_LOG_SHIPPER_QUEUE_MAX` | `10000` | In-process queue cap; overflow drops the oldest line. |
| `AXIOM_LOG_SHIPPER_LABELS` | unset | `k=v,k=v` merged into every record (e.g. `service=axiom-api,env=prod`). |
| `AXIOM_LOG_SHIPPER_LEVEL` | `INFO` | Per-shipper minimum level. |

### Better Stack / Logtail

```
AXIOM_LOG_SHIPPER_URL=https://in.logs.betterstack.com
AXIOM_LOG_SHIPPER_TOKEN=<source-token>
AXIOM_LOG_SHIPPER_LABELS=service=axiom-api,env=prod
```

In Better Stack, save a query

```
json.request_id = "<paste id here>"
```

and pin it to your dashboard. Sort ascending by `dt` to read the trace
top-to-bottom.

### Grafana Loki

```
AXIOM_LOG_SHIPPER_URL=https://logs-prod-xxx.grafana.net/loki/api/v1/push
AXIOM_LOG_SHIPPER_TOKEN=<api-token>
AXIOM_LOG_SHIPPER_PROVIDER=loki
AXIOM_LOG_SHIPPER_LABELS=service=axiom-api,env=prod
```

Loki streams are keyed by `request_id` so the saved query

```logql
{service="axiom-api", request_id="<paste id here>"} | json
```

returns one stream per trace. Pin it as a dashboard panel for the
10-second support workflow.

### Operator visibility

`/api/health` includes a `log_shipper` block with `enabled`, `provider`,
`sent`, `dropped_queue_full`, `failed_batches`, `last_error`, and
`last_send_ts` so a misconfigured intake (bad token, dead host,
saturated queue) is loud on the existing health dashboard instead of
silently dropping traces.

### Deployment

In Replit Deployments, set the env vars above on the deployed app
(Deployments tab → Secrets). The shipper auto-installs at startup
the next time the deployment boots; no code change is needed when
rotating the token or pointing at a different intake.

## Manifest signing key rotation

The model manifest published to `<bucket>/exports/models_manifest.json`
is signed with an offline ed25519 keypair (task #73). The matching
public key is configured on the API as `AXIOM_MANIFEST_PUBLIC_KEY` and
verified on every boot / `/api/models/reload` by
`app/models_loader.py`. To rotate the keypair (scheduled rotation or
suspected compromise) without a readiness gap, follow
[`docs/manifest_signing_runbook.md`](docs/manifest_signing_runbook.md).

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

## Private GitHub recovery mirror

Every `/api/models/reload` (i.e. every model promotion) pushes the loaded
artifacts to a private GitHub repository so we can rebuild the predictor
catalog if Supabase Storage is unavailable. The mirror is **best-effort**
and silently no-ops when the env vars below are missing, so it must be
provisioned explicitly before a recovery is needed.

### Configuration

| Var | Required | Default | Purpose |
| --- | --- | --- | --- |
| `GITHUB_MIRROR_REPO` | yes | — | `owner/repo` slug of the private mirror. |
| `GITHUB_TOKEN` | yes | — | Fine-grained or classic PAT with `repo` scope on the mirror repo. |
| `GITHUB_MIRROR_BRANCH` | no | `main` | Branch that receives the mirror commits. |
| `GITHUB_API_BASE` | no | `https://api.github.com` | Override for GitHub Enterprise. |

If either of `GITHUB_MIRROR_REPO` / `GITHUB_TOKEN` is unset, every push
short-circuits with `skipped=true, reason="GITHUB_MIRROR_REPO and/or
GITHUB_TOKEN not configured"` and `GET /api/admin/models/mirror/status`
returns `enabled=false`. **Mirroring will not run** until both are set.

The mirror status and force-push endpoints are admin-only and live
under `/api/admin/models/mirror/...` (not `/api/models/mirror/...`);
the operator account hitting them must satisfy `require_admin`.

### One-time setup

1. **Create the private repo.** On GitHub, create an empty private repo
   (e.g. `your-org/axiom-models-mirror`) and initialize it with a
   default branch named `main` (or whatever you intend to set
   `GITHUB_MIRROR_BRANCH` to). The branch must already exist — the
   mirror writes to it via the Contents API and does not create
   branches.
2. **Mint a Personal Access Token.** Either:
   - **Fine-grained PAT** (recommended): scope it to *only* the mirror
     repo, with `Contents: Read and write` and `Metadata: Read` repo
     permissions. Set an expiry and put a calendar reminder to rotate.
   - **Classic PAT**: grant the `repo` scope. Classic tokens have access
     to every repo the user can see, so prefer the fine-grained option.
3. **Set the secrets** on the API server. In the Replit workspace, open
   the Secrets pane (or use `replit secrets set` in deployment) and add
   `GITHUB_MIRROR_REPO`, `GITHUB_TOKEN`, and optionally
   `GITHUB_MIRROR_BRANCH`. Restart the `artifacts/api-server: API Server`
   workflow so the new env is picked up.
4. **Verify the wiring.** As an admin user:
   ```bash
   curl -sS -H "X-API-Token: $AXIOM_API_TOKEN" \
     "$AXIOM_API_BASE/api/admin/models/mirror/status"
   ```
   You should see `{"enabled": true, "last_result": null}` (or the
   previous run's result). Then force a push:
   ```bash
   curl -sS -X POST -H "X-API-Token: $AXIOM_API_TOKEN" \
     "$AXIOM_API_BASE/api/admin/models/mirror/push"
   ```
   A successful response is `{"ok": true, "skipped": false, "version":
   "<v>", "files_pushed": ["models/<v>/models_manifest.json", ...,
   "models/latest.json"], ...}`. Confirm the files appear in the mirror
   repo on GitHub.

   Common failure modes:
   - `400 github mirror is not configured` — env vars are missing or the
     workflow wasn't restarted.
   - `ok=false, reason="github_mirror PUT ... failed: 404 ..."` — the
     repo slug is wrong or the token can't see it.
   - `ok=false, reason="...: 422 ... Branch ... not found"` — the
     branch in `GITHUB_MIRROR_BRANCH` doesn't exist on the mirror repo.
   - `ok=false, reason="...: 401 Bad credentials"` — token is invalid
     or expired; mint a new one and re-set `GITHUB_TOKEN`.

### Layout in the mirror repo

```
models/<version>/models_manifest.json
models/<version>/models.tar
models/<version>/source.tar
models/latest.json   # { "version": "<v>", "promoted_at": "...", "files": [...] }
```

`models/latest.json` is a pointer to the most recent promoted version so
recovery doesn't need to enumerate the tree. Older versions are kept
indefinitely — the mirror never deletes.

### Recovering when Supabase Storage is down

If `/api/health` reports Storage unavailable and the API server cannot
download the model artifacts, you can rebuild the local cache from the
mirror by hand. The cache directory is whatever `resolve_model_cache_dir()`
returns (`app/models_loader.py`); typically a writable path on the
server's filesystem.

1. Read the latest pointer to find the active version and capture it
   into `$VERSION`:
   ```bash
   VERSION=$(curl -sS -H "Authorization: Bearer $GITHUB_TOKEN" \
     -H "Accept: application/vnd.github.raw" \
     "https://api.github.com/repos/$GITHUB_MIRROR_REPO/contents/models/latest.json?ref=${GITHUB_MIRROR_BRANCH:-main}" \
     | python -c "import json,sys; print(json.load(sys.stdin)['version'])")
   echo "recovering version: $VERSION"
   ```
2. Download the three artifact files into the model cache directory
   (overwriting whatever is there). The cache path is whatever
   `app.models_loader.resolve_model_cache_dir()` returns on this
   host — print it with
   `python -c "from app.models_loader import CACHE_ROOT; print(CACHE_ROOT)"`
   from the api-server working directory so you don't have to guess.
   Then, using the raw Contents API:
   ```bash
   CACHE=/path/printed/above
   for f in models_manifest.json models.tar source.tar; do
     curl -sSL -H "Authorization: Bearer $GITHUB_TOKEN" \
       -H "Accept: application/vnd.github.raw" \
       "https://api.github.com/repos/$GITHUB_MIRROR_REPO/contents/models/$VERSION/$f?ref=${GITHUB_MIRROR_BRANCH:-main}" \
       -o "$CACHE/$f"
   done
   ```
   (For files larger than 100 MiB the Contents API rejects raw
   downloads — clone the repo with `git clone --depth=1` instead and
   copy the files out of `models/<v>/`.)
3. Restart the API server. On boot, `models_loader.ensure_artifacts()`
   sees the manifest + archives already on disk, validates the sha256s
   against the manifest, extracts them, and the predictor catalog comes
   back online without ever touching Supabase Storage.
4. Once Supabase Storage is healthy again, call
   `POST /api/admin/models/mirror/push` to confirm round-trip parity
   (it will short-circuit with `skipped=true` if the version is
   unchanged) and `POST /api/models/reload` to re-sync from Supabase as
   the source of truth.
