# Axiom — Kalshi Connection, Data Flow & Live Display

A complete, ground-truth reference for how Axiom connects to Kalshi, how orders
are submitted and tracked, how fills and positions flow from the exchange into
the database and out to the frontend, and exactly which components display which
Kalshi data.

> This document covers every layer: **Authentication → REST client → WebSocket
> → Order lifecycle → Slippage guard → Fills & reconciliation → DB schemas →
> Backend API endpoints → Frontend components.**

---

## 1. Authentication

**Files:** `app/kalshi/auth.py`, `app/kalshi/client.py`, `app/config.py`

Kalshi uses a **custom RSA-PSS signing scheme** — not Bearer tokens. Every
request (REST and WebSocket upgrade) is signed with an RSA private key.

### 1.1 Credentials (env vars)

| Env var | What it holds |
|---|---|
| `KALSHI_API_KEY_ID` | UUID assigned to your API key in the Kalshi dashboard |
| `KALSHI_API_PRIVATE_KEY` | RSA private key in PEM format |
| `KALSHI_ENV` | `prod` or `demo` (controls which base URL is used) |

Loaded via `app/config.py` → `get_settings()`. At startup,
`auth.validate_credentials()` verifies the key is valid RSA and produces a test
signature to catch misconfiguration before live trading.

### 1.2 Signature construction (every request)

```
message   = timestamp_ms + HTTP_METHOD + path
signature = base64( RSA-PSS-SHA256(private_key, message) )
```

Three headers are attached to every request:

| Header | Value |
|---|---|
| `KALSHI-ACCESS-KEY` | The API Key UUID |
| `KALSHI-ACCESS-TIMESTAMP` | Unix timestamp in **milliseconds** |
| `KALSHI-ACCESS-SIGNATURE` | Base64-encoded RSA-PSS signature |

The timestamp is embedded in the signature, so replay attacks are blocked by the
exchange's timestamp window.

### 1.3 Environment routing

| `KALSHI_ENV` | REST base URL | WebSocket URL |
|---|---|---|
| `prod` | `https://api.elections.kalshi.com/trade-api/v2` | `wss://api.elections.kalshi.com/trade-api/ws/v2` |
| `demo` | `https://demo-api.kalshi.co/trade-api/v2` | `wss://demo-api.kalshi.co/trade-api/ws/v2` |

### 1.4 Cluster probe

A background task (`cluster_probe` in `client.py`) runs every 10 minutes and
verifies that the credentials in use match the configured environment (`prod` vs
`demo`), catching the silent-misconfiguration case where prod keys are loaded
against the demo endpoint or vice versa.

---

## 2. REST client (`app/kalshi/client.py`)

**Class:** `KalshiClient` — async, built on `httpx.AsyncClient`.

The same client instance is reused across the lifetime of the process (httpx
handles internal connection pooling). `aclose()` is called on shutdown.

