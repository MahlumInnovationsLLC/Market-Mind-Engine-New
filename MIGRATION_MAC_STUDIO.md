# Mac Studio Migration Runbook

A linear, copy-pasteable cutover from a clean Mac Studio to a paper-trading
Axiom-Predict deployment. Every step is mandatory, in this order — the
preflight gate at the bottom (`scripts/preflight_mac_studio.sh`) refuses
to advance if any of the four prerequisite checks fail.

> **Why this lives in the repo (not chat history):** every cutover after
> the first must be reproducible from a fresh clone. Treat this document
> as the source of truth — if the steps drift from reality, fix the doc
> in the same PR that introduces the drift.

---

## Hardware appendix

**Recommended baseline:** Mac Studio M3 Ultra · 96 GB unified memory ·
1 TB SSD · 10 GbE.

| Reason | Detail |
|---|---|
| 80-core GPU + 32-core Neural Engine | runs `llama3.3:70b` at ~25 tok/s |
| 96 GB unified memory | holds 2-3 LLMs warm simultaneously |
| 1 TB SSD | model weights + Postgres mirror + 30 days of logs |
| 10 GbE | network never bottlenecks Cloudflare tunnel uplink |

**Stretch:** the 192 GB RAM build keeps `qwen2.5:32b`, `llama3.3:70b`,
and `deepseek-v2:16b` warm so persona dispatch never pays a cold-load
penalty.

---

## Cost-comparison appendix

| Component | Replit-hosted | Mac Studio (one-time + amortized) |
|---|---|---|
| FastAPI server | $25/mo (Reserved VM) | $0/mo (electricity ~$8/mo) |
| Hosted LLM (OpenAI + Anthropic) | $80-200/mo @ current swarm cadence | $0/mo (`AXIOM_LLM_MODE=local`) |
| Cloudflare tunnel | free tier | free tier |
| Mac Studio M3 Ultra 96 GB | — | $4,999 one-time (~$140/mo over 36 months) |
| **Total monthly steady-state** | **~$105–225/mo** | **~$148/mo** |

The Mac Studio breaks even on month ~24 against the Replit + hosted-LLM
path while delivering full local LLM control (no per-token spend ceiling
on the swarm and no provider rate limits during a busy NFL Sunday).

---

## Step 1 — Bootstrap the Mac

```bash
# Required tools
brew install ollama postgresql@16 cloudflared pnpm uv git

# Python 3.12 (the api-server pins to >=3.12)
brew install python@3.12

# Optional — useful for live Postgres tunneling diagnostics
brew install pgbouncer
```

## Step 2 — Pull the local LLMs

The local-LLM mode landed in task #735 (`AXIOM_LLM_MODE=local|hybrid|cloud`).
Pull every model the persona resolver might dispatch to:

```bash
# Required (~80 GB)
ollama pull qwen2.5:14b      #  9 GB — fast persona, replaces gpt-4o-mini
ollama pull llama3.3:70b     # 43 GB — reasoning, replaces claude-sonnet-4-5
ollama pull deepseek-v2:16b  # 9.4 GB — backup

# Optional capacity
ollama pull mistral-small:24b   # 14 GB — alt. reasoning
ollama pull qwen2.5:32b         # 19 GB — bigger fast persona

# Verify
ollama list
curl -s http://localhost:11434/api/tags | jq '.models[].name'
```

## Step 3 — Clone + dependency sync

```bash
cd ~
git clone https://github.com/MahlumInnovationsLLC/Market-Mind-Engine.git axiom
cd axiom

# Web + mobile deps
pnpm install

# FastAPI deps (uv is canonical on Mac Studio — Replit uses .pythonlibs)
cd artifacts/api-server
uv sync

# Sanity check the heavy ML deps actually installed
uv pip list | grep -E "lightgbm|xgboost|scikit-learn|asyncpg|fastapi"
```

## Step 4 — `.env` template

Create `~/axiom/artifacts/api-server/.env`. Only the **REQUIRED** rows
must be filled before preflight; everything below the first divider can
land later.

