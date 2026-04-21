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