### 2.1 All REST endpoints used

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/exchange/status` | Check if the exchange is active/accepting orders |
| `GET` | `/portfolio/balance` | Account balance (cents) and available-to-trade |
| `GET` | `/portfolio/positions` | Open positions (supports `position_fp` for fractional) |
| `GET` | `/markets/{ticker}` | Single market detail (title, status, close time, yes/no ask) |
| `GET` | `/markets` | List markets (filterable by status, series, pagination) |
| `GET` | `/series` | List series (used to discover sport/vertical series prefixes) |
| `GET` | `/series/{series}/markets/{ticker}/candlesticks` | Price history for a market |
| `GET` | `/markets/{ticker}/orderbook` | One-shot L2 orderbook snapshot (fallback when WS cache is stale) |
| `POST` | `/portfolio/orders` | **Submit an order** |
| `DELETE` | `/portfolio/orders/{order_id}` | Cancel an order by exchange order ID |
| `GET` | `/portfolio/orders/{order_id}` | Get order status (used in submit-recovery and fill reconciliation) |
| `GET` | `/portfolio/fills` | List recent fills with pagination (used by the reconciler) |

### 2.2 Retry logic

- **GET (idempotent):** up to 4 attempts with backoff via a shared `retry_request` helper.
- **POST / DELETE (mutating):** **single-shot only** — never retried automatically to prevent double-submits or double-cancels. Ambiguous POST failures are handled by `_attempt_submit_recovery` (see §5.4).

### 2.3 Error handling

All non-2xx responses raise `KalshiAPIError(status_code, response_body)`. The
lifecycle layer catches and classifies these into structured statuses (see §5.5).

---

## 3. WebSocket connection (`app/kalshi/ws.py`)

The WebSocket carries **real-time orderbook, fill, and ticker data** and is the
primary data source for live trading decisions (the REST API is used as a
fallback only).

### 3.1 Connection & authentication

The WS upgrade is a standard HTTP GET with the same RSA-PSS headers as REST
(method: `GET`, path: `/trade-api/ws/v2`). The signature is re-computed fresh
on every connection attempt (including reconnects).

### 3.2 Channel subscriptions

After connecting, Axiom sends a subscribe command:

```json
{
  "id": <cmd_id>,
  "cmd": "subscribe",
  "params": {
    "channels": ["fill", "orderbook_snapshot", "orderbook_delta", "ticker"],
    "market_tickers": ["<ticker1>", "<ticker2>", ...]
  }
}
```

| Channel | What it carries |
|---|---|
| `orderbook_snapshot` | Full L2 orderbook replacement for a market |
| `orderbook_delta` | Incremental bid/ask size changes |
| `ticker` | Last-traded price, volume, open interest |
| `fill` | Your account's fill events (matched contracts, price, side) |

### 3.3 Message loop

The loop runs as a background asyncio task (`_run`). Large frames — typically
`orderbook_snapshot` at >16 KB — are parsed via `asyncio.to_thread(json.loads)`
to avoid blocking the event loop. All parsed messages are published to an
**in-process event bus** (`bus`) for downstream consumers (the orderbook cache
consumer and the lifecycle fill handler both subscribe).

### 3.4 Reconnection

Exponential backoff starting at 1 second, capped at 30 seconds. Every reconnect
re-signs the upgrade request. There is no session token to restore — the
subscription list is re-sent after each successful connect.

---

## 4. Orderbook cache (`app/kalshi/orderbook.py`)

The orderbook cache is the bridge between the WS feed and the pre-submit
slippage guard.

### 4.1 Structure

```
_books:      { ticker → { "yes" → { price_cents: size }, "no" → { price_cents: size } } }
_updated_at: { ticker → datetime }
```

### 4.2 Population

`start_orderbook_consumer` subscribes to the event bus:
- `orderbook_snapshot` → **full replace** for the ticker.
- `orderbook_delta` → **incremental update**: positive size = add/update a
  level, zero size = remove a level.

### 4.3 Staleness fallback

If a market's `_updated_at` is more than **5 seconds** old when the slippage
guard runs, it triggers a REST fallback (`GET /markets/{ticker}/orderbook`) to
get a fresh snapshot before evaluating the order.

### 4.4 Slippage guard reads

`_check_slippage_guard` (in `lifecycle.py`):
1. Calls `_ensure_fresh_orderbook` (WS cache or REST fallback).
2. `cache.worst_fill_price` — projects the taker cost for the requested
   quantity by walking the book depth.
3. If `(worst_fill_price - edge_price) / edge > slippage_guard_max_edge_pct`,
   the order is **rejected or sliced** to the fillable depth.
4. If the requested side has **zero resting liquidity** and the order is GTC,
   it is **allowed as a resting limit** (worst case: unfills at the edge-capped
   price — it can't produce a bad fill). FOK on an empty side converts to a
   resting GTC (strips the marketable buffer).

---

## 5. Order lifecycle (`app/kalshi/lifecycle.py`)

Every order — paper and live — flows through `submit_order`. This is the single
gated path to the exchange.

### 5.1 `submit_order` full flow

```
submit_order(req: OrderRequest, force_paper=False)
│
├─ 1. Resolve trading mode (paper vs live)
├─ 2. Kill-switch check (_kill.halted → immediate reject)
├─ 3. Validate count + price bounds
├─ 4. Slippage guard (_check_slippage_guard)
│       ├─ Returns None → pass through
│       ├─ Returns slice → resize count
│       └─ Returns rest_empty_side_as_gtc → rewrite FOK→GTC, strip buffer
├─ 5. In-process dedupe (atomic check vs _resting + _in_flight sets)
├─ 6. Write `submitted` row to order_lifecycle table
│
├─ PAPER PATH → _simulate_paper_fill_and_record (paper_fill_sim)
│
└─ LIVE PATH
        ├─ POST /portfolio/orders with to_kalshi_payload()
        ├─ Success → write `resting` lifecycle row + add to _resting dict
        ├─ Recoverable error (0 / 408 / 429 / 5xx) → _attempt_submit_recovery
        └─ Terminal error → write `rejected` lifecycle row