```bash
# === REQUIRED — Supabase (data plane) ===
SUPABASE_PROJECT_REF=frwhxftngijwcujqhltf
SUPABASE_DB_PASSWORD=<from-supabase-dashboard-url-encoded>
SUPABASE_SERVICE_ROLE_KEY=<service-role-jwt>

# === REQUIRED — LLM mode ===
# Task #735's switch. Mac Studio defaults to local; flip to "hybrid" if
# you want the Ollama persona for fast/reasoning slots but keep the
# moderator + gatekeeper on a hosted model for cold-start reliability.
AXIOM_LLM_MODE=local      # cloud | local | hybrid

# === REQUIRED — Ollama ===
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_REQUEST_TIMEOUT_S=120
# These three control which local model fills each persona slot.
OLLAMA_MODEL_FAST=qwen2.5:14b
OLLAMA_MODEL_REASONING=llama3.3:70b
OLLAMA_MODEL_BACKUP=deepseek-v2:16b

# === REQUIRED — Auth (Clerk) ===
CLERK_PUBLISHABLE_KEY=pk_live_...
CLERK_SECRET_KEY=sk_live_...

# === REQUIRED — Trading venue (Kalshi) ===
KALSHI_API_KEY_ID=<...>
KALSHI_API_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"

# === REQUIRED — Trading mode ===
# Stays on "paper" for the entire cutover. The bottom of this runbook
# gates the flip to "live" behind a 100-cycle paper soak.
AXIOM_TRADING_MODE=paper

# -------------------------------------------------------------------
# Optional below this line — fill in as features are needed.
# -------------------------------------------------------------------

# Cloud LLM fallback — only consulted under AXIOM_LLM_MODE=hybrid (and
# strictly never under "local"). Leave unset on a pure-local cutover.
OPENAI_API_KEY=<sk-...>            # or OPENAI_APY_KEY (V1 typo, both honored)
ANTHROPIC_API_KEY=<sk-ant-...>
GEMINI_API_KEY=<...>               # only if a Gemini gatekeeper override is configured

# Optional remote-Ollama tunneling (e.g. an M-series cluster behind
# Tailscale). Uses bearer auth on the Ollama HTTP front.
OLLAMA_API_KEY=<bearer>

# Polymarket (live mode only — paper mode is signer-agnostic)
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_WALLET_ADDRESS=0x...

# Notifications + churn digest
POSTMARK_SERVER_TOKEN=<...>
POSTMARK_FROM_EMAIL=alerts@yourdomain.com
OPERATOR_ALERT_EMAILS=you@yourdomain.com

# Stripe billing (only if you're running the public marketing pages)
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...

# CORS — frontend origin(s) the FastAPI server should accept
AXIOM_CORS_ORIGINS=https://yourdomain.com,http://localhost:5173
```

## Step 5 — Database migrations

```bash
cd ~/axiom/artifacts/api-server
uv run alembic upgrade head
uv run alembic current   # confirm we're at HEAD
```

## Step 6 — Run the four-gate preflight

This is the single command that proves the cutover is safe.

> **The FastAPI server does not auto-load `.env`.** Every preflight
> gate (and the long-running services in Step 7) needs the env
> exported into the shell first. From a clean terminal:
>
> ```bash
> cd ~/axiom
> set -a            # export every var assigned below
> source ./artifacts/api-server/.env
> set +a
> ```
>
> Run the preflight (and every later step) from the same shell, or
> wrap the source line into your `~/.zprofile` so new shells inherit
> the env automatically.

```bash
cd ~/axiom
PYTHON="uv run python" ./scripts/preflight_mac_studio.sh
```

The script runs four gates sequentially and exits non-zero on the first
failure:

| Gate | What it checks |
|---|---|
| 1 — `verify_v155_artifacts.py` | every canonical predictor in the v155 manifest pulls through `models_loader.ensure_artifacts()`, sha256 verifies, and smoke-loads. |
| 2 — `preflight_calibrators.py` | `calibration_snapshots` carries a `fitted` Platt / isotonic curve for NBA, NFL, NCAAB, NHL, MLB (the V1-archive backed leagues). |
| 3 — `preflight_swarm_smoke.py` | one full `run_debate` round-trip in `AXIOM_LLM_MODE=local` — every persona dispatches against Ollama and the gatekeeper renders a verdict without raising. |
| 4 — `preflight_paper_cycle.py` | one full `orchestrator.cycle.run_cycle('sports')` in `AXIOM_TRADING_MODE=paper` — candidates flow through every gate and the paper-execution stub. |

Fix-up tips when a gate fails:
- **Gate 1** — chase the failing predictor in `app/models_loader.py`
  (filename mapping / multi-part reassembly / sha256 computation). Do
  NOT modify the bucket from the Mac Studio; artifact correctness is
  upstream's job.
- **Gate 2** — refit the missing leagues:
  ```bash
  uv run python -c "import asyncio; from app.calibrators import refit_all; print(asyncio.run(refit_all()))"
  ```
- **Gate 3** — `ollama list` must show all three required models. A
  4xx error from Ollama means a model name in `.env` doesn't match a
  pulled tag.
- **Gate 4** — the cycle entry surfaces the failing component; check
  `app/orchestrator/cycle.py` logs for which gate vetoed.

## Step 7 — `launchd` services

Three services run as user-level launch agents so they survive a
reboot. Drop these plists into `~/Library/LaunchAgents/`.

> **Substitute `<USERNAME>` everywhere below** with the actual macOS
> username (`whoami`). The plists are templates — `launchd` does not
> expand `~`, so the absolute paths must be filled in before
> `launchctl load`.
>
> The api service runs through a tiny wrapper (`bin/run-api.sh`) so
> launchd inherits the `.env` instead of you having to enumerate every
> secret in the plist's `EnvironmentVariables` block. Create it now:
>
> ```bash
> mkdir -p ~/axiom/bin
> cat > ~/axiom/bin/run-api.sh <<'EOF'
> #!/usr/bin/env bash
> # launchd wrapper: source .env then exec uvicorn. Keeps the plist
> # free of secrets and lets you rotate keys by editing .env + a
> # single `launchctl kickstart -k`.
> set -euo pipefail
> cd /Users/<USERNAME>/axiom/artifacts/api-server
> set -a
> source ./.env
> set +a
> exec /opt/homebrew/bin/uv run uvicorn app.main:app \
>   --host 0.0.0.0 --port 8000 --workers 2
> EOF
> chmod +x ~/axiom/bin/run-api.sh
> ```

`com.axiom.ollama.plist` (Ollama daemon):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.axiom.ollama</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/ollama</string>
        <string>serve</string>
    </array>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/Users/Shared/axiom/ollama.log</string>
    <key>StandardErrorPath</key><string>/Users/Shared/axiom/ollama.err</string>
</dict>
</plist>
```

`com.axiom.api.plist` (FastAPI server — invokes `bin/run-api.sh` so
the wrapper picks up `.env`; replace `<USERNAME>` before loading):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.axiom.api</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/&lt;USERNAME&gt;/axiom/bin/run-api.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/&lt;USERNAME&gt;/axiom/artifacts/api-server</string>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/Users/Shared/axiom/api.log</string>
    <key>StandardErrorPath</key><string>/Users/Shared/axiom/api.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key><string>/Users/&lt;USERNAME&gt;</string>
    </dict>
</dict>
</plist>
```