```

### 5.2 `to_kalshi_payload` — exact fields sent to Kalshi

| Field | Value |
|---|---|
| `ticker` | `market_ticker` |
| `action` | `"buy"` or `"sell"` |
| `side` | `"yes"` or `"no"` |
| `count` | integer (number of contracts) |
| `type` | `"limit"` or `"market"` |
| `client_order_id` | UUID string (our internal idempotency key) |
| `time_in_force` | `"fill_or_kill"` for FOK; omitted for GTC |
| `yes_price` or `no_price` | Limit price in **cents (1–99)**, present only for `limit` orders, keyed on `side` |

> **Critical:** `yes_price` / `no_price` are stamped directly from
> `RiskContext.current_price` (which carries `pick_price` in the bought side's
> coordinates). A NO order built from the YES quote would bid 62¢ for a contract
> that should cost 38¢. See §Edge-side coordinate lesson.

### 5.3 `_resting` dict — in-memory open orders

```python
_resting: dict[str, OrderRecord]
# key = client_order_id

@dataclass
class OrderRecord:
    order_id: str
    request: OrderRequest
    paper: bool
    state: str                  # "resting" | "filled" | "canceled" | ...
    exchange_order_id: str | None
    filled_count: int
    avg_fill_price_cents: int | None
    over_filled: bool
    fill_count_authoritative: bool   # True = we trust filled_count
```

Resting orders are aged out by the **cycle stale-sweep** in `run_cycle`, which
calls `cancel_order` on orders older than `resting_order_max_age_secs`
(default ~30s).

### 5.4 Submit recovery (`_attempt_submit_recovery`)

Triggered when a POST to `/portfolio/orders` returns a transport error or
ambiguous 5xx. The goal: determine if the order actually placed before deciding
to retry.

1. Calls `GET /portfolio/orders?client_order_id=<uuid>`.
2. **Found** → promotes state to `resting`, `filled`, or `canceled` from venue
   `status`.
3. **Not found** → increments `recovery_attempts` counter in the
   `order_recovery_attempts` table. After 5 failed attempts fires an
   `order_submit_stuck` alert.

This is why POST to Kalshi is single-shot: `_attempt_submit_recovery` is the
correct way to handle ambiguity, not an automatic retry that could double-buy.

### 5.5 Error classification (`_classify_exception`)

Maps exchange exceptions to structured `skipped_*` statuses:

| Exception text | Status |
|---|---|
| `market_not_found` / "Market closed" | `skipped_gate` (routine expiry — not a failure) |
| Slippage / thin book | `skipped_slippage` |
| Score below confidence floor | `skipped_low_confidence` |
| No matching Kalshi ticker | `skipped_unresolved` |
| Existing position | `skipped_already_placed` |

---

## 6. Fill flow: exchange → database

### 6.1 End-to-end fill path

```
Kalshi exchange
    │ (WebSocket fill event)
    ▼
app/kalshi/ws.py  → publishes to bus
    │
    ▼
lifecycle.handle_ws_fill(event)
    ├─ Match to OrderRecord in _resting (via exchange_order_id)
    ├─ _record_fill_idempotent(order_id, fill_id, count, price_cents)
    │       └─ INSERT INTO order_fills ... (no-op if (order_id, fill_id) exists)
    ├─ Update rec.filled_count + rec.avg_fill_price_cents (weighted average)
    ├─ _upsert_position_rollup(ticker, side, count, price_cents)
    │       └─ UPDATE positions table
    └─ _insert_live_trade_row()
            └─ INSERT INTO trades (pnl=NULL, settled_at=NULL)

Later, settlement scheduler (~180s tick):
    app/settlement/pipeline.py
        ├─ SELECT trades WHERE settled_at IS NULL
        ├─ Fetch market outcome from Kalshi
        ├─ Compute pnl based on outcome and side
        └─ UPDATE trades SET pnl=..., settled_at=now()
```

### 6.2 Cancel + fill reconciliation

When the stale-sweep (or a manual cancel) calls `cancel_order`:

1. `DELETE /portfolio/orders/{order_id}`.
2. Parse cancel response for `filled_count` via `_extract_venue_filled_count`
   (checks `resp["filled_count"]`, `resp["executed_count"]`, and the nested
   `resp["order"]["filled_count"]`).
3. If silent, does a follow-up `GET /portfolio/orders/{order_id}`.
4. Sets `rec.filled_count = max(in_memory, venue)` and
   `fill_count_authoritative = True`.
5. If still unknown, sets `fill_count_authoritative = False` — **unknown fill
   state must never be treated as a clean zero** (double-bet risk on live money).

### 6.3 Reconciler (`app/kalshi/reconciler.py`)

A supervised background loop that runs `_tick_once()` periodically to keep
local state in sync with exchange truth:

1. **Fill sync** — `client.list_fills(min_ts=cursor)` fetches recent fills.
   `_replay_fill(f)` inserts any fill not already in `trades` (heals missed WS
   events).
2. **Position diff** — `_diff_positions(positions)` compares local `trades`
   rollups against `client.get_positions()`.
3. **Auto-correction** — discrepancies are recorded in
   `position_drift_resolutions`. If configured, it can auto-accept exchange
   truth and raise an alert.

---

## 7. Database schemas

### 7.1 `trades` table

The primary trade ledger. One row per fill event.

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` | Primary key |
| `trade_id` | `text` | Unique; matches `fill_id` for live trades |
| `market_id` | `text` | Kalshi ticker (e.g. `KXNHLSPREAD-26MAY24...`) |
| `market_title` | `text` | Human-readable market title (cached at write time) |
| `vertical` | `text` | e.g. `sports`, `crypto`, `weather` |
| `side` | `text` | `yes` or `no` |
| `action` | `text` | `buy` or `sell` |
| `quantity` | `numeric` | Contracts filled |
| `price` | `numeric` | Fill price in **decimal dollars** (e.g. `0.62`) |
| `fees` | `numeric` | Exchange fees in dollars (0 in most cases) |
| `pnl` | `numeric` | Stamped at settlement (`NULL` until settled) |
| `trading_mode` | `text` | `live` or `paper` |
| `exchange` | `text` | `kalshi` |
| `executed_at` | `timestamptz` | Time of fill |
| `settled_at` | `timestamptz` | Stamped when outcome resolves (`NULL` until) |
| `placed_by` | `text` | `orchestrator`, `edge_terminal`, `manual`, etc. |
| `model_prob` | `numeric` | Model win-probability at placement time |
| `model_edge` | `numeric` | Signed edge at placement time |
| `kelly_fraction` | `numeric` | Kelly fraction used for sizing |
| `metadata` | `jsonb` | Full context blob (raw fill payloads, signal_id, etc.) |

> **`price` is in dollars** (e.g. `0.62`, not `62`). Any frontend display in
> cents must convert: `round(price * 100)`.

### 7.2 `order_fills` table

One row per partial fill. Supports weighted-average fill price calculation.

| Column | Type | Notes |
|---|---|---|
| `order_id` | `text` | Internal `client_order_id` (our UUID) |
| `fill_id` | `text` | Venue fill/trade ID (idempotency key) |
| `count` | `integer` | Contracts in this partial fill |
| `price_cents` | `integer` | Fill price in cents |
| `received_at` | `timestamptz` | Defaults to `now()` |
| `source` | `text` | `ws`, `paper_sim`, `reconciler_replay`, etc. |

Unique on `(order_id, fill_id)` — duplicate fill events are silently ignored.

### 7.3 `order_lifecycle` table

Audit trail for every state transition of every order.

| Column | Type | Notes |
|---|---|---|
| `client_order_id` | `text` | Our UUID |
| `state` | `text` | `submitted`, `resting`, `filled`, `canceled`, `rejected` |
| `exchange_order_id` | `text` | Kalshi's order ID (NULL until accepted) |
| `recorded_at` | `timestamptz` | |
| `metadata` | `jsonb` | Request payload, error body, etc. |

### 7.4 `positions` table (rollup)

A running exposure summary by market for fast position queries.

| Column | Type | Notes |
|---|---|---|
| `market_ticker` | `text` | PK |
| `net_position` | `integer` | Net contracts (+ = long YES, − = long NO) |
| `cost_basis_cents` | `integer` | Total cost of open position |
| `last_updated_at` | `timestamptz` | |