`com.axiom.cloudflared.plist` (public ingress — replace `<USERNAME>`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.axiom.cloudflared</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/cloudflared</string>
        <string>tunnel</string>
        <string>--config</string>
        <string>/Users/&lt;USERNAME&gt;/.cloudflared/config.yml</string>
        <string>run</string>
    </array>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/Users/Shared/axiom/tunnel.log</string>
</dict>
</plist>
```

Load all three:

```bash
mkdir -p /Users/Shared/axiom
launchctl load -w ~/Library/LaunchAgents/com.axiom.ollama.plist
launchctl load -w ~/Library/LaunchAgents/com.axiom.api.plist
launchctl load -w ~/Library/LaunchAgents/com.axiom.cloudflared.plist
```

## Step 8 — Cloudflare tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create axiom-mac-studio
# Note the UUID — you'll reference it in the config below.

mkdir -p ~/.cloudflared
cat > ~/.cloudflared/config.yml <<'EOF'
tunnel: <UUID-from-create>
credentials-file: /Users/colter/.cloudflared/<UUID-from-create>.json

ingress:
  - hostname: api.yourdomain.com
    service: http://localhost:8000
  - service: http_status:404
EOF

cloudflared tunnel route dns axiom-mac-studio api.yourdomain.com
```

## Step 9 — Point the Replit-hosted frontend at the Mac Studio

The web app reads `VITE_API_URL` and prepends it to every `apiFetch`
call (and every WebSocket connection — protocol auto-upgrades
http→ws / https→wss). Empty default preserves today's same-origin
behavior, so this is a pure additive change.

Set, on Replit:

```
VITE_API_URL=https://api.yourdomain.com
```

Restart the `artifacts/web: web` workflow. From this point the same
bundled frontend talks to the Mac-Studio-hosted backend; flip the env
back to empty to fall back to the Replit-hosted FastAPI server.

(Local dev: `cp artifacts/web/.env.example artifacts/web/.env.local`
and fill in `VITE_API_URL` to test the same wiring locally.)

## Step 10 — Paper soak

```bash
# AXIOM_TRADING_MODE=paper is already set in .env from Step 4.
# The trading loop ticks every ~45s; let it run for at least 100
# cycles (~75 minutes) so the paper P&L includes a representative
# slice of the swarm gatekeeper veto distribution.
tail -f /Users/Shared/axiom/api.log | grep -E 'cycle_complete|gate_'
```

What "healthy" looks like over the soak:
- Zero hosted-LLM cost rows in the cost ledger (every persona dispatched
  to Ollama under `AXIOM_LLM_MODE=local`).
- All 17 risk gates evaluate per candidate (no gate raises an exception
  that bypasses evaluation — the `_GATEKEEPER_FAILURES` counter on
  `/api/health/snapshot` should stay at zero).
- Calibrator hits return real Platt-curve adjusted probabilities (the
  `apply()` info dict shows `applied=true` with a non-`degenerate` fit
  for sports markets).
- At least one paper-trade row written to `trades` per active sports
  market over the soak window (the volume reflects the gatekeeper
  threshold tuning, not a wiring bug).

## Step 11 — Cutover to live trading

Only after a clean paper soak:

```bash
# 1. Confirm the operator emails on the alert distribution.
echo "$OPERATOR_ALERT_EMAILS"

# 2. Flip the mode and reload the api service so the new mode propagates.
sed -i '' 's/^AXIOM_TRADING_MODE=paper$/AXIOM_TRADING_MODE=live/' \
  ~/axiom/artifacts/api-server/.env
launchctl kickstart -k gui/$(id -u)/com.axiom.api

# 3. Watch the first live order land.
tail -f /Users/Shared/axiom/api.log | grep -E 'order|fill|kill_switch'
```

Rollback (any time, single command):

```bash
sed -i '' 's/^AXIOM_TRADING_MODE=live$/AXIOM_TRADING_MODE=paper/' \
  ~/axiom/artifacts/api-server/.env
launchctl kickstart -k gui/$(id -u)/com.axiom.api
```

---

## Appendix — files touched by this runbook

- `scripts/preflight_mac_studio.sh` — the four-gate preflight wrapper.
- `artifacts/api-server/scripts/verify_v155_artifacts.py` — gate 1.
- `artifacts/api-server/scripts/preflight_calibrators.py` — gate 2.
- `artifacts/api-server/scripts/preflight_swarm_smoke.py` — gate 3.
- `artifacts/api-server/scripts/preflight_paper_cycle.py` — gate 4.
- `artifacts/web/.env.example` — `VITE_API_URL` documentation.
- `artifacts/web/src/lib/api.ts` (`resolveApiUrl`) and
  `artifacts/web/src/hooks/use-websocket.ts` (`buildWsUrl`) — the
  frontend host knob implementation.