### 7.5 `position_drift_resolutions` table

Records discrepancies caught by the reconciler between local rollup and exchange
truth.

---

## 8. Backend API endpoints (Kalshi data → frontend)

All served from `artifacts/api-server`. Routers are registered in `app/main.py`
and handler logic lives in `app/dashboard.py`, `app/kalshi/`, etc.

### 8.1 REST endpoints

#### `GET /api/kalshi/positions`
Live open positions directly from `client.get_positions()`, enriched with market
titles from `market_title_cache` and current mid-prices.

```json
{
  "items": [
    {
      "market_ticker": "KXNHLSPREAD-26MAY24BOSCOL-BOS2",
      "market_title": "Bruins -1.5 vs. Colorado",
      "bet_type": "YES",
      "position": 10,
      "market_exposure_cents": 420,
      "realized_pnl_cents": 0,
      "fees_paid_cents": 0,
      "current_value_cents": 510,
      "entry_price_cents": 42,
      "last_price_cents": 51
    }
  ],
  "count": 1
}
```

#### `GET /api/dashboard/recent-trades`
Last N trades from the `trades` table (includes both paper and live, by mode
filter).

```json
{
  "items": [
    {
      "id": 1234,
      "trade_id": "fill_abc123",
      "market_id": "KXNHLSPREAD-26MAY24BOSCOL-BOS2",
      "market_title": "Bruins -1.5 vs. Colorado",
      "vertical": "sports",
      "league": "nhl",
      "side": "yes",
      "action": "buy",
      "quantity": 10,
      "price": 0.42,
      "fees": 0.0,
      "pnl": null,
      "trading_mode": "live",
      "exchange": "kalshi",
      "placed_by": "edge_terminal",
      "executed_at": "2026-06-10T14:32:00Z"
    }
  ],
  "tier": "standard",
  "delayed": false
}
```

#### `GET /api/public/equity-curve`
Daily-bucketed cumulative P&L (`date_trunc('day', ...)` on `trades.executed_at`).

```json
{
  "days": 30,
  "points": [
    { "date": "2026-06-09", "pnl": 12.40, "cumulative_pnl": 187.30 },
    { "date": "2026-06-10", "pnl": 8.10,  "cumulative_pnl": 195.40 }
  ],
  "placed_by": null,
  "verticals": ["sports", "weather"]
}
```

#### `GET /api/kalshi/balance`
```json
{ "balance_cents": 250000, "available_to_trade_cents": 230000 }
```

#### `GET /api/kalshi/markets`
Proxy to `client.list_markets()` — returns the raw Kalshi market list (tickers,
titles, status, yes/no ask prices, close times).

#### `GET /api/kalshi/orders/resting`
Currently open/resting orders from the Kalshi API.

#### `GET /api/dashboard/top-edges`
Candidate predictions with the highest current edge, including Kalshi
tickers, model probability, market probability, and edge value.

#### `GET /api/dashboard/bankroll/recent`
Recent bankroll event series: `current_balance_cents`, `total_equity_cents`,
and a sparkline of balance events plus a `last_tick_at` for the sync-health
badge.

### 8.2 WebSocket endpoints

#### `/api/ws/pipeline`
Real-time orchestrator cycle events.

| Event | Payload fields |
|---|---|
| `cycle_start` | `cycle_id`, `vertical`, `timestamp` |
| `gate_pass` / `gate_fail` | `cycle_id`, `candidate`, `gate_name`, `reason` |
| `candidate_complete` | `cycle_id`, `ticker`, `placed: bool`, `order_id`, `side`, `price`, `quantity` |
| `cycle_complete` | `cycle_id`, `vertical`, `placed_count`, `skipped_count` |

#### `/api/ws/settlement`
Trade resolution events.

| Event | Payload fields |
|---|---|
| `trade_settled` | `trade_id`, `market_id`, `pnl`, `outcome` |
| `market_settle_complete` | `market_id`, `market_title`, `settled_count` |
| `backfill_progress` | `batch`, `total` |

#### `/api/ws/positions`
Filtered event stream: `candidate_complete` (where `placed=true`) and
`trade_settled` — events that directly change inventory or cost basis.

#### `/api/ws/orderbook/{ticker}`
Real-time L2 orderbook for a specific market (mirrors the WS cache).

```json
{
  "type": "orderbook_delta",
  "market_ticker": "KXNHLSPREAD-26MAY24BOSCOL-BOS2",
  "bids": [[42, 150], [41, 300]],
  "asks": [[43, 100], [44, 200]]
}
```

#### `/api/ws/kalshi`
Pass-through for all events from the internal Kalshi event bus (all market data
and account events).

---

## 9. Frontend components (how Kalshi data is displayed)

All in `artifacts/web/src/`.

### 9.1 Live positions — `ActivePositions`

**File:** `src/components/dashboard/active-positions.tsx`
**Data source:** polls `GET /api/kalshi/positions?limit=10` every **30 seconds**
(`refetchInterval: 30_000`)

| Field displayed | Source column |
|---|---|
| Market title | `market_title` (falls back to `market_ticker`) |
| Ticker | `market_ticker` |
| Side | `bet_type` — YES (green), NO (red) |
| Quantity | `position` (absolute value) |
| Entry cost | `market_exposure_cents` |
| Current value | `current_value_cents` |
| Realized P&L | `realized_pnl_cents` |
| Unrealized P&L | `current_value_cents - market_exposure_cents` |

Position count also surfaces in the **KPI summary bar**
(`kpi-summary-bar.tsx`) as "OPEN POSITIONS" from `GET /api/dashboard/overview`.

### 9.2 Recent trades / fills — `RecentTrades`

**File:** `src/components/telemetry/recent-trades.tsx`
**Data source:** `useGetRecentTrades` polls `GET /api/dashboard/recent-trades`
every **60 seconds**, and also **force-refetches** when a `trade_settled` or
`cycle_complete` WebSocket event fires.

| Field displayed | Source |
|---|---|
| Time | `executed_at` |
| Market | `market_title` / `market_id` |
| Exchange | `exchange` (badge: Kalshi / Polymarket) |
| Side / Action | `side` / `action` (e.g. YES BUY) |
| Qty | `quantity` |
| Price | `price` (stored dollars, displayed as cents) |
| P&L | `pnl` (null until settled) |

### 9.3 Equity curve — `EquityCharts`

**File:** `src/components/dashboard/equity-charts.tsx`
**Data source:** `GET /api/public/equity-curve`
**Shape:** array of `{ date, pnl, cumulative_pnl }` daily points.
**Timeframe:** controlled by `usePeriod` hook (7d / 30d / all-time toggle).

Draws cumulative P&L as a time-series line chart. No real-time WS — polls on
focus and on a timer.

### 9.4 Bankroll panel — `BankrollPanel`

**File:** `src/components/dashboard/bankroll-panel.tsx`
**Data source:** `useGetRecentBankrollEvents` → `GET /api/dashboard/bankroll/recent`
**Cadence:** polls every **30 seconds**.

Displays:
- **Live Kalshi balance** (`current_balance_cents`)
- **Total equity** (`total_equity_cents`)
- **24h sparkline** of balance events
- **SYNC badge** (FRESH / STALE / STOPPED) based on `last_tick_at` from the
  backend bankroll-sync loop — this is the inline health indicator for whether
  the system is current with the exchange.

### 9.5 WebSocket provider and hooks

**File:** `src/components/realtime/websocket-provider.tsx`

`WebSocketProvider` wraps the entire app and maintains two persistent WS
connections:

| Connection | Endpoint | What it drives |
|---|---|---|
| Pipeline | `/api/ws/pipeline` | `usePipelineSubscription` → invalidates React Query caches on `candidate_complete`, `cycle_complete` |
| Settlement | `/api/ws/settlement` | `RecentTrades` refetches on `trade_settled`, `market_settle_complete` |

**File:** `src/hooks/use-websocket.ts`
`useWebSocket` handles the underlying connection, exponential backoff
reconnection, and heartbeat keep-alive for both streams.

### 9.6 Stale data badges

`TileStaleBadge` and `useTileStaleClass` — used across dashboard components to
visually dim or flag data that hasn't updated within an expected window. Surfaces
staleness without hiding the last-known value.

### 9.7 KPI summary bar — `KpiSummaryBar`

**File:** `src/components/dashboard/kpi-summary-bar.tsx`
**Data source:** `/api/dashboard/overview`

Aggregate Kalshi metrics shown at the top of the Overview tab:

| KPI | Meaning |
|---|---|
| P&L TODAY | Realized PnL for the current UTC day (`settled_at` date) |
| WIN RATE | All-time win rate across settled trades |
| ROI | Period-based return on capital |
| VOLUME | Total amount staked (handle) |
| OPEN POSITIONS | Count of live open positions on Kalshi |
| SHARPE | Trade-weighted Sharpe ratio |

---

## 10. The full data path, end-to-end

```
Kalshi Exchange
        │
        ├── WebSocket ──────────────────────────────────────────────────────────┐
        │   (fill, orderbook_snapshot, orderbook_delta, ticker)                 │
        │                                                                       ▼
        │                                                           app/kalshi/ws.py
        │                                                               │
        │                                              ┌────────────────┴────────────────┐
        │                                              ▼                                 ▼
        │                                   orderbook cache                     lifecycle.handle_ws_fill
        │                                   (app/kalshi/orderbook.py)                   │
        │                                   Used by slippage guard                      ├── INSERT order_fills
        │                                                                               ├── UPDATE positions
        │                                                                               └── INSERT trades (pnl=NULL)
        │
        ├── REST calls ──── client.py ──── lifecycle.submit_order ────────────────────────────────┐
        │                                   (POST /portfolio/orders)                              │
        │                                                                                         │
        └── Reconciler loop ────────────────────────────────────────────────────────────────────┘
            (list_fills → replay missed fills; get_positions → diff + heal)

Postgres (Supabase)
        │
        ├── trades table ──────────────────────────────────────────────────────────────────────────
        │         ↑ written by lifecycle (fills) and paper_fill_sim (paper)
        │         ↑ updated at settlement (pnl, settled_at)
        │
        └── positions / order_fills / order_lifecycle / position_drift_resolutions

FastAPI Endpoints (api-server)
        │
        ├── GET /api/kalshi/positions ─────────────── ActivePositions (30s poll)
        ├── GET /api/dashboard/recent-trades ──────── RecentTrades (60s poll + WS invalidation)
        ├── GET /api/public/equity-curve ──────────── EquityCharts (periodic)
        ├── GET /api/dashboard/bankroll/recent ────── BankrollPanel (30s poll)
        ├── GET /api/dashboard/overview ───────────── KpiSummaryBar
        ├── WS  /api/ws/pipeline ──────────────────── WebSocketProvider → React Query invalidation
        └── WS  /api/ws/settlement ─────────────────── RecentTrades force-refetch

React Frontend (artifacts/web)
        │
        ├── ActivePositions     — live open positions, P&L per market
        ├── RecentTrades        — fill history with settlement P&L
        ├── EquityCharts        — daily cumulative P&L curve
        ├── BankrollPanel       — balance, equity, sync health
        ├── KpiSummaryBar       — aggregate Sharpe, win rate, ROI, volume
        └── Pipeline page       — real-time cycle/gate/placement event stream
```

---

## 11. Operational facts worth knowing

- **`trades.price` is stored in decimal dollars** (e.g. `0.62`). Every
  frontend component that displays cents must convert: `round(price * 100)`.
  The daily email renderer learned this the hard way.

- **Open positions are sourced directly from Kalshi** (`GET /portfolio/positions`)
  for the `ActivePositions` component — this is exchange truth. The local
  `positions` rollup table is the internal tracking layer; the reconciler
  (`reconciler.py`) heals drift between the two.

- **Fills can arrive out of order** (WS events can lag; the reconciler's fill
  replay catches them). The `(order_id, fill_id)` unique index in `order_fills`
  makes duplicate inserts a silent no-op.

- **Paper mode and live mode write to the same `trades` table**, differentiated
  by `trading_mode = 'paper'`. All frontend endpoints accept a mode filter.
  The paper fill simulator (`paper_fill_sim.py`) synthesizes fills from the
  orderbook cache for realistic paper-mode behavior.

- **The bankroll sync badge** (`SYNC: FRESH / STALE / STOPPED`) in
  `BankrollPanel` is the fastest visual indicator of Kalshi connectivity health
  on the frontend. If it goes STALE, the WS or REST connection to Kalshi has
  likely degraded.

- **The api-server has no hot reload.** After any Python edit, the workflow must
  be restarted before the running server reflects the change. A standalone import
  test proves the file; only a restart proves the server.
