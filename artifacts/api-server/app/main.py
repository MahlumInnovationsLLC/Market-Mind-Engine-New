"""Axiom-Predict FastAPI application — foundation slice.

This is the minimum viable backend that:
  * Connects to the existing Supabase Postgres (149-table production schema)
  * Connects to Supabase Storage (axiom-predict-artifacts bucket)
  * Exposes `/api/healthz` (open) plus Clerk-gated dashboard endpoints
  * Reports which trained models are present in storage
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import (
    admin as admin_module,
    billing,
    calibrators,
    dashboard,
    db,
    free_tier,
    github_mirror,
    health,
    margin_regressor,
    migrations as db_migrations,
    models_loader,
    predictor,
    rc_billing,
    ready_gate,
    runtime_settings as runtime_settings_mod,
    storage,
)
from .notifications import (
    alerts as notif_alerts,
    churn as notif_churn,
    daily_summary,
    email as notif_user_email,
    persona_brier_alerts as notif_persona_brier_alerts,
    postmark as notif_postmark,
    push as notif_push,
    sms as notif_sms,
    suppression as notif_suppression,
    weekly_roundup as notif_weekly_roundup,
)
from .auth import (
    AuthedUser,
    auth_configured,
    check_admin,
    check_approval,
    require_admin,
    require_approved_user,
    require_auth,
)
from .config import get_settings
from .middleware import RequestIdMiddleware
from .observability import configure_logging
from .log_shipper import get_shipper_status
from . import autoresearch
from .feeds import registry as feed_registry, scheduler as feed_scheduler
from .feeds.odds_api import register_odds_feeds
from .feeds.sportradar import register_sportradar_extra_feeds
from .feeds.weather import register_weather_feeds
from .feeds.crypto import register_crypto_feeds
from .feeds.commodities import register_commodities_feeds
from .feeds.kalshi_crypto_markets import register_kalshi_crypto_markets_feed
from .feeds.tech_evidence import register_tech_evidence_feeds
from .feeds.election_polls import register_election_polls_feeds
from .feeds.unusual_whales import register_unusual_whales_feeds
from .feeds.public_betting import register_public_betting_feeds
from .feeds.outlier_bet import register_outlier_bet_feed
from .features import drift as feature_drift, registry as feature_registry
from .elo import scheduler as elo_scheduler
from .orchestrator import pipeline_event_hub, run_cycle, scheduler as orchestrator_scheduler
from .settlement import (
    backfill_is_running,
    get_backfill_status,
    run_backfill,
    run_cycle as run_settlement_cycle,
    scheduler as settlement_scheduler,
    settle_market,
    settlement_event_hub,
)
from .swarm import llm_log_hub, refresh_persona_weights, run_debate
from .swarm import profiles as swarm_profiles, toggles as swarm_toggles
from .kalshi import (
    OrderRequest,
    auth as kalshi_auth,
    bus as kalshi_bus,
    client as kalshi_client_mod,
    lifecycle as kalshi_lifecycle,
    market_titles as kalshi_market_titles,
    ws as kalshi_ws,
)
from .polymarket import (
    auth as polymarket_auth,
    bus as polymarket_bus,
    client as polymarket_client_mod,
    lifecycle as polymarket_lifecycle,
    ws as polymarket_ws,
)
from .polymarket.lifecycle import OrderRequest as PolymarketOrderRequest
from .telegram import bindings as telegram_bindings, bot as telegram_bot


configure_logging()
log = logging.getLogger("axiom.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("axiom-predict api starting")
    settings = get_settings()  # noqa: F841 — early validation
    ready_gate.begin()
    if not auth_configured():
        # No Clerk publishable key means no way to verify tokens, which means
        # every protected endpoint will return 503.  Fail fast in production
        # rather than silently serving 503s; in dev, log loudly.
        is_prod = (
            os.environ.get("REPLIT_DEPLOYMENT") == "1"
            or (os.environ.get("ENV") or os.environ.get("APP_ENV") or "").lower()
            in ("prod", "production")
        )
        msg = (
            "CLERK_PUBLISHABLE_KEY is not set — operator console auth is "
            "disabled and every protected endpoint will return 503"
        )
        if is_prod:
            raise RuntimeError(msg)
        log.warning(msg)
    try:
        await db.get_pool()
    except Exception as e:
        log.error("Postgres pool init failed: %s", e)

    # Apply Alembic migrations BEFORE any request handler can land. The
    # baseline migration owns the schema that used to be created by
    # lazy ALTERs at request time. Run on a worker thread so the sync
    # psycopg engine doesn't block the event loop.
    try:
        await asyncio.to_thread(db_migrations.run_migrations_sync)
        ready_gate.mark_check(
            "migrations",
            ok=True,
            detail=f"head={db_migrations.current_revision()}",
        )
    except Exception as e:
        log.error("alembic migrations failed: %s", e)
        ready_gate.mark_check("migrations", ok=False, detail=f"{type(e).__name__}: {e}")
    # Hydrate the in-process promoted-feature set from
    # ``autoresearch_features``. Promoted UW candidates are opted back
    # into the live feature vector so a freshly retrained model with
    # the expanded column layout actually sees them. Best-effort: if
    # the DB read fails we serve the built-in 32-column layout.
    try:
        from .autoresearch import feature_bridge as _ar_bridge
        from .autoresearch import store as _ar_store
        from .features import vector as _features_vector
        promoted_rows = await _ar_store.list_candidates(status="production")
        registered = _ar_bridge.register_candidates(promoted_rows)
        _features_vector.set_promoted_names(registered)
        log.info(
            "feature registry promoted set hydrated: %d names",
            len(registered),
        )
    except Exception as e:
        log.warning("promoted feature hydration failed: %s", e)
    # Register + start the external data feed pollers.  Feeds with missing
    # API keys register themselves as `enabled=false` so they show up in
    # /api/feeds/status with a clear skipped_reason but never poll.
    try:
        register_odds_feeds()
        register_weather_feeds()
        register_crypto_feeds()
        register_commodities_feeds()
        try:
            register_kalshi_crypto_markets_feed()
        except Exception as e:
            log.warning("kalshi_crypto_markets feed registration failed: %s", e)
        register_tech_evidence_feeds()
        try:
            register_election_polls_feeds()
        except Exception as e:
            log.warning("election_polls feed registration failed: %s", e)
        try:
            from .feeds.econ_calendar import register_econ_calendar_feeds
            register_econ_calendar_feeds()
        except Exception as e:
            log.warning("econ_calendar feed registration failed: %s", e)
        try:
            from .feeds.entertainment_signals import (
                register_entertainment_signal_feeds,
            )
            register_entertainment_signal_feeds()
        except Exception as e:
            log.warning("entertainment_signals feed registration failed: %s", e)
        try:
            from .feeds import weather_climatology as _wclim

            await _wclim.backfill_climatology()
            await _wclim.refresh_climatology_cache()
        except Exception as e:
            log.warning("weather_climatology bootstrap failed: %s", e)
        try:
            register_sportradar_extra_feeds()
        except Exception as e:
            log.warning("sportradar extra feed registration failed: %s", e)
        try:
            from .feeds.nfl_snap_counts import (
                ensure_schema as _ensure_nfl_snap_schema,
                register_nfl_snap_counts_feed,
            )
            await _ensure_nfl_snap_schema()
            register_nfl_snap_counts_feed()
        except Exception as e:
            log.warning("nfl_snap_counts feed registration failed: %s", e)
        try:
            from .feeds.team_elo_seed import register_team_elo_seed_feed
            register_team_elo_seed_feed()
        except Exception as e:
            log.warning("team_elo_seed feed registration failed: %s", e)
        try:
            register_unusual_whales_feeds()
        except Exception as e:
            log.warning("unusual_whales feed registration failed: %s", e)
        try:
            register_public_betting_feeds()
        except Exception as e:
            log.warning("public_betting feed registration failed: %s", e)
        try:
            register_outlier_bet_feed()
        except Exception as e:
            log.warning("outlier_bet feed registration failed: %s", e)
        await feed_scheduler.start()
    except Exception as e:
        log.error("feed scheduler start failed: %s", e)
    # Bootstrap persona weights from swarm_analyst_metrics so the swarm
    # is ready to vote with calibrated weights on the very first request.
    try:
        n = await refresh_persona_weights()
        log.info("swarm persona weights bootstrapped from %d rows", n)
    except Exception as e:
        log.warning("swarm persona weight bootstrap failed: %s", e)

    # Validate Kalshi credentials at startup (non-fatal — degraded mode allowed).
    cred = kalshi_auth.validate_credentials(
        settings.kalshi_api_key_id, settings.kalshi_private_key
    )
    if cred.get("ok"):
        log.info("Kalshi credentials OK (key fingerprint=%s, env=%s, mode=%s)",
                 cred.get("key_fingerprint"), settings.kalshi_env, settings.trading_mode)
    else:
        log.warning("Kalshi credentials NOT validated: %s", cred.get("error"))

    # Cluster auto-detect: sign a real authenticated call against the
    # configured Kalshi cluster so a wrong-environment mistake (prod
    # creds pointed at demo, or vice versa) is loud at boot instead of
    # silently 401'ing the WS reconnect loop and balance/position polls.
    # See `app.kalshi.client.cluster_probe` for the policy.  We then
    # start a slow background loop (default every 10 minutes) so a
    # mid-session credential rotation or KALSHI_ENV flip is also
    # caught — the boot result alone went stale the moment anything
    # changed underneath us.
    try:
        cluster_check = await kalshi_client_mod.cluster_probe(force=True)
        if cluster_check.get("ok"):
            log.info(
                "Kalshi cluster auth probe OK (env=%s, key_belongs_to=%s)",
                cluster_check.get("configured_env"),
                cluster_check.get("key_belongs_to"),
            )
        else:
            log.error(
                "Kalshi cluster auth probe FAILED: %s", cluster_check.get("error")
            )
    except Exception as e:
        log.warning("Kalshi cluster auth probe raised: %s", e)
    try:
        await kalshi_client_mod.start_cluster_probe_loop()
    except Exception as e:
        log.warning("Kalshi cluster re-probe loop start failed: %s", e)

    # Start WS subscriber + bus->lifecycle bridge if creds are present.
    fill_task = None
    try:
        await kalshi_ws.maybe_start_ws()
        fill_task = await kalshi_lifecycle.attach_ws_fill_handler()
        from .kalshi import orderbook as kalshi_orderbook
        await kalshi_orderbook.start_orderbook_consumer()
    except Exception as e:
        log.warning("Kalshi WS startup skipped: %s", e)

    # Validate Polymarket credentials at startup (non-fatal — degraded mode allowed).
    pcred = polymarket_auth.validate_credentials(
        settings.polymarket_api_key,
        settings.polymarket_api_secret,
        settings.polymarket_api_passphrase,
        settings.polymarket_wallet_address,
    )
    if pcred.get("ok"):
        log.info("Polymarket credentials OK (key fingerprint=%s, mode=%s)",
                 pcred.get("key_fingerprint"), settings.trading_mode)
    else:
        log.warning("Polymarket credentials NOT validated: %s", pcred.get("error"))

    # Drain the Polymarket bus into the lifecycle so user-channel `trade`
    # / `order` updates flip resting orders to `filled`. Mirrors the
    # Kalshi fill router above; non-fatal so a missing WS doesn't take
    # down the API.
    polymarket_fill_task = None
    try:
        polymarket_fill_task = await polymarket_lifecycle.attach_ws_fill_handler()
    except Exception as e:
        log.warning("Polymarket fill router start failed: %s", e)

    # Start the trading orchestrator scheduler.  Honors
    # AXIOM_ORCHESTRATOR_ENABLED + AXIOM_TRADING_MODE.  Cycle failures
    # are caught inside the loop so a sick DB doesn't stop the process.
    try:
        await orchestrator_scheduler.start()
    except Exception as e:
        log.warning("orchestrator scheduler start failed: %s", e)

    # Settlement loop: closes finalized markets, writes post-mortems,
    # updates persona Brier, accumulates daily PnL, raises drift alerts.
    # Runs independent of trading-mode halts so feedback never stalls.
    try:
        await settlement_scheduler.start()
    except Exception as e:
        log.warning("settlement scheduler start failed: %s", e)

    # Background tap on the settlement event hub: caches the most recent
    # market_settle_complete events in-process so the dashboard panel can
    # render history immediately on first paint (the WS only delivers
    # events that occur while a client is connected).
    settlement_tap_task = asyncio.create_task(
        _settlement_recent_tap(), name="settlement-recent-tap"
    )

    # System-health collector + daily P&L summary scheduler.  Both are
    # background tasks that touch in-memory state only on success and
    # use try/except internally so a sick dep can't take down the process.
    try:
        await health.start()
    except Exception as e:
        log.warning("health collector start failed: %s", e)
    try:
        await daily_summary.start()
    except Exception as e:
        log.warning("daily summary scheduler start failed: %s", e)
    try:
        await notif_weekly_roundup.start()
    except Exception as e:
        log.warning("weekly roundup scheduler start failed: %s", e)
    # Per-persona daily Brier degradation watcher (task #305).  Polls
    # every ~10m, computes today's per-persona Brier via the same
    # `aggregate_persona_day` helper the daily summary uses, and pages
    # an operator alert when any persona's score crosses the configured
    # absolute / jump threshold.
    try:
        await notif_persona_brier_alerts.start()
    except Exception as e:
        log.warning("persona brier alerts scheduler start failed: %s", e)
    # RevenueCat webhook silence detector — fires an operator alert
    # when the mobile-IAP webhook stops delivering during business hours
    # (catches dashboard pause/resume + upstream outages). Per task #252.
    try:
        await rc_billing.start()
    except Exception as e:
        log.warning("rc webhook silence loop start failed: %s", e)
    # Per-provider LLM fallback-rate spike monitor (task #145). Reads
    # `fallback_used` / `primary_provider` from `llm_predictions.metadata`
    # and pages when either provider's fallback rate exceeds the
    # configured threshold over the trailing window.
    try:
        from .swarm import fallback_monitor as _fallback_monitor
        await _fallback_monitor.start()
    except Exception as e:
        log.warning("LLM fallback monitor start failed: %s", e)
    try:
        await notif_churn.start()
    except Exception as e:
        log.warning("churn digest scheduler start failed: %s", e)
    # Telegram concierge bot (per task #111). No-op when
    # TELEGRAM_BOT_TOKEN is unset (the dev / CI default).
    try:
        await telegram_bot.start()
    except Exception as e:
        log.warning("telegram bot start failed: %s", e)

    # Periodic feature-drift snapshot logger.  Pure in-process — no DB
    # dependency, so it's safe to start before the ready gate clears.
    try:
        await feature_drift.scheduler.start()
    except Exception as e:
        log.warning("feature drift scheduler start failed: %s", e)

    # Elo updater: scans the `games` table for newly-finished matchups
    # and feeds them into `record_game()` so per-team Elo ratings stay
    # current.  Idempotent at both the SQL and engine layers.
    try:
        await elo_scheduler.scheduler.start()
    except Exception as e:
        log.warning("elo updater scheduler start failed: %s", e)

    # Continuous Kalshi balance -> bankroll_events sync. Polls every 60s
    # and writes a delta row whenever the live balance moves so the
    # bankroll-floor risk gate and equity curve always reflect reality.
    try:
        from . import bankroll_sync as _bankroll_sync
        await _bankroll_sync.start()
    except Exception as e:
        log.warning("bankroll sync start failed: %s", e)

    # `order_fills` dedupe-table retention sweep. The table only needs
    # rows around long enough to suppress WS-replay duplicates on
    # reconnect (a few hours); without this loop it grows unboundedly.
    # Per task #154.
    try:
        from . import order_fills_retention as _order_fills_retention
        await _order_fills_retention.start()
    except Exception as e:
        log.warning("order_fills retention start failed: %s", e)

    # `free_tier_market_views` quota-table retention sweep. The free-tier
    # quota check only ever reads today's UTC partition, so anything older
    # than ~30 days is dead weight and would otherwise grow linearly with
    # active free users. Per task #165.
    try:
        from . import free_tier_views_retention as _free_tier_views_retention
        await _free_tier_views_retention.start()
    except Exception as e:
        log.warning("free_tier_views retention start failed: %s", e)

    # `crypto_ohlcv` bar-table retention sweep. The poller is
    # append-with-upsert with no TTL, so without this loop the table
    # grows unboundedly (~1M rows/year per symbol at 1m). Per task #175.
    try:
        from . import crypto_ohlcv_retention as _crypto_ohlcv_retention
        await _crypto_ohlcv_retention.start()
    except Exception as e:
        log.warning("crypto_ohlcv retention start failed: %s", e)

    # Kalshi market-tick cache refresh loop. Tops up the
    # ``market_price_ticks`` cache for recently-traded markets so the
    # /trade/:ref chart serves out of Postgres on revisit instead of
    # round-tripping the Kalshi candlesticks endpoint live. Per task #359.
    try:
        from .kalshi import tick_cache_refresh as _tick_cache_refresh
        await _tick_cache_refresh.start()
    except Exception as e:
        log.warning("kalshi tick cache refresh start failed: %s", e)

    # Continuous Kalshi position + fill reconciler. Periodically pulls
    # /portfolio/positions and /portfolio/fills, diffs against our local
    # DB, raises model_health_alerts on drift > tolerance, and back-fills
    # any fills the WS missed. Disabled in paper mode and when Kalshi
    # creds are absent. Per cutover task #266.
    try:
        from .kalshi import reconciler as _kalshi_reconciler
        await _kalshi_reconciler.start()
    except Exception as e:
        log.warning("kalshi reconciler start failed: %s", e)

    # AutoResearch — nightly UW-signal mining job. Discovered candidates
    # land in `autoresearch_features` with status='shadow' for an admin
    # to promote (see /api/admin/autoresearch/*). Disabled by setting
    # AXIOM_AUTORESEARCH_DISABLED=1.
    try:
        await autoresearch.scheduler.start()
    except Exception as e:
        log.warning("autoresearch scheduler start failed: %s", e)

    # Scheduled crypto-binary backtests (task #421). Runs `run_backtest`
    # on every supported (symbol, timeframe) pair on a regular cadence,
    # persists each result to `crypto_backtest_runs` so the dashboard
    # can chart a trend, and pages the operator when Brier / log-loss
    # crosses the configured absolute threshold or jumps versus the
    # rolling baseline.
    try:
        from .crypto import backtest_scheduler as _crypto_backtest_scheduler
        await _crypto_backtest_scheduler.start()
    except Exception as e:
        log.warning("crypto backtest scheduler start failed: %s", e)

    # Kick off model artifact reassembly in the background so a cold cache
    # (~240 MiB download) doesn't block the HTTP server from accepting
    # traffic. Health endpoints surface progress while it warms up.
    models_task = asyncio.create_task(models_loader.ensure_artifacts())

    # Load any previously-fitted per-league probability calibrators so
    # `predict()` can apply them on the very first request. Best-effort
    # — `apply()` cleanly bypasses any league with no fitted artifact,
    # so a fresh install with no snapshots still trades on raw probs.
    try:
        await calibrators.load_all()
    except Exception as e:
        log.warning("per-league calibrator bootstrap failed: %s", e)

    # Load any previously-fitted per-league margin/total regressors so
    # spread/total markets immediately use fitted coefficients instead
    # of the cold-start textbook SDs.  Failures are non-fatal — the
    # registry simply stays empty until the next settlement-loop refit
    # lands a fresh fit.
    try:
        await margin_regressor.load_all()
    except Exception as e:
        log.warning("margin regressor bootstrap failed: %s", e)

    # Hydrate the persisted KXRAIN EMOS coefficients (task #280) so a
    # restarted API picks the most-recent nightly refit back up
    # instead of dropping to the shipped defaults until the next
    # retrain cycle lands. Best-effort — a missing settings row just
    # leaves the env / default coefficients in place.
    try:
        from .settlement import retrain as _retrain
        await _retrain.load_persisted_precip_emos_coefficients()
    except Exception as e:
        log.warning("precip EMOS coefficient bootstrap failed: %s", e)

    # Per-vertical retrained calibrator listener (task #88).  The
    # online-learning worker writes fresh isotonic snapshots tagged
    # `drift_status.status='retrained'`; this listener invalidates
    # the in-process cache on every `retrain_complete` event so the
    # next trading cycle picks up the freshly-promoted calibrator.
    try:
        from .models import live_calibrator as _live_calibrator
        _live_calibrator.start_invalidation_listener()
    except Exception as e:
        log.warning("live calibrator listener start failed: %s", e)

    # Restore the operator-set active-predictor override (if any) so the
    # next /predict call honours the last "make active" choice without
    # waiting for an admin POST. Best-effort: a missing setting just
    # leaves the static priority chain in place.
    try:
        active_name = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = 'predictor.active_name'"
        )
        if active_name:
            predictor.set_active_override(str(active_name))
            log.info("[predictor] restored active override -> %s", active_name)
    except Exception as e:
        log.debug("predictor active override restore skipped: %s", e)

    # READY-Gate watcher: stays parked here while the rest of the boot
    # sequence does its async work, then flips the gate so the
    # orchestrator (which is parked in `_loop`) can start firing
    # cycles. Surfaced via /api/health/ready.
    ready_watcher_task = asyncio.create_task(
        _ready_gate_watcher(models_task), name="ready-gate-watcher"
    )

    try:
        yield
    finally:
        try:
            await daily_summary.stop()
        except Exception as e:
            log.warning("daily summary stop error: %s", e)
        try:
            await notif_weekly_roundup.stop()
        except Exception as e:
            log.warning("weekly roundup stop error: %s", e)
        try:
            await notif_persona_brier_alerts.stop()
        except Exception as e:
            log.warning("persona brier alerts stop error: %s", e)
        try:
            from .crypto import backtest_scheduler as _crypto_backtest_scheduler
            await _crypto_backtest_scheduler.stop()
        except Exception as e:
            log.warning("crypto backtest scheduler stop error: %s", e)
        try:
            await rc_billing.stop()
        except Exception as e:
            log.warning("rc webhook silence loop stop error: %s", e)
        try:
            from .swarm import fallback_monitor as _fallback_monitor
            await _fallback_monitor.stop()
        except Exception as e:
            log.warning("LLM fallback monitor stop error: %s", e)
        try:
            await notif_churn.stop()
        except Exception as e:
            log.warning("churn digest stop error: %s", e)
        try:
            await telegram_bot.stop()
        except Exception as e:
            log.warning("telegram bot stop error: %s", e)
        try:
            await feature_drift.scheduler.stop()
        except Exception as e:
            log.warning("feature drift scheduler stop error: %s", e)
        try:
            await elo_scheduler.scheduler.stop()
        except Exception as e:
            log.warning("elo updater scheduler stop error: %s", e)
        try:
            await autoresearch.scheduler.stop()
        except Exception as e:
            log.warning("autoresearch scheduler stop error: %s", e)
        try:
            await health.stop()
        except Exception as e:
            log.warning("health collector stop error: %s", e)
        try:
            settlement_tap_task.cancel()
            try:
                await settlement_tap_task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception as e:
            log.warning("settlement tap stop error: %s", e)
        try:
            await settlement_scheduler.stop()
        except Exception as e:
            log.warning("settlement scheduler stop error: %s", e)
        try:
            await orchestrator_scheduler.stop()
        except Exception as e:
            log.warning("orchestrator scheduler stop error: %s", e)
        try:
            from . import bankroll_sync as _bankroll_sync
            await _bankroll_sync.stop()
        except Exception as e:
            log.warning("bankroll sync stop error: %s", e)
        try:
            from . import order_fills_retention as _order_fills_retention
            await _order_fills_retention.stop()
        except Exception as e:
            log.warning("order_fills retention stop error: %s", e)
        try:
            from . import free_tier_views_retention as _free_tier_views_retention
            await _free_tier_views_retention.stop()
        except Exception as e:
            log.warning("free_tier_views retention stop error: %s", e)
        try:
            from . import crypto_ohlcv_retention as _crypto_ohlcv_retention
            await _crypto_ohlcv_retention.stop()
        except Exception as e:
            log.warning("crypto_ohlcv retention stop error: %s", e)
        try:
            from .kalshi import tick_cache_refresh as _tick_cache_refresh
            await _tick_cache_refresh.stop()
        except Exception as e:
            log.warning("kalshi tick cache refresh stop error: %s", e)
        try:
            from .kalshi import reconciler as _kalshi_reconciler
            await _kalshi_reconciler.stop()
        except Exception as e:
            log.warning("kalshi reconciler stop error: %s", e)
        try:
            await feed_scheduler.stop()
        except Exception as e:
            log.warning("feed scheduler stop error: %s", e)
        if not models_task.done():
            models_task.cancel()
            try:
                await models_task
            except (asyncio.CancelledError, Exception):
                pass
        if not ready_watcher_task.done():
            ready_watcher_task.cancel()
            try:
                await ready_watcher_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await kalshi_client_mod.stop_cluster_probe_loop()
        except Exception as e:
            log.warning("kalshi cluster re-probe loop stop error: %s", e)
        try:
            await kalshi_ws.stop_ws()
        except Exception:
            pass
        try:
            from .kalshi import orderbook as _kalshi_orderbook
            await _kalshi_orderbook.stop_orderbook_consumer()
        except Exception:
            pass
        if fill_task is not None:
            fill_task.cancel()
        if polymarket_fill_task is not None:
            polymarket_fill_task.cancel()
            try:
                await polymarket_fill_task
            except (asyncio.CancelledError, Exception):
                pass
        await db.close_pool()
        log.info("axiom-predict api stopped")


async def _ready_gate_watcher(models_task: asyncio.Task) -> None:
    """Wait for the boot-time prerequisites and then flip the READY gate.

    Checks (each surfaced by name on /api/health/ready):
      * `secrets` — required application secrets are set.
      * `models`  — model_loader.ensure_artifacts() completed and
                    `state.ready` is True (manifest verification passed).
      * `feeds`   — every enabled feed has produced at least one poll
                    outcome (success OR failure — the gate cares only
                    that the polling pipeline is alive).

    The watcher does not retry: a failed check is reported through
    /api/health/ready and the orchestrator stays parked until the
    process restarts (and the next boot runs the checks again).
    """
    settings = get_settings()
    # Secrets check — Settings construction already validated the
    # mandatory ones (SUPABASE_*); we just record the result and surface
    # which optional creds are present.
    missing = []
    for label, present in (
        ("supabase_db", bool(settings.supabase_db_password)),
        ("supabase_service_key", bool(settings.supabase_service_key)),
        ("clerk", auth_configured()),
    ):
        if not present:
            missing.append(label)
    if missing:
        ready_gate.mark_check(
            "secrets", ok=False, detail=f"missing: {', '.join(missing)}"
        )
    else:
        ready_gate.mark_check("secrets", ok=True, detail="required secrets present")

    # Models — wait for the loader background task; mark ok only when
    # the resulting state is `ready` (manifest verification passed),
    # OR when the deploy explicitly has no model artifacts configured
    # yet (T007 deferred). The latter must not park the orchestrator —
    # downstream code that needs predictors checks `state.ready` itself
    # and degrades gracefully.
    try:
        await models_task
        state = models_loader.get_state()
        if state.ready and not state.error:
            ready_gate.mark_check(
                "models",
                ok=True,
                detail=f"{len(state.predictors)} predictor(s) loaded",
            )
        elif (
            not state.error
            and not state.ready
            and not state.in_progress
            and not state.predictors
        ):
            # T007 deferred: no artifacts available, no error reported.
            # Allow READY to flip so the orchestrator can run feed-driven
            # work (markets refresh, settlement, etc.) without ML scoring.
            ready_gate.mark_check(
                "models",
                ok=True,
                detail="no model artifacts configured (T007 deferred); orchestrator allowed without ML predictors",
            )
        else:
            ready_gate.mark_check(
                "models",
                ok=False,
                detail=state.error or "models_loader did not reach ready state",
            )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        ready_gate.mark_check("models", ok=False, detail=f"{type(e).__name__}: {e}")

    # Feeds — give each enabled real-time feed up to FEED_FIRST_POLL_TIMEOUT
    # to produce its first outcome. Disabled feeds are ignored. Long-cadence
    # backfill feeds (cadence ≥ FEEDS_REALTIME_THRESHOLD_SECONDS) are
    # excluded — they're not real-time data sources and shouldn't park the
    # trading loop while they slowly chew through historical data on the
    # 24h tick. The threshold defaults to 1 hour: anything that polls less
    # often than that is treated as a backfill/maintenance feed.
    timeout = float(os.environ.get("AXIOM_READY_FEEDS_TIMEOUT_SECONDS") or "120")
    realtime_threshold = float(
        os.environ.get("AXIOM_READY_FEEDS_REALTIME_THRESHOLD_SECONDS") or "3600"
    )
    deadline = asyncio.get_event_loop().time() + timeout
    pending_feeds: list[str] = []
    while True:
        snap = feed_registry.snapshot()
        enabled_realtime = [
            s for s in snap
            if s.get("enabled")
            and float(s.get("cadence_seconds") or 0.0) < realtime_threshold
        ]
        pending_feeds = [
            s["name"]
            for s in enabled_realtime
            if not (s.get("last_ok_at") or s.get("last_error_at"))
        ]
        if not pending_feeds:
            break
        if asyncio.get_event_loop().time() >= deadline:
            break
        await asyncio.sleep(1.0)
    snap_now = feed_registry.snapshot()
    enabled_total = sum(1 for s in snap_now if s.get("enabled"))
    backfill_n = sum(
        1 for s in snap_now
        if s.get("enabled")
        and float(s.get("cadence_seconds") or 0.0) >= realtime_threshold
    )
    if pending_feeds:
        ready_gate.mark_check(
            "feeds",
            ok=False,
            detail=(
                f"no first poll within {timeout:.0f}s: {', '.join(pending_feeds)} "
                f"(realtime threshold={realtime_threshold:.0f}s; "
                f"{backfill_n} backfill feed(s) excluded)"
            ),
        )
    else:
        ready_gate.mark_check(
            "feeds",
            ok=True,
            detail=(
                f"{enabled_total - backfill_n} realtime feed(s) responded; "
                f"{backfill_n} backfill feed(s) deferred"
            ),
        )

    # Trip the gate only if every check passed. If any failed, leave
    # the gate clear so the orchestrator stays parked; ops can inspect
    # /api/health/ready to see why and either fix the problem and
    # restart, or surface this to the operator UI.
    all_checks = ready_gate.status()["checks"]
    if all(c["ok"] for c in all_checks.values()):
        ready_gate.set_ready()
    else:
        bad = [n for n, c in all_checks.items() if not c["ok"]]
        log.error(
            "READY gate NOT flipped — failing checks: %s; orchestrator parked",
            bad,
        )


app = FastAPI(
    title="Axiom-Predict API",
    version="0.1.0",
    description="Backend for the Axiom-Predict prediction-market trading platform.",
    lifespan=lifespan,
)

_settings = get_settings()
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_settings.cors_origins) or ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["authorization", "content-type", "accept", "x-request-id"],
    expose_headers=["x-request-id"],
)


# ---------------------------------------------------------------------------
# Health + diagnostics
# ---------------------------------------------------------------------------


@app.get("/api/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe (always returns ok if the process is up). Public."""
    return {"status": "ok"}


@app.get("/api/health/ready")
async def health_ready(response: Request) -> dict[str, object]:
    """Readiness probe — public.

    Returns 200 once the READY gate has flipped (migrations applied,
    required secrets present, models loaded, every enabled feed has
    responded at least once). Returns 503 with the per-check status
    while any prerequisite is still failing or in progress, so a
    deployment health-check or load-balancer can hold traffic off the
    instance until the trading loop is allowed to unpark.
    """
    snap = ready_gate.status()
    if snap["ready"]:
        return {"status": "ready", **snap}
    raise HTTPException(status_code=503, detail={"status": "not_ready", **snap})


# ---------------------------------------------------------------------------
# Billing: Stripe Checkout + Customer Portal + webhook + tier resolution
# ---------------------------------------------------------------------------


class CheckoutSessionRequest(BaseModel):
    price_id: str = Field(..., min_length=1, max_length=200)


@app.get("/api/billing/pricing")
async def billing_pricing() -> dict[str, object]:
    """Public — the pricing-page renders against this without needing auth."""
    return billing.public_pricing_catalog()


@app.get("/api/public/equity-curve")
async def public_equity_curve(days: int = 30) -> dict[str, object]:
    """Public — daily cumulative realized PnL for the marketing landing page."""
    clamped_days = max(1, min(int(days), 365))
    points = await dashboard.get_public_equity_curve(clamped_days)
    return {"days": clamped_days, "points": points}


@app.get("/api/public/stats")
async def public_stats() -> dict[str, object]:
    """Public — high-level live trading stats for the marketing hero."""
    return await dashboard.get_public_stats()


@app.get("/api/public/recent-signals")
async def public_recent_signals(limit: int = 4) -> dict[str, object]:
    """Public — most-recent settled signals (anonymized) for the landing page."""
    clamped = max(1, min(int(limit), 12))
    rows = await dashboard.get_recent_settled_signals(clamped)
    return {"signals": rows}


class LandingLeadRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    interest: str | None = Field(default=None, max_length=120)
    source: str | None = Field(default=None, max_length=80)


# In-process per-IP rate limiter for the public lead-capture endpoint.
# Maps ip_hash -> list of unix-timestamp floats of recent submissions.
# Bounded by pruning to the last `_LEADS_RATE_WINDOW_S` seconds on every
# call, so memory stays proportional to active submitters.
_LEADS_RATE_LIMIT_PER_MIN = 5
_LEADS_RATE_WINDOW_S = 60.0
_LEADS_DEDUPE_HOURS = 24
_leads_rate_buckets: dict[str, list[float]] = {}


def _leads_rate_check(ip_hash: str | None) -> bool:
    """Return True if the request is under the per-IP rate limit.

    No-op (always allows) when we have no client IP to bucket by — we'd
    rather let the request through than 429 every caller behind a proxy
    that strips the source address.
    """
    if not ip_hash:
        return True
    now = time.time()
    cutoff = now - _LEADS_RATE_WINDOW_S
    bucket = _leads_rate_buckets.get(ip_hash, [])
    bucket = [t for t in bucket if t > cutoff]
    if len(bucket) >= _LEADS_RATE_LIMIT_PER_MIN:
        _leads_rate_buckets[ip_hash] = bucket
        return False
    bucket.append(now)
    _leads_rate_buckets[ip_hash] = bucket
    return True


@app.post("/api/public/leads")
async def public_leads(body: LandingLeadRequest, request: Request) -> dict[str, object]:
    """Public — capture an early-access lead from the landing form.

    Stores the row in `landing_leads` and best-effort sends a confirmation
    email via Postmark (no-op if Postmark isn't configured).  Returns
    `{ok: true}` whether or not the email was delivered, so callers can
    keep their UX simple.

    De-duplication: a submission whose lower(email) already appears in
    `landing_leads` within the last 24 hours short-circuits — we return
    `{ok: true, deduped: true}` without inserting a new row or
    re-sending the confirmation email. Per-IP rate-limited to 5 per
    minute (returns 429 over quota) to keep bots from spamming Postmark.
    """
    import hashlib

    email = body.email.strip()
    if "@" not in email or "." not in email.split("@", 1)[-1]:
        raise HTTPException(status_code=400, detail="invalid email")
    interest = (body.interest or "").strip() or None
    source = (body.source or "landing").strip() or "landing"
    user_agent = request.headers.get("user-agent", "")[:500] or None
    client_host = request.client.host if request.client else ""
    ip_hash = hashlib.sha256(client_host.encode("utf-8")).hexdigest()[:32] if client_host else None

    if not _leads_rate_check(ip_hash):
        raise HTTPException(status_code=429, detail="too many requests")

    # Skip the insert + Postmark send if this email already signed up
    # within the dedupe window. Failures fall through to the normal
    # insert path so a flaky lookup never costs us a real lead.
    try:
        existing_id = await dashboard.find_recent_lead_by_email(
            email, hours=_LEADS_DEDUPE_HOURS
        )
    except Exception as e:
        log.warning("lead dedupe lookup failed: %s", e)
        existing_id = None
    if existing_id is not None:
        return {
            "ok": True,
            "lead_id": existing_id,
            "confirmation_sent": False,
            "deduped": True,
        }

    try:
        lead_id = await dashboard.insert_landing_lead(
            email=email,
            interest=interest,
            source=source,
            user_agent=user_agent,
            ip_hash=ip_hash,
        )
    except Exception as e:
        log.warning("insert_landing_lead failed: %s", e)
        raise HTTPException(status_code=503, detail="lead store unavailable")

    interest_line = f"Interest area: {interest}\n" if interest else ""
    text_body = (
        "Thanks for signing up for early access to Axiom Predict.\n\n"
        f"{interest_line}"
        "We'll email you the moment the next vertical opens up. In the\n"
        "meantime you can watch the live equity curve and methodology at\n"
        "https://axiom-predict.app.\n\n"
        "— The Axiom Predict team\n"
    )
    try:
        results = await notif_postmark.send(
            subject="You're on the Axiom Predict early-access list",
            text_body=text_body,
            to=[email],
            tag="landing-lead-confirmation",
        )
        delivered = any(r.get("ok") for r in results)
    except Exception as e:
        log.warning("postmark confirmation failed for lead %s: %s", lead_id, e)
        delivered = False
    if delivered:
        await dashboard.mark_lead_confirmation_sent(lead_id)
    return {"ok": True, "lead_id": lead_id, "confirmation_sent": delivered}


@app.get("/api/me/subscription")
async def me_subscription(
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    sub = await billing.get_user_subscription(user)
    payload = sub.as_dict()
    # Surface the daily market-view quota for free callers so the
    # dashboard can render a "N of 5 markets used today" indicator
    # before the user even opens a market. Pro/Elite/Founder are
    # uncapped, so we omit the field entirely for them — the client
    # uses presence-of-quota as the signal to render the badge.
    if sub.tier == billing.TIER_FREE:
        viewed = await free_tier.count_markets_viewed_today(user.user_id)
        payload["quota"] = {
            "viewed_today": viewed,
            "limit": free_tier.FREE_TIER_DAILY_MARKET_LIMIT,
            "resets_at": "00:00 UTC",
        }
    return payload


@app.post("/api/billing/checkout-session")
async def billing_checkout_session(
    body: CheckoutSessionRequest,
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    return await billing.create_checkout_session(user, body.price_id)


@app.post("/api/billing/portal-session")
async def billing_portal_session(
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    return await billing.create_portal_session(user)


@app.post("/api/billing/revenuecat-webhook")
async def billing_revenuecat_webhook(request: Request) -> dict[str, object]:
    """Receive subscription lifecycle events from RevenueCat (mobile IAP)."""
    return await rc_billing.handle_webhook(request)


@app.post("/api/billing/webhook")
async def billing_webhook(request: Request) -> dict[str, object]:
    """Stripe webhook receiver.  Signature-verified, idempotent."""
    return await billing.handle_webhook(request)


@app.get("/api/me/access")
async def me_access(user: AuthedUser = Depends(require_auth)) -> dict[str, object]:
    """Report whether the signed-in user is on the operator allow-list.

    Authenticated but unapproved users hit this endpoint to detect that
    they should see the "awaiting approval" screen instead of the
    dashboard (which would otherwise just throw 403s).
    """
    approved, email = await check_approval(user)
    is_admin = await check_admin(user)
    # Always reflect the caller in the admin user table so the admin
    # console shows everyone who has ever signed in.
    try:
        from .auth import _resolve_email_and_name

        _, name = await _resolve_email_and_name(user)
        await admin_module.upsert_seen_user(user.user_id, email, name)
    except Exception as e:
        log.warning("/api/me/access upsert failed: %s", e)

    # Best-effort: ping the operator the first time an unapproved user
    # hits this endpoint so they know to come approve them. Dedupe per
    # user for 24h so repeated polling from the "Awaiting approval"
    # screen doesn't spam the inbox. Never fail the access check on
    # email-provider trouble.
    if not (approved or is_admin):
        async def _notify_pending() -> None:
            try:
                ok = await notif_alerts.dedupe_or_skip(
                    "pending-approval",
                    user.user_id,
                    ttl_seconds=24 * 3600,
                )
                if ok:
                    notif_alerts.pending_approval(
                        user_id=user.user_id,
                        user_email=email,
                    )
            except Exception as e:
                log.warning("pending_approval notify failed: %s", e)

        try:
            asyncio.create_task(_notify_pending())
        except Exception as e:
            log.warning("pending_approval schedule failed: %s", e)

    return {
        "approved": approved or is_admin,
        "is_admin": is_admin,
        "email": email,
        "user_id": user.user_id,
    }


# ---------------------------------------------------------------------------
# Mobile push notifications (Expo)
# ---------------------------------------------------------------------------


class PushTokenRequest(BaseModel):
    expo_token: str = Field(..., min_length=1, max_length=300)
    platform: str | None = Field(default=None, max_length=32)


class NotificationPrefsRequest(BaseModel):
    # Mobile push toggles (existing behaviour).
    notify_live_trade: bool | None = None
    notify_halt: bool | None = None
    # Mobile push for position settlement (task #132).
    notify_settlement: bool | None = None
    # Per-user email channel toggles (task #112).
    email_daily_summary: bool | None = None
    email_drift: bool | None = None
    email_halt: bool | None = None
    email_live_trade: bool | None = None
    email_weekly_roundup: bool | None = None
    # Per-user HTML trade-fill confirmation + consolidate-partials mode
    # (task #269).
    email_trade_confirmation: bool | None = None
    digest_partials: bool | None = None
    # Elite-only premium channels.
    sms_live_trade: bool | None = None
    telegram_live_trade: bool | None = None
    # Phone number for SMS — `null` clears, omitted leaves unchanged.
    sms_phone_number: str | None = None
    # Whether the request explicitly intends to clear / set the phone
    # so we can distinguish "omitted" from "set to null".
    sms_phone_number_present: bool = False
    # Per-user quiet-hours window (task #131). Each field follows the
    # same "omitted vs explicit null" pattern as the phone number — the
    # `*_present` flags let the client clear an individual value (e.g.
    # blanking the timezone disables the window) while leaving any
    # un-mentioned field untouched.
    quiet_hours_start_hour: int | None = None
    quiet_hours_start_hour_present: bool = False
    quiet_hours_end_hour: int | None = None
    quiet_hours_end_hour_present: bool = False
    quiet_hours_tz: str | None = None
    quiet_hours_tz_present: bool = False


@app.get("/api/me/push-token")
async def me_list_push_tokens(
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    """Non-secret summary of devices registered for push notifications.

    Returned to the web /profile page so the operator can see whether the
    Axiom Mobile app is connected without us echoing raw expo tokens.
    """
    devices = await notif_push.list_user_devices(user.user_id)
    return {"count": len(devices), "devices": devices}


@app.post("/api/me/push-token")
async def me_register_push_token(
    body: PushTokenRequest,
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    ok = await notif_push.register_token(
        clerk_user_id=user.user_id,
        expo_token=body.expo_token,
        platform=body.platform,
    )
    if not ok:
        raise HTTPException(status_code=400, detail="invalid push token")
    return {"ok": True}


@app.delete("/api/me/push-token")
async def me_unregister_push_token(
    body: PushTokenRequest,
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    removed = await notif_push.unregister_token(
        clerk_user_id=user.user_id,
        expo_token=body.expo_token,
    )
    return {"ok": True, "removed": removed}


async def _notification_prefs_payload(user: AuthedUser) -> dict[str, object]:
    prefs = await notif_push.get_prefs(user.user_id)
    # SECURITY: never echo the in-flight verification code back to the
    # client — that would let any authenticated session bypass the SMS
    # round-trip and self-verify a stranger's phone. Strip the raw code
    # and the internal `verified_at` timestamp; expose only the public
    # `sms_phone_verified` boolean (and the expiry, for UI countdown).
    safe_prefs = {
        k: v for k, v in prefs.items()
        if k not in ("sms_phone_verification_code", "sms_phone_verified_at")
    }
    sub = await billing.get_user_subscription(user)
    elite_unlocked = sub.tier in (billing.TIER_ELITE, billing.TIER_FOUNDER)
    return {
        "prefs": safe_prefs,
        "tier": sub.tier,
        "elite_unlocked": elite_unlocked,
        "elite_only_toggles": sorted(notif_push.ELITE_ONLY_TOGGLES),
    }


class MarkReadRequest(BaseModel):
    ids: list[int] | None = None


@app.get("/api/me/notifications")
async def me_notifications(
    limit: int = 50,
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    """Per-user history of mobile push notifications (task #133).

    Returns the most recent ``limit`` (capped at 200) entries plus the
    current unread count so the mobile tab can render a badge without a
    second roundtrip.
    """
    items = await notif_push.list_notifications(user.user_id, limit=limit)
    unread = await notif_push.unread_notification_count(user.user_id)
    return {"items": items, "unread_count": unread}


@app.post("/api/me/notifications/read")
async def me_notifications_mark_read(
    body: MarkReadRequest | None = None,
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    ids = body.ids if body else None
    updated = await notif_push.mark_notifications_read(user.user_id, ids=ids)
    unread = await notif_push.unread_notification_count(user.user_id)
    return {"ok": True, "updated": updated, "unread_count": unread}


@app.delete("/api/me/notifications/{notification_id}")
async def me_notifications_delete(
    notification_id: int,
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    """Remove a single in-app notification row owned by the caller.

    Powers the swipe-to-dismiss gesture on the mobile alerts feed
    (task #347). Returns 404 when the row does not exist or belongs to
    a different user — the latter is intentionally indistinguishable to
    avoid leaking which ids are valid for other accounts.
    """
    deleted = await notif_push.delete_notification(user.user_id, notification_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="notification not found")
    unread = await notif_push.unread_notification_count(user.user_id)
    return {"ok": True, "unread_count": unread}


@app.delete("/api/me/notifications")
async def me_notifications_clear_all(
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    """Drop every in-app notification row for the caller.

    Backs the "Clear all" header affordance on the mobile alerts feed
    (task #347). Always returns 200 so the UI can collapse the list
    without special-casing the empty state.
    """
    deleted = await notif_push.delete_all_notifications(user.user_id)
    return {"ok": True, "deleted": deleted, "unread_count": 0}


@app.get("/api/me/notification-history")
async def me_notification_history(
    limit: int = 30,
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    """Read-only feed of the most recent alert dispatches the calling
    user was eligible for (task #192).

    Backed by `notification_dedupe`; entries include the alert type,
    timestamp, and the channels (email/push/sms/telegram) the user was
    opted into at fetch time. Caps at 200.
    """
    items = await notif_alerts.list_user_history(
        user_id=user.user_id, limit=int(limit or 30)
    )
    return {"items": items, "limit": min(200, max(1, int(limit or 30)))}


@app.get("/api/me/notification-prefs")
async def me_get_notification_prefs(
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    return await _notification_prefs_payload(user)


@app.put("/api/me/notification-prefs")
async def me_set_notification_prefs(
    body: NotificationPrefsRequest,
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    # Reject Elite-only toggles for users who can't actually use them so
    # the dispatch path doesn't have to second-guess stale opt-ins after
    # a downgrade.
    sub = await billing.get_user_subscription(user)
    elite_unlocked = sub.tier in (billing.TIER_ELITE, billing.TIER_FOUNDER)
    if not elite_unlocked:
        for col in notif_push.ELITE_ONLY_TOGGLES:
            v = getattr(body, col, None)
            if v:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "tier_required",
                        "required_tier": "elite",
                        "current_tier": sub.tier,
                        "toggle": col,
                    },
                )

    # Block paid SMS toggles unless the destination phone has been
    # verified end-to-end (task #191). Users who change the phone in
    # the same call are also blocked, since the change wipes the
    # previous proof and the new number isn't verified yet.
    sms_enabling = [
        col for col in notif_push.SMS_PREF_COLUMNS
        if bool(getattr(body, col, None))
    ]
    if sms_enabling:
        current = await notif_push.get_prefs(user.user_id)
        cur_phone = (current.get("sms_phone_number") or "")
        next_phone = (
            (body.sms_phone_number or "").strip()
            if body.sms_phone_number_present
            else cur_phone
        )
        verified = bool(current.get("sms_phone_verified")) and (
            not body.sms_phone_number_present or next_phone == cur_phone
        )
        if not verified:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "sms_phone_not_verified",
                    "toggles": sorted(sms_enabling),
                },
            )

    toggles: dict[str, bool | None] = {}
    for col in notif_push.PREF_COLUMNS:
        toggles[col] = getattr(body, col, None)

    extra: dict[str, object] = {}
    if body.sms_phone_number_present:
        extra["sms_phone_number"] = body.sms_phone_number
    if body.quiet_hours_start_hour_present:
        extra["quiet_hours_start_hour"] = body.quiet_hours_start_hour
    if body.quiet_hours_end_hour_present:
        extra["quiet_hours_end_hour"] = body.quiet_hours_end_hour
    if body.quiet_hours_tz_present:
        extra["quiet_hours_tz"] = body.quiet_hours_tz
    try:
        prefs_after = await notif_push.set_prefs(
            clerk_user_id=user.user_id,
            **extra,
            **toggles,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Auto-issue a verification SMS as soon as a new (unverified) number
    # is saved so the user lands directly on the "enter the code" step
    # without an extra round trip. Gated on Elite tier so non-paying
    # accounts can't repeatedly save phone numbers to burn Twilio spend
    # on a channel they cannot use anyway — mirrors the explicit gate
    # on /sms/start-verification.
    if (
        elite_unlocked
        and body.sms_phone_number_present
        and prefs_after.get("sms_phone_number")
        and not prefs_after.get("sms_phone_verified")
    ):
        await _send_sms_verification(user.user_id)

    return await _notification_prefs_payload(user)


async def _send_sms_verification(clerk_user_id: str) -> dict[str, object]:
    issued = await notif_push.issue_phone_verification_code(clerk_user_id)
    if not issued:
        raise HTTPException(
            status_code=400,
            detail={"error": "no_phone_on_file"},
        )
    code, phone, expires_at = issued
    body = (
        f"Axiom verification code: {code}. "
        f"Enter it on /system?tab=settings to confirm this number. "
        f"Expires in {notif_push.SMS_VERIFICATION_TTL_SECONDS // 60} min."
    )
    sent = await notif_sms.send([phone], body, tag="phone_verify")
    return {
        "sent": sent,
        "expires_at": expires_at.isoformat(),
        "twilio_configured": notif_sms.is_configured(),
    }


class SmsVerifyRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=10)


async def _require_elite_for_sms(user: AuthedUser) -> None:
    """Reject verification calls from non-Elite users.

    SMS dispatch is an Elite-only premium channel; allowing free or Pro
    users to trigger verification SMS would expose us to Twilio spend
    from accounts that can't actually use the channel. The auth-gate
    mirrors the toggle-side check in PUT /api/me/notification-prefs.
    """
    sub = await billing.get_user_subscription(user)
    if sub.tier not in (billing.TIER_ELITE, billing.TIER_FOUNDER):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "tier_required",
                "required_tier": "elite",
                "current_tier": sub.tier,
            },
        )


@app.post("/api/me/notification-prefs/sms/start-verification")
async def me_sms_start_verification(
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    """Send (or re-send) the 6-digit verification code via SMS.

    Idempotent — each call rotates the code and resets the expiry, so
    repeated taps from a user who didn't receive the first text just
    issue a fresh challenge.
    """
    await _require_elite_for_sms(user)
    prefs = await notif_push.get_prefs(user.user_id)
    if not prefs.get("sms_phone_number"):
        raise HTTPException(
            status_code=400, detail={"error": "no_phone_on_file"}
        )
    if prefs.get("sms_phone_verified"):
        return {"ok": True, "already_verified": True}
    result = await _send_sms_verification(user.user_id)
    # Don't surface raw issuance internals to the client — only the
    # high-level "we sent it" + expiry hint for UI countdown.
    return {
        "ok": True,
        "expires_at": result.get("expires_at"),
        "twilio_configured": result.get("twilio_configured"),
    }


@app.post("/api/me/notification-prefs/sms/verify")
async def me_sms_verify(
    body: SmsVerifyRequest,
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    await _require_elite_for_sms(user)
    ok = await notif_push.confirm_phone_verification_code(
        user.user_id, body.code
    )
    if not ok:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_or_expired_code"},
        )
    payload = await _notification_prefs_payload(user)
    payload["ok"] = True
    return payload


# ---------------------------------------------------------------------------
# Telegram concierge binding (per task #111)
# ---------------------------------------------------------------------------


def _binding_public_view(row: dict[str, object] | None) -> dict[str, object]:
    if not row:
        return {"linked": False, "bot_username": telegram_bot.bot_username()}
    bound_at = row.get("bound_at")
    notify_push = row.get("notify_push")
    return {
        "linked": bound_at is not None,
        "telegram_user_id": row.get("telegram_user_id"),
        "telegram_username": row.get("telegram_username"),
        "bound_at": bound_at,
        "last_seen_at": row.get("last_seen_at"),
        "pending_link_code": row.get("link_code") if not bound_at else None,
        "pending_link_expires_at": (
            row.get("link_code_expires_at") if not bound_at else None
        ),
        # `notify_push` mirrors the per-binding mute flag toggled by the
        # bot's /mute and /unmute commands (task #186). Defaults to True
        # when the column is null so legacy rows behave as opted-in.
        "notify_push": True if notify_push is None else bool(notify_push),
        "bot_username": telegram_bot.bot_username(),
        "bot_running": telegram_bot.is_running(),
    }


@app.get("/api/me/telegram")
async def me_telegram_status(
    user: AuthedUser = Depends(require_approved_user),
) -> dict[str, object]:
    """Current Telegram binding for the calling operator."""
    row = await telegram_bindings.get_binding_by_clerk(user.user_id)
    return _binding_public_view(row)


@app.post("/api/me/telegram/link")
async def me_telegram_issue_link(
    user: AuthedUser = Depends(require_approved_user),
) -> dict[str, object]:
    """Issue a fresh short-lived link code.

    The operator pastes the returned ``code`` into Telegram as
    ``/start <code>`` and the bot binds the chat to this Clerk user.
    Calling this again before the prior code is used invalidates the
    earlier code (the row carries a single pending code at a time).
    """
    row = await telegram_bindings.issue_link_code(user.user_id)
    out = _binding_public_view(row)
    # Surface the new code at the top level so the dashboard doesn't have
    # to look it up under pending_link_code (the read endpoint hides it
    # once the code has been consumed).
    out["code"] = row.get("link_code")
    out["expires_at"] = row.get("link_code_expires_at")
    return out


@app.delete("/api/me/telegram")
async def me_telegram_unbind(
    user: AuthedUser = Depends(require_approved_user),
) -> dict[str, object]:
    removed = await telegram_bindings.unbind(user.user_id)
    return {"ok": True, "removed": removed}


class TelegramNotifyPushRequest(BaseModel):
    enabled: bool


@app.post("/api/me/telegram/notify-push")
async def me_telegram_set_notify_push(
    body: TelegramNotifyPushRequest,
    user: AuthedUser = Depends(require_approved_user),
) -> dict[str, object]:
    """Dashboard equivalent of the bot's /mute and /unmute commands.

    Toggles ``telegram_bindings.notify_push`` for the calling operator's
    binding (task #186 added the column). Returns the refreshed binding
    view so the dashboard can re-render without a follow-up GET. Returns
    HTTP 404 when the operator has no Telegram binding yet — there is
    nothing to mute until they finish linking via /start.
    """
    ok = await telegram_bindings.set_notify_push(user.user_id, body.enabled)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail={"error": "telegram_binding_not_found"},
        )
    row = await telegram_bindings.get_binding_by_clerk(user.user_id)
    return _binding_public_view(row)


# ---------------------------------------------------------------------------
# Admin console
# ---------------------------------------------------------------------------


class AdminSetApprovedRequest(BaseModel):
    approved: bool


class AdminSetAdminRequest(BaseModel):
    is_admin: bool


@app.get("/api/admin/users")
async def admin_list_users(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    return {"items": await admin_module.list_users()}


@app.get("/api/admin/watchdog")
async def admin_watchdog_status(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Proxy the L2 watchdog's status snapshot to the operator console.

    The watchdog process binds to ``127.0.0.1:${WATCHDOG_PORT:-8099}`` and
    is not directly exposed by the artifact's reverse proxy, so the
    operator console reaches it through this admin endpoint. Returns
    ``{"available": False, "error": ...}`` if the watchdog is not running
    (e.g. the API server was started without it).
    """
    import httpx as _httpx

    host = os.environ.get("WATCHDOG_HOST", "127.0.0.1")
    port = int(os.environ.get("WATCHDOG_PORT", "8099"))
    url = f"http://{host}:{port}/status"
    try:
        async with _httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return {
                "available": False,
                "error": f"watchdog returned HTTP {r.status_code}",
                "endpoint": url,
            }
        return {"available": True, "endpoint": url, "status": r.json()}
    except Exception as e:  # noqa: BLE001
        return {
            "available": False,
            "error": f"{type(e).__name__}: {e}",
            "endpoint": url,
        }


@app.get("/api/admin/rc-webhook/stats")
async def admin_rc_webhook_stats(
    hours: int = 24,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Last `hours` of RevenueCat webhook deliveries for the admin tile.

    Returns hourly counts split by status (ok / duplicate / 4xx / 5xx),
    aggregate totals, the last delivery timestamp, and the configured
    alerting thresholds so the operator can sanity-check both the
    inbound traffic and the alert config in one place.
    """
    return await rc_billing.get_webhook_stats(hours=hours)


@app.get("/api/admin/rc-webhook/failed-events")
async def admin_rc_webhook_failed_events(
    limit: int = 20,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Recent RC webhook event_ids whose latest delivery was a 5xx.

    Drives the operator-console retry table — each row carries the last
    failure time, attempt count, last error message, and whether we
    have the raw payload stored to feed back into ``_apply_event``.
    """
    items = await rc_billing.list_failed_events(limit=limit)
    return {"items": items}


@app.post("/api/admin/rc-webhook/retry/{event_id}")
async def admin_rc_webhook_retry(
    event_id: str,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Replay a stored RevenueCat webhook event through ``_apply_event``.

    Pulled from the dashboard's failed-events table so an operator can
    clear a stuck delivery without grepping logs and re-POSTing the
    payload by hand. The action is recorded in ``admin_audit_log`` with
    the apply outcome (ok / failed / missing) so it's auditable after
    the fact.
    """
    actor_email = (await check_approval(actor))[1]
    result = await rc_billing.retry_event(event_id)

    # Audit the action regardless of outcome. ``target_user_id`` is the
    # Clerk user id from the stored RC subscription when we have it,
    # otherwise the actor — the audit table requires NOT NULL.
    target_user_id = (
        str(result.get("user_id")) if result.get("user_id") else actor.user_id
    )
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor.user_id,
                actor_email,
                target_user_id,
                None,
                "rc_webhook_retry",
                json.dumps({"event_id": event_id}),
                json.dumps(
                    {
                        "status": result.get("status"),
                        "event_id": result.get("event_id") or event_id,
                        "event_type": result.get("event_type"),
                        "error": result.get("error"),
                    }
                ),
            )
    except Exception as e:
        log.warning("rc retry audit log insert failed: %s", e)

    if result.get("status") == "missing":
        raise HTTPException(
            status_code=404,
            detail=str(result.get("error") or "no stored payload for event"),
        )
    if result.get("status") == "failed":
        raise HTTPException(
            status_code=502,
            detail=str(result.get("error") or "retry apply failed"),
        )
    return result


@app.get("/api/admin/users/{clerk_user_id}")
async def admin_get_user(
    clerk_user_id: str,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    detail = await admin_module.get_user_detail(clerk_user_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="user not found")
    return detail


@app.post("/api/admin/users/{clerk_user_id}/approved")
async def admin_set_user_approved(
    clerk_user_id: str,
    body: AdminSetApprovedRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    actor_email = (await check_approval(actor))[1]
    try:
        return await admin_module.set_approved(
            actor_user_id=actor.user_id,
            actor_email=actor_email,
            target_user_id=clerk_user_id,
            approved=bool(body.approved),
        )
    except admin_module.AdminInvariantError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/admin/users/{clerk_user_id}/admin")
async def admin_set_user_admin(
    clerk_user_id: str,
    body: AdminSetAdminRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    actor_email = (await check_approval(actor))[1]
    return await admin_module.set_admin(
        actor_user_id=actor.user_id,
        actor_email=actor_email,
        target_user_id=clerk_user_id,
        is_admin=bool(body.is_admin),
    )


class AdminAccessRequestDecision(BaseModel):
    decision: str = Field(..., pattern="^(approve|deny|undeny)$")


@app.get("/api/admin/access-requests")
async def admin_list_access_requests(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Users who hit `/api/me/access` while unapproved and have not been
    explicitly denied. Drives the operator-console "Access requests" panel.
    """
    return {"items": await admin_module.list_pending_access_requests()}


@app.get("/api/admin/access-requests/denied")
async def admin_list_denied_access_requests(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Users an operator previously denied. Drives the "Denied" sub-list
    in the operator console so a denial can be undone (re-invite).
    """
    return {"items": await admin_module.list_denied_access_requests()}


@app.post("/api/admin/access-requests/{clerk_user_id}")
async def admin_decide_access_request(
    clerk_user_id: str,
    body: AdminAccessRequestDecision,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    actor_email = (await check_approval(actor))[1]
    try:
        if body.decision == "approve":
            return await admin_module.set_approved(
                actor_user_id=actor.user_id,
                actor_email=actor_email,
                target_user_id=clerk_user_id,
                approved=True,
            )
        if body.decision == "undeny":
            return await admin_module.clear_denied(
                actor_user_id=actor.user_id,
                actor_email=actor_email,
                target_user_id=clerk_user_id,
            )
        return await admin_module.set_denied(
            actor_user_id=actor.user_id,
            actor_email=actor_email,
            target_user_id=clerk_user_id,
        )
    except admin_module.AdminInvariantError as e:
        raise HTTPException(status_code=409, detail=str(e))


class AdminSetAggressionModeRequest(BaseModel):
    aggression_mode: str = Field(
        ..., pattern="^(conservative|balanced|aggressive)$"
    )
    # Optional free-form note recorded alongside the audit row so the
    # runtime panel can surface *why* operators flipped the mode.
    reason: str | None = Field(default=None, max_length=500)
    # Optional auto-revert: if `revert_at` is set we schedule the system
    # to flip back to `revert_to_mode` (default: balanced) at that
    # timestamp.  The next call to `get_state()` applies due reverts.
    revert_at: str | None = None
    revert_to_mode: str | None = Field(
        default=None, pattern="^(conservative|balanced|aggressive)$"
    )


@app.get("/api/admin/runtime-settings")
async def admin_get_runtime_settings(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Snapshot of operator-tunable runtime knobs (aggression_mode)."""
    return await runtime_settings_mod.get_state()


@app.post("/api/admin/runtime-settings")
async def admin_set_runtime_settings(
    body: AdminSetAggressionModeRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Switch the global aggression mode.  The change is picked up on
    the next orchestrator cycle (no restart required) and immediately
    retunes the moderator/gatekeeper bias plus min-edge / Kelly defaults.

    An optional ``reason`` note + ``revert_at`` schedule are recorded so
    the runtime panel can show *why* the mode was changed and auto-flip
    back to ``revert_to_mode`` (default Balanced) when the window ends.
    """
    actor_email = (await check_approval(actor))[1]
    try:
        await runtime_settings_mod.set_aggression_mode(
            body.aggression_mode,
            actor_user_id=actor.user_id,
            actor_email=actor_email,
            reason=body.reason,
            revert_at=body.revert_at,
            revert_to_mode=body.revert_to_mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await runtime_settings_mod.get_state()


@app.delete("/api/admin/runtime-settings/revert")
async def admin_cancel_aggression_revert(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Cancel a pending scheduled aggression-mode auto-revert."""
    actor_email = (await check_approval(actor))[1]
    await runtime_settings_mod.cancel_pending_revert(
        actor_user_id=actor.user_id, actor_email=actor_email
    )
    return await runtime_settings_mod.get_state()


# ---------------------------------------------------------------------------
# Gatekeeper model selector — operator picks which provider/model the LLM
# gatekeeper uses without redeploying.  Read by `swarm.runner` on every
# debate; falls back to the persona default when no override is set.
# ---------------------------------------------------------------------------
class AdminSetGatekeeperModelRequest(BaseModel):
    # Empty string / null on either field reverts to the persona default.
    provider: str | None = Field(default=None, max_length=32)
    model_id: str | None = Field(default=None, max_length=128)


@app.get("/api/admin/runtime-settings/gatekeeper-model")
async def admin_get_gatekeeper_model(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Current gatekeeper model + the available catalogue (with
    per-provider availability flags so the UI can grey out options
    whose API key is not configured)."""
    return await runtime_settings_mod.get_gatekeeper_model_state()


@app.post("/api/admin/runtime-settings/gatekeeper-model")
async def admin_set_gatekeeper_model(
    body: AdminSetGatekeeperModelRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Set or clear the gatekeeper model override.

    Pass an empty/null ``provider`` (or ``model_id``) to revert to the
    persona default.  Otherwise the pair must be a curated catalogue
    entry whose API key is currently configured — anything else returns
    400 so a misconfiguration cannot silently disable the gatekeeper.
    """
    actor_email = (await check_approval(actor))[1]
    try:
        return await runtime_settings_mod.set_gatekeeper_model(
            body.provider,
            body.model_id,
            actor_user_id=actor.user_id,
            actor_email=actor_email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class AdminAggressionPresetRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=60)
    target_mode: str = Field(
        ..., pattern="^(conservative|balanced|aggressive)$"
    )
    reason: str | None = Field(default=None, max_length=500)
    # Optional auto-revert window expressed in minutes so a preset can
    # carry "go aggressive for 4h" without re-deriving a wall-clock
    # timestamp every time it's applied.
    revert_minutes: int | None = Field(default=None, ge=1, le=14 * 24 * 60)


@app.post("/api/admin/runtime-settings/presets")
async def admin_add_aggression_preset(
    body: AdminAggressionPresetRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Save a one-click preset (label + target mode + reason + optional
    revert duration) the runtime panel can apply with a single tap."""
    actor_email = (await check_approval(actor))[1]
    try:
        await runtime_settings_mod.add_aggression_preset(
            label=body.label,
            target_mode=body.target_mode,
            reason=body.reason,
            revert_minutes=body.revert_minutes,
            actor_user_id=actor.user_id,
            actor_email=actor_email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await runtime_settings_mod.get_state()


@app.delete("/api/admin/runtime-settings/presets/{preset_id}")
async def admin_delete_aggression_preset(
    preset_id: str,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Remove a saved aggression-mode preset by id."""
    actor_email = (await check_approval(actor))[1]
    try:
        removed = await runtime_settings_mod.delete_aggression_preset(
            preset_id,
            actor_user_id=actor.user_id,
            actor_email=actor_email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if removed is None:
        raise HTTPException(status_code=404, detail="preset not found")
    return await runtime_settings_mod.get_state()


@app.get("/api/admin/audit-log")
async def admin_get_audit_log(
    limit: int = 100,
    offset: int = 0,
    target: str | None = None,
    actor_filter: str | None = Query(default=None, alias="actor"),
    action: str | None = None,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    # Fetch the page and the total in parallel: operators want to see
    # "Showing 1–50 of 312" so they know whether further paging is worthwhile,
    # and the Next button should be disabled on the actual last page.
    items, total = await asyncio.gather(
        admin_module.list_audit_log(
            limit,
            offset,
            target=target,
            actor=actor_filter,
            action=action,
        ),
        admin_module.count_audit_log(
            target=target,
            actor=actor_filter,
            action=action,
        ),
    )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "target": target,
        "actor": actor_filter,
        "action": action,
    }


@app.get("/api/admin/audit-log/actions")
async def admin_get_audit_log_actions(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    return {"items": await admin_module.list_audit_log_actions()}


@app.get("/api/admin/audit-log.csv")
async def admin_export_audit_log_csv(
    target: str | None = None,
    actor_filter: str | None = Query(default=None, alias="actor"),
    action: str | None = None,
    limit: int = 10000,
    actor: AuthedUser = Depends(require_admin),
) -> Response:
    """CSV export of the audit log honouring the same filters as the
    JSON listing.  Ships a `text/csv` attachment so the browser triggers
    a download and the operator can attach the slice to a compliance
    ticket without needing direct DB access (task #292).
    """
    csv_text = await admin_module.export_audit_log_csv(
        target=target,
        actor=actor_filter,
        action=action,
        limit=int(limit),
    )
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"axiom-audit-log-{stamp}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "content-disposition": f'attachment; filename="{filename}"',
            "cache-control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Early-access (landing) leads — admin browse + CSV export (task #125)
# ---------------------------------------------------------------------------


@app.get("/api/admin/landing-leads")
async def admin_list_landing_leads(
    interest: str | None = None,
    since: str | None = None,
    until: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Paginated `landing_leads` rows for the admin "Early-access leads" page.

    `interest` matches case-insensitively; pass `__none__` for rows
    without an interest selected.  `since`/`until` are inclusive/exclusive
    ISO timestamps (or `YYYY-MM-DD` — Postgres will coerce).  `search`
    is a case-insensitive substring on `email`.
    """
    return await dashboard.list_landing_leads(
        interest=interest,
        since=since,
        until=until,
        search=search,
        limit=int(limit),
        offset=int(offset),
    )


@app.get("/api/admin/landing-leads/interests")
async def admin_list_landing_lead_interests(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Distinct interest values currently in `landing_leads` (filter UI)."""
    return {"items": await dashboard.list_landing_lead_interests()}


@app.get("/api/admin/landing-leads.csv")
async def admin_export_landing_leads_csv(
    interest: str | None = None,
    since: str | None = None,
    until: str | None = None,
    search: str | None = None,
    limit: int = 10000,
    actor: AuthedUser = Depends(require_admin),
) -> Response:
    """CSV export honouring the same filters as the JSON listing."""
    csv_text = await dashboard.export_landing_leads_csv(
        interest=interest,
        since=since,
        until=until,
        search=search,
        limit=int(limit),
    )
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"axiom-landing-leads-{stamp}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "content-disposition": f'attachment; filename="{filename}"',
            "cache-control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Tech / AI Kelly auto-promote (task #199)
# ---------------------------------------------------------------------------


@app.get("/api/dashboard/tech-ai/markets/{ticker}/evidence")
async def dashboard_tech_ai_market_evidence(
    ticker: str,
    lookback_days: int = 30,
    limit: int = 25,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Surface the evidence rows pinned to a tech-AI market.

    Powers the dashboard's "evidence pinned" panel: parses the ticker,
    pulls recent rows from `tech_evidence` scoped to the symbol class,
    and returns them alongside the current Bayesian probability so the
    UI can render the model output next to the underlying signals.
    """
    from .tech_ai import predictor as _ta_predictor
    from .tech_ai.tickers import InvalidTechAITicker, parse_tech_ai_ticker
    from .tech_ai.evidence import resolve_evidence
    try:
        parsed = parse_tech_ai_ticker(ticker)
    except InvalidTechAITicker as e:
        raise HTTPException(status_code=400, detail=f"invalid tech-ai ticker: {e}")
    lookback_days = max(1, min(180, int(lookback_days)))
    limit = max(1, min(100, int(limit)))
    rows = await resolve_evidence(parsed, lookback_days=lookback_days, limit=limit)
    try:
        pred = await _ta_predictor.predict_tech_ai_binary(
            parsed, league="TECH_AI", lookback_days=lookback_days
        )
        prediction = pred.as_dict()
    except Exception as e:
        log.debug("tech-ai dashboard predict failed for %s: %s", ticker, e)
        prediction = None
    return {
        "ticker": {
            "raw": parsed.raw,
            "symbol": parsed.symbol,
            "direction": parsed.direction,
            "threshold": parsed.threshold,
            "when": parsed.when,
        },
        "lookback_days": lookback_days,
        "evidence": [
            {
                "source": r.source,
                "signal_id": r.signal_id,
                "symbol": r.symbol,
                "kind": r.kind,
                "observed_at": r.observed_at.isoformat() if r.observed_at else None,
                "weight": r.weight,
                "payload": r.payload,
            }
            for r in rows
        ],
        "prediction": prediction,
    }


@app.get("/api/admin/tech-ai/promotion-status")
async def admin_tech_ai_promotion_status(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Return the current tech-AI Kelly promotion eligibility snapshot."""
    from .tech_ai import promotion as _tech_ai_promotion
    status = await _tech_ai_promotion.evaluate_promotion()
    return status.as_dict()


@app.post("/api/admin/tech-ai/promote-kelly")
async def admin_tech_ai_promote_kelly(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Promote the tech-AI Kelly multiplier from 0.06 to 0.10.

    Refuses unless the latest evaluation reports ``eligible=True``
    (>= 30 settled trades and rolling Brier < 0.20).  When eligible,
    writes ``risk.kelly_multiplier_tech_ai = 0.10`` into
    ``system_settings`` and records an audit row.
    """
    from .tech_ai import promotion as _tech_ai_promotion
    status = await _tech_ai_promotion.promote(
        actor_user_id=actor.user_id,
        actor_email=getattr(actor, "email", None),
    )
    if not status.eligible and status.current_multiplier < _tech_ai_promotion.PROMOTED_MULTIPLIER:
        raise HTTPException(status_code=409, detail={
            "error": "not_eligible",
            "status": status.as_dict(),
        })
    return status.as_dict()


# ---------------------------------------------------------------------------
# Elections vertical (task #201) — dashboard arb panel + Kelly auto-promote
# ---------------------------------------------------------------------------


@app.get("/api/dashboard/elections/markets/{ticker}/arb")
async def dashboard_elections_market_arb(
    ticker: str,
    threshold_pp: float = 4.0,
    lookback_days: int = 60,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Surface the Kalshi-vs-Polymarket arb panel for an election market.

    Powers the dashboard's side-by-side arb panel: parses the ticker,
    pulls the latest Kalshi market price + the freshest Polymarket
    cross-reference row from `election_polls`, runs the arb detector
    and returns the spread alongside the model prediction so the UI
    can render a one-click "send to Kalshi" call-to-action.
    """
    from .elections.tickers import (
        InvalidElectionTicker,
        parse_election_ticker,
    )
    from .elections.polls import resolve_polls
    from .elections.arb import detect_arb
    from .elections import predictor as _elections_predictor
    try:
        parsed = parse_election_ticker(ticker)
    except InvalidElectionTicker as e:
        raise HTTPException(status_code=400, detail=f"invalid election ticker: {e}")
    lookback_days = max(1, min(180, int(lookback_days)))
    threshold_pp = max(0.0, min(50.0, float(threshold_pp)))
    polls = await resolve_polls(parsed, lookback_days=lookback_days)

    # Pick the *freshest* polymarket xref row whose candidate matches
    # the ticker's candidate slot — falling back to nothing rather than
    # reading the wrong leg (binary YES/NO markets list both outcomes,
    # multi-outcome markets list every candidate; picking the first
    # row would frequently invert the arb direction).
    from .elections.model import _candidate_matches as _cand_match
    polymarket_price: float | None = None
    polymarket_meta: dict[str, object] | None = None
    xref_rows = [
        r for r in polls
        if (r.kind or "").lower() == "polymarket_xref"
        and r.support_pct is not None
        and _cand_match(parsed.candidate or "", r.candidate or "")
    ]
    xref_rows.sort(
        key=lambda r: r.observed_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if xref_rows:
        row = xref_rows[0]
        polymarket_price = float(row.support_pct) / 100.0
        polymarket_meta = {
            "observed_at": row.observed_at.isoformat() if row.observed_at else None,
            "candidate": row.candidate,
            "payload": row.payload,
        }

    kalshi_price: float | None = None
    try:
        from . import db as _db
        row = await _db.fetchrow(
            """
            SELECT yes_price_cents
              FROM kalshi_markets
             WHERE market_id = $1
             ORDER BY observed_at DESC
             LIMIT 1
            """,
            parsed.raw,
        )
        if row and row.get("yes_price_cents") is not None:
            kalshi_price = float(row["yes_price_cents"]) / 100.0
    except Exception as e:
        log.debug("kalshi price lookup failed for %s: %s", parsed.raw, e)

    arb = detect_arb(
        market_id=parsed.raw,
        candidate=parsed.candidate,
        kalshi_price=kalshi_price,
        polymarket_price=polymarket_price,
        threshold_pp=threshold_pp,
    )

    try:
        pred = await _elections_predictor.predict_election_binary(
            parsed, league="ELECTIONS", lookback_days=lookback_days
        )
        prediction = pred.as_dict()
    except Exception as e:
        log.debug("elections dashboard predict failed for %s: %s", ticker, e)
        prediction = None

    return {
        "ticker": {
            "raw": parsed.raw,
            "office": parsed.office,
            "candidate": parsed.candidate,
            "region": parsed.region,
            "scope": parsed.scope,
            "when": parsed.when,
        },
        "kalshi": {"price": kalshi_price},
        "polymarket": {"price": polymarket_price, **(polymarket_meta or {})},
        "arb": arb.as_dict(),
        "prediction": prediction,
        "lookback_days": lookback_days,
    }


@app.get("/api/admin/elections/promotion-status")
async def admin_elections_promotion_status(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Return the current elections Kelly promotion eligibility snapshot."""
    from .elections import promotion as _elections_promotion
    status = await _elections_promotion.evaluate_promotion()
    return status.as_dict()


@app.post("/api/admin/elections/promote-kelly")
async def admin_elections_promote_kelly(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Promote the elections Kelly multiplier from 0.05 to 0.08.

    Refuses unless the latest evaluation reports ``eligible=True``
    (>= 30 settled trades and rolling Brier < 0.20).  When eligible,
    writes ``risk.kelly_multiplier_elections = 0.08`` into
    ``system_settings`` and records an audit row.
    """
    from .elections import promotion as _elections_promotion
    status = await _elections_promotion.promote(
        actor_user_id=actor.user_id,
        actor_email=getattr(actor, "email", None),
    )
    if not status.eligible and status.current_multiplier < _elections_promotion.PROMOTED_MULTIPLIER:
        raise HTTPException(status_code=409, detail={
            "error": "not_eligible",
            "status": status.as_dict(),
        })
    return status.as_dict()


# ---------------------------------------------------------------------------
# Events / macro economics vertical (task #202) — admin promotion +
# public upcoming-release calendar.
# ---------------------------------------------------------------------------


@app.get("/api/admin/events-econ/promotion-status")
async def admin_events_econ_promotion_status(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Current events-econ Kelly promotion eligibility snapshot."""
    from .events_econ import promotion as _ee_promotion
    status = await _ee_promotion.evaluate_promotion()
    return status.as_dict()


@app.post("/api/admin/events-econ/promote-kelly")
async def admin_events_econ_promote_kelly(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Promote the events-econ Kelly multiplier from 0.15 to 0.20.

    Refuses unless the latest evaluation reports ``eligible=True``
    (>= 30 settled trades and rolling Brier < 0.20).  When eligible,
    writes ``risk.kelly_multiplier_events_econ = 0.20`` into
    ``system_settings`` and records an audit row.
    """
    from .events_econ import promotion as _ee_promotion
    status = await _ee_promotion.promote(
        actor_user_id=actor.user_id,
        actor_email=getattr(actor, "email", None),
    )
    if not status.eligible and status.current_multiplier < _ee_promotion.PROMOTED_MULTIPLIER:
        raise HTTPException(status_code=409, detail={
            "error": "not_eligible",
            "status": status.as_dict(),
        })
    return status.as_dict()


@app.get("/api/events-econ/calendar")
async def events_econ_calendar(within_hours: float = 168.0, limit: int = 50) -> dict[str, object]:
    """Upcoming economic releases for the dashboard calendar panel.

    Public: no auth — only surfaces release schedule + consensus, all
    of which is already public information from the upstream feeds.

    Each item carries the consensus-prior `model_prob`, the latest
    Kalshi YES mid (when a matching market exists), the implied edge
    in bps, and a countdown / `imminent` flag (set when the release
    is within the 5-minute pre-release blackout window).
    """
    from datetime import datetime, timezone
    from .events_econ.calendar import (
        kalshi_ticker_prefix_for_release,
        settled_history_for_type,
        upcoming_releases,
    )
    from .events_econ.model import at_consensus_probability

    rows = await upcoming_releases(within_hours=within_hours, limit=limit)

    # Cache settled history per release_type for the duration of the
    # request so we don't hammer the DB with N identical lookups when
    # the calendar lists multiple upcoming prints of the same type.
    history_cache: dict[str, list] = {}
    items: list[dict[str, object]] = []
    now = datetime.now(timezone.utc)
    for r in rows:
        if r.release_type not in history_cache:
            history_cache[r.release_type] = await settled_history_for_type(
                r.release_type
            )
        history = history_cache[r.release_type]

        # Model probability: at-consensus baseline given the rolling
        # forecast-error distribution of settled same-type prints.
        # Always deterministic — no synthetic ticker parsing.
        model_prob: float | None = None
        try:
            res = at_consensus_probability(r, history)
            model_prob = float(res.probability)
        except Exception:  # pragma: no cover — never fail the route
            model_prob = None

        # Kalshi mid + edge: scope the snapshot lookup to THIS release
        # instance's canonical ticker prefix (e.g. KXCPI-25MAR-) so we
        # don't surface a mid from an unrelated contract month.
        kalshi_mid: float | None = None
        prefix = kalshi_ticker_prefix_for_release(r.release_type, r.release_at)
        if prefix is not None:
            try:
                mid_raw = await db.fetchval(
                    """
                    SELECT yes_mid_price
                      FROM kalshi_market_snapshots
                     WHERE ticker LIKE $1
                     ORDER BY observed_at DESC
                     LIMIT 1
                    """,
                    f"{prefix}%",
                )
                if mid_raw is not None:
                    kalshi_mid = float(mid_raw)
            except Exception:  # pragma: no cover — snapshots table optional
                kalshi_mid = None

        edge_bps: float | None = None
        if model_prob is not None and kalshi_mid is not None:
            edge_bps = round((model_prob - kalshi_mid) * 10_000.0, 1)

        seconds_until = None
        imminent = False
        if r.release_at is not None:
            release_at = r.release_at
            if release_at.tzinfo is None:
                release_at = release_at.replace(tzinfo=timezone.utc)
            seconds_until = max(0.0, (release_at - now).total_seconds())
            imminent = 0 < seconds_until <= 5 * 60.0

        items.append({
            "source": r.source,
            "signal_id": r.signal_id,
            "release_type": r.release_type,
            "release_at": r.release_at.isoformat() if r.release_at else None,
            "consensus": r.consensus,
            "previous": r.previous,
            "model_prob": model_prob,
            "kalshi_mid": kalshi_mid,
            "kalshi_ticker_prefix": prefix,
            "edge_bps": edge_bps,
            "seconds_until_release": seconds_until,
            "imminent": imminent,
        })
    return {"items": items}


@app.get("/api/events-econ/release-history")
async def events_econ_release_history(
    lookback_days: int = 90,
    limit: int = 20,
) -> dict[str, object]:
    """Recent *settled* macro releases with surprise vs consensus.

    Public: no auth — release schedule and prints are public information.
    Each item carries the actual print, consensus, surprise (actual −
    consensus), and the release timestamp so the dashboard can render
    a "release surprises" panel.
    """
    from .events_econ.calendar import recent_releases

    rows = await recent_releases(
        lookback_days=max(1, int(lookback_days)),
        limit=max(1, min(100, int(limit))),
    )
    items: list[dict[str, object]] = []
    for r in rows:
        surprise: float | None = None
        if r.actual is not None and r.consensus is not None:
            surprise = round(float(r.actual) - float(r.consensus), 6)
        direction: str
        if surprise is None:
            direction = "unknown"
        elif surprise > 0:
            direction = "above"
        elif surprise < 0:
            direction = "below"
        else:
            direction = "inline"
        items.append({
            "source": r.source,
            "signal_id": r.signal_id,
            "release_type": r.release_type,
            "release_at": r.release_at.isoformat() if r.release_at else None,
            "consensus": r.consensus,
            "previous": r.previous,
            "actual": r.actual,
            "revised_previous": r.revised_previous,
            "surprise": surprise,
            "direction": direction,
        })
    return {"items": items}


# ---------------------------------------------------------------------------
# Entertainment vertical (task #203) — admin promotion + public weekend
# release calendar / awards leaderboard.
# ---------------------------------------------------------------------------


@app.get("/api/admin/entertainment/promotion-status")
async def admin_entertainment_promotion_status(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Current entertainment Kelly promotion eligibility snapshot."""
    from .entertainment import promotion as _ent_promotion
    status = await _ent_promotion.evaluate_promotion()
    return status.as_dict()


@app.post("/api/admin/entertainment/promote-kelly")
async def admin_entertainment_promote_kelly(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Promote the entertainment Kelly multiplier from 0.05 to 0.08.

    Refuses unless the latest evaluation reports ``eligible=True``
    (>= 30 settled trades and rolling Brier < 0.22).  When eligible,
    writes ``risk.kelly_multiplier_entertainment = 0.08`` into
    ``system_settings`` and records an audit row.
    """
    from .entertainment import promotion as _ent_promotion
    status = await _ent_promotion.promote(
        actor_user_id=actor.user_id,
        actor_email=getattr(actor, "email", None),
    )
    if not status.eligible and status.current_multiplier < _ent_promotion.PROMOTED_MULTIPLIER:
        raise HTTPException(status_code=409, detail={
            "error": "not_eligible",
            "status": status.as_dict(),
        })
    return status.as_dict()


class EntertainmentBackfillRequest(BaseModel):
    days: int = Field(
        default=730, ge=7, le=365 * 5,
        description="Lookback window in days (7..1825).",
    )


@app.post("/api/admin/entertainment/backfill")
async def admin_entertainment_backfill_start(
    body: EntertainmentBackfillRequest | None = None,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Trigger a one-shot historical backfill of entertainment signals.

    Walks each registered upstream archive (Box Office Mojo weekend,
    Spotify weekly chart download, Rotten Tomatoes browse archive,
    awards historical winners JSON) back ``days`` days (default 730 —
    two years) and upserts the results into ``entertainment_signals``
    so the entertainment hybrid model has calibrated priors from
    cycle 1 instead of falling back to its 0.5 default.

    Idempotent: safe to re-run.  Returns 409 if another backfill is
    already in flight.
    """
    from .entertainment import backfill as _ent_backfill
    if _ent_backfill.is_running():
        raise HTTPException(
            status_code=409,
            detail={
                "error": "backfill_already_running",
                "status": _ent_backfill.get_status(),
            },
        )
    days = (body.days if body is not None
            else _ent_backfill.DEFAULT_BACKFILL_DAYS)

    async def _runner() -> None:
        try:
            await _ent_backfill.run_backfill(days=days)
        except Exception:
            log.exception("entertainment backfill failed")

    asyncio.create_task(_runner(), name="entertainment-backfill")
    return {"started": True, "days": days,
            "status": _ent_backfill.get_status()}


@app.get("/api/admin/entertainment/backfill-status")
async def admin_entertainment_backfill_status(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Snapshot of the in-process backfill plus the most recent
    persisted run (so the dashboard panel survives restarts)."""
    from .entertainment import backfill as _ent_backfill
    persisted = await _ent_backfill.load_persisted_status()
    return {
        "current": _ent_backfill.get_status(),
        "last_persisted": persisted,
    }


@app.get("/api/entertainment/calendar")
async def entertainment_calendar(
    within_hours: float = 168.0,
    limit: int = 50,
) -> dict[str, object]:
    """Upcoming entertainment events for the dashboard calendar panel.

    Public: no auth — only surfaces release schedules + nominee
    placeholders, all of which is already public information from
    the upstream feeds.
    """
    from datetime import datetime, timezone
    from .entertainment.signals import settled_history_for_type, upcoming_signals
    from .entertainment.model import at_baseline_probability
    from .entertainment import backfill as _ent_backfill

    rows = await upcoming_signals(within_hours=within_hours, limit=limit)
    history_cache: dict[str, list] = {}
    items: list[dict[str, object]] = []
    now = datetime.now(timezone.utc)

    # Best-effort: pull a Kalshi mid for each row's matching market so
    # the dashboard can surface model-vs-market expected edge.  Failures
    # degrade silently (kalshi_mid=None).
    try:
        from .entertainment.predictor import snapshot_kalshi_mids as _ent_kalshi_snap
        _kalshi_mids = await _ent_kalshi_snap()
    except Exception:  # pragma: no cover — kalshi snapshot optional
        _kalshi_mids = {}

    for r in rows:
        if r.signal_type not in history_cache:
            history_cache[r.signal_type] = await settled_history_for_type(r.signal_type)
        history = history_cache[r.signal_type]

        model_prob: float | None = None
        try:
            res = at_baseline_probability(r, history)
            model_prob = float(res.probability)
        except Exception:  # pragma: no cover — never fail the route
            log.debug(
                "entertainment calendar at_baseline_probability failed for %s/%s",
                r.source, r.signal_id,
            )
            model_prob = None

        seconds_until = None
        imminent = False
        if r.event_at is not None:
            event_at = r.event_at
            if event_at.tzinfo is None:
                event_at = event_at.replace(tzinfo=timezone.utc)
            seconds_until = max(0.0, (event_at - now).total_seconds())
            imminent = 0 < seconds_until <= 60 * 60.0

        kalshi_key = f"{r.signal_type}:{r.title}".upper()
        kalshi_mid = _kalshi_mids.get(kalshi_key)
        edge_bps: float | None = None
        if model_prob is not None and isinstance(kalshi_mid, (int, float)):
            edge_bps = (float(model_prob) - float(kalshi_mid)) * 10_000.0

        items.append({
            "source": r.source,
            "signal_id": r.signal_id,
            "signal_type": r.signal_type,
            "title": r.title,
            "event_at": r.event_at.isoformat() if r.event_at else None,
            "value": r.value,
            "model_prob": model_prob,
            "kalshi_mid": kalshi_mid,
            "edge_bps": edge_bps,
            "seconds_until_event": seconds_until,
            "imminent": imminent,
        })
    backfill_status = await _ent_backfill.load_persisted_status()
    return {"items": items, "backfill": backfill_status}


@app.get("/api/entertainment/awards-leaderboard")
async def entertainment_awards_leaderboard(
    award_type: str = "OSCAR",
    limit: int = 20,
) -> dict[str, object]:
    """Leading nominees / candidates for an upcoming awards cycle."""
    from .entertainment.signals import awards_leaderboard
    rows = await awards_leaderboard(award_type, limit=limit)
    items = [
        {
            "source": r.source,
            "signal_id": r.signal_id,
            "signal_type": r.signal_type,
            "title": r.title,
            "event_at": r.event_at.isoformat() if r.event_at else None,
            "value": r.value,
        }
        for r in rows
    ]
    return {"award_type": award_type.upper(), "items": items}


# ---------------------------------------------------------------------------
# AutoResearch — UW signal mining + shadow feature promotion
# ---------------------------------------------------------------------------


class AutoResearchDecision(BaseModel):
    decision: str = Field(..., pattern="^(promote|reject|reset)$")


@app.get("/api/admin/autoresearch/features")
async def admin_autoresearch_list(
    status: str | None = None,
    vertical: str | None = None,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """List discovered candidate features (default: all statuses).

    The mining job ranks by |IC| so the most promising candidates are
    returned first. Filter by ``status`` (``shadow``/``production``/
    ``rejected``) and/or ``vertical`` to narrow the view.
    """
    if status is not None and status not in autoresearch.store.VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {autoresearch.store.VALID_STATUSES}",
        )
    items = await autoresearch.store.list_candidates(
        status=status, vertical=vertical
    )
    return {
        "items": items,
        "scheduler": autoresearch.scheduler.status(),
    }


@app.post("/api/admin/autoresearch/features/{name}/decision")
async def admin_autoresearch_decide(
    name: str,
    body: AutoResearchDecision,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Promote, reject, or reset a discovered candidate to ``shadow``.

    Promotion does not retroactively change the live predictor's column
    layout (the trained meta-model has fixed columns); it records the
    operator's intent so the next retrain pipeline picks the feature
    up. Every decision lands in ``admin_audit_log``.
    """
    actor_email = (await check_approval(actor))[1]
    before = await autoresearch.store.get_candidate(name)
    if before is None:
        raise HTTPException(status_code=404, detail="autoresearch feature not found")
    new_status = {
        "promote": "production",
        "reject": "rejected",
        "reset": "shadow",
    }[body.decision]
    after = await autoresearch.store.set_status(
        name=name, status=new_status, actor_user_id=actor.user_id,
    )
    if after is None:
        raise HTTPException(status_code=502, detail="status update failed")
    try:
        import json
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor.user_id,
                actor_email,
                f"autoresearch:{name}",
                None,
                f"autoresearch_{body.decision}",
                json.dumps({
                    "status": before.get("status"),
                    "ic": before.get("ic"),
                    "sharpe": before.get("sharpe"),
                }),
                json.dumps({
                    "status": after.get("status"),
                    "ic": after.get("ic"),
                    "sharpe": after.get("sharpe"),
                }),
            )
    except Exception as e:
        log.warning("autoresearch audit log write failed: %s", e)
    # Refresh the in-process promoted set so the live feature vector
    # reflects this decision on the very next prediction. The matching
    # retrain (which actually teaches the model to use the new column)
    # is a separate offline step — see docs/retrain_runbook.md.
    try:
        from .autoresearch import feature_bridge as _ar_bridge
        from .features import vector as _features_vector
        promoted_rows = await autoresearch.store.list_candidates(
            status="production"
        )
        registered = _ar_bridge.register_candidates(promoted_rows)
        _features_vector.set_promoted_names(registered)
    except Exception as e:
        log.warning("promoted set refresh after status change failed: %s", e)
    return after


@app.post("/api/admin/autoresearch/run")
async def admin_autoresearch_run(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Trigger one mining pass synchronously (handy for backfills + tests)."""
    result = await autoresearch.scheduler.run_now()
    return {
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "duration_seconds": result.duration_seconds,
        "verticals_examined": result.verticals_examined,
        "candidates_evaluated": result.candidates_evaluated,
        "candidates_persisted": result.candidates_persisted,
        "insufficient_samples": result.insufficient_samples,
        "errors": result.errors,
    }


# ---------------------------------------------------------------------------
# Margin-regressor status (admin-only)
# ---------------------------------------------------------------------------


@app.get("/api/admin/margin-regressor/status")
async def admin_margin_regressor_status(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Per-league fit status for the spread/total margin regressors.

    Surfaces ``n_train``, ``last_fit_at`` (ISO-8601 UTC) and the
    persisted residual SDs for each supported league so the operator
    can confirm the settlement-loop refit job is actually keeping the
    in-process registry warm — and how stale each league's coefficients
    are if it has stopped landing fits.
    """
    return margin_regressor.status()


@app.post("/api/admin/margin-regressor/refit")
async def admin_margin_regressor_refit(
    min_games: int = 50,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Trigger one synchronous refit pass.  Mirrors the settlement-loop
    refit but lets an operator force a fresh fit (after a backfill,
    after a model swap, etc.) without waiting for the next cycle."""
    summary = await margin_regressor.refit_all_leagues(min_games=min_games)
    return summary.as_dict()


# ---------------------------------------------------------------------------
# Risk-knob editor (admin-only)
# ---------------------------------------------------------------------------


class AdminSetRiskKnobsRequest(BaseModel):
    """Partial update — keys absent from the payload are left unchanged.

    Setting a key to ``null`` clears the override so the gate falls back
    to the active aggression-mode default.
    """
    overrides: dict[str, float | int | None]


@app.get("/api/admin/risk-knobs")
async def admin_get_risk_knobs(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    return await runtime_settings_mod.get_risk_knob_state()


@app.get("/api/admin/risk-knobs/history")
async def admin_get_risk_knobs_history(
    per_knob_limit: int = Query(10, ge=1, le=50),
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Per-knob mutation history sourced from ``admin_audit_log`` rows
    with ``action='set_risk_knob'``.  Returned shape:
    ``{"history": {knob_key: [{at, actor_user_id, actor_email, before, after}, ...]}}``
    Knobs with no recorded change come back as empty lists.
    """
    history = await runtime_settings_mod.get_risk_knob_history(
        per_knob_limit=per_knob_limit,
    )
    return {"history": history}


@app.post("/api/admin/risk-knobs")
async def admin_set_risk_knobs(
    body: AdminSetRiskKnobsRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    actor_email = (await check_approval(actor))[1]
    try:
        await runtime_settings_mod.set_risk_knob_overrides(
            body.overrides,
            actor_user_id=actor.user_id,
            actor_email=actor_email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await runtime_settings_mod.get_risk_knob_state()


# ---------------------------------------------------------------------------
# Per-vertical persona toggles (admin-only) — Task #194
# ---------------------------------------------------------------------------


class AdminSetPersonaToggleRequest(BaseModel):
    vertical: str = Field(..., min_length=1, max_length=64)
    persona: str = Field(..., min_length=1, max_length=128)
    enabled: bool


@app.get("/api/admin/persona-toggles")
async def admin_get_persona_toggles(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Snapshot of every per-vertical persona profile + the current
    enabled/disabled state.  Drives the operator-console "Personas"
    tab. Verticals not listed in the registry still appear if a stored
    toggle row references them, so a forgotten override never becomes
    invisible.
    """
    from . import verticals as _verticals

    overrides = await swarm_toggles.list_all_overrides()
    override_by_vertical: dict[str, dict[str, bool]] = {}
    for row in overrides:
        override_by_vertical.setdefault(row["vertical"], {})[row["persona"]] = row[
            "enabled"
        ]

    items: list[dict[str, object]] = []
    seen_verticals: set[str] = set()
    for spec in _verticals.all_specs():
        v = spec.name
        seen_verticals.add(v)
        profile = swarm_profiles.get_profile(spec.persona_profile)
        ov = override_by_vertical.get(v, {})
        personas_payload = [
            {
                "id": p.id,
                "display": p.display,
                "provider": p.provider,
                "model_id": p.model_id,
                "enabled": ov.get(p.id, True),
            }
            for p in profile.personas
        ]
        items.append(
            {
                "vertical": v,
                "vertical_label": spec.description or v,
                "persona_profile": profile.name,
                "profile_description": profile.description,
                "personas": personas_payload,
            }
        )
    # Surface dangling overrides (vertical disappeared from the
    # registry) so the operator can clean them up rather than have
    # them silently apply to nothing.
    for v in sorted(set(override_by_vertical.keys()) - seen_verticals):
        items.append(
            {
                "vertical": v,
                "vertical_label": v,
                "persona_profile": None,
                "profile_description": None,
                "personas": [
                    {
                        "id": pid,
                        "display": pid,
                        "provider": None,
                        "model_id": None,
                        "enabled": enabled,
                    }
                    for pid, enabled in sorted(override_by_vertical[v].items())
                ],
                "orphaned": True,
            }
        )
    return {
        "items": items,
        "available_profiles": [
            {"name": p.name, "description": p.description,
             "personas": [pp.id for pp in p.personas]}
            for p in swarm_profiles.PROFILES.values()
        ],
    }


@app.post("/api/admin/persona-toggles")
async def admin_set_persona_toggle(
    body: AdminSetPersonaToggleRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Toggle one persona on/off for one vertical.

    The runner picks the change up on the next debate (no restart
    required) and an ``admin_audit_log`` row is written so the audit
    panel surfaces it next to runtime / risk-knob changes.
    """
    actor_email = (await check_approval(actor))[1]
    try:
        await swarm_toggles.set_toggle(
            body.vertical,
            body.persona,
            enabled=bool(body.enabled),
            actor_user_id=actor.user_id,
            actor_email=actor_email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"vertical": body.vertical,
            "toggles": await swarm_toggles.get_toggles_for_vertical(body.vertical)}


# ---------------------------------------------------------------------------
# Manual cycle trigger (admin-only) + trace
# ---------------------------------------------------------------------------


class AdminRunCycleRequest(BaseModel):
    vertical: str = Field("sports", min_length=1, max_length=64)


@app.get("/api/admin/elo/status")
async def admin_elo_status(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Return the elo updater scheduler status (last cycle counters)."""
    return elo_scheduler.scheduler.status()


@app.post("/api/admin/elo/run-cycle")
async def admin_elo_run_cycle(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Run one elo updater cycle out-of-band and return the counters."""
    cr = await elo_scheduler.scheduler.run_now()
    return {
        "started_at": cr.started_at,
        "completed_at": cr.completed_at,
        "duration_seconds": cr.duration_seconds,
        "games_examined": cr.games_examined,
        "games_updated": cr.games_updated,
        "games_skipped": cr.games_skipped,
        "teams_touched": cr.teams_touched,
        "errors": cr.errors,
    }


@app.post("/api/admin/orchestrator/run-cycle")
async def admin_run_cycle(
    body: AdminRunCycleRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Fire one orchestrator cycle out-of-band and return the result
    plus the pipeline trace events the cycle produced.

    Refuses (503) until the READY gate has flipped, mirroring the
    Pro+ ``/api/orchestrator/run-cycle`` endpoint — without this, an
    operator could trigger a trade against half-loaded predictors.
    """
    if not ready_gate.is_ready():
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", **ready_gate.status()},
        )
    try:
        cr = await run_cycle(body.vertical)
    except Exception as e:
        log.exception("admin manual cycle failed")
        raise HTTPException(status_code=502, detail=f"cycle error: {e}")
    # Pull the trace events the cycle just produced from the in-process
    # ring buffer so the admin UI can render them inline without
    # subscribing to the WS.
    all_recent = pipeline_event_hub.recent_events(max_cycles=10)
    trace = [ev for ev in all_recent if ev.get("cycle_id") == cr.cycle_id]
    return {
        "cycle_id": cr.cycle_id,
        "vertical": cr.vertical,
        "started_at": cr.started_at,
        "completed_at": cr.completed_at,
        "duration_seconds": cr.duration_seconds,
        "picks_evaluated": cr.picks_evaluated,
        "trades_placed": cr.trades_placed,
        "fresh_debates": cr.fresh_debates,
        "errors": cr.errors,
        "trace": trace,
    }


# ---------------------------------------------------------------------------
# Models inventory browser + active-predictor selector (admin-only)
# ---------------------------------------------------------------------------


class AdminSetActiveModelRequest(BaseModel):
    name: str | None = Field(
        None,
        description="Catalog name of the predictor to make active. "
                    "Pass null/omit to clear the override and fall "
                    "back to the static priority chain.",
    )


def _model_inventory_payload() -> list[dict[str, object]]:
    """Build a row per known predictor in the loader catalog.

    Each row carries the version tag, full sha256, on-disk size,
    last-modified time, plus loaded / last-loaded / active flags so
    the admin browser can render a single table without further calls.
    """
    state = models_loader.get_state()
    active = predictor.active_model_info() or {}
    active_name = active.get("name") if active.get("bound") else None
    override = predictor.get_active_override()
    rows: list[dict[str, object]] = []
    for name, meta in sorted(state.predictors.items()):
        loaded_obj = models_loader.get_loaded_predictor(name)
        # `loaded_at` is only known for the currently-bound predictor;
        # for any other smoke-loaded predictor we report the smoke
        # `load_seconds` timestamp via state.loaded[name].
        smoke = state.loaded.get(name) if isinstance(state.loaded, dict) else None
        loaded_seconds = None
        if isinstance(smoke, dict):
            loaded_seconds = smoke.get("load_seconds")
        last_loaded_at = None
        if active_name == name and active.get("loaded_at"):
            last_loaded_at = active.get("loaded_at")
        rows.append({
            "name": name,
            "version": str(meta.get("version") or ""),
            "sha256": str(meta.get("sha256") or ""),
            "size": int(meta.get("size") or 0),
            "mtime": float(meta.get("mtime") or 0.0),
            "loaded": loaded_obj is not None,
            "last_loaded_at": last_loaded_at,
            "smoke_load_seconds": loaded_seconds,
            "is_active": active_name == name,
            "is_override": override == name,
        })
    return rows


@app.get("/api/admin/models")
async def admin_list_models(
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Predictor catalog + active selection state for the admin browser."""
    state = models_loader.get_state()
    return {
        "items": _model_inventory_payload(),
        "ready": bool(state.ready),
        "in_progress": bool(state.in_progress),
        "active_predictor": predictor.active_model_info(),
        "active_override": predictor.get_active_override(),
        "manifest": state.manifest,
    }


@app.post("/api/admin/models/active")
async def admin_set_active_model(
    body: AdminSetActiveModelRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Persist + apply the operator's choice of active predictor.

    Stores the name in ``system_settings.predictor.active_name`` so the
    selection survives a restart, then mirrors it into the in-process
    override and invalidates the cached predictor selection so the
    next ``predict()`` rebinds.  Audited via ``admin_audit_log``.
    """
    name = (body.name or "").strip() or None
    if name is not None:
        state = models_loader.get_state()
        if name not in state.predictors:
            raise HTTPException(
                status_code=400,
                detail=f"unknown predictor name: {name!r}",
            )
    actor_email = (await check_approval(actor))[1]
    before = predictor.get_active_override()
    try:
        if name is None:
            await db.execute(
                "DELETE FROM system_settings WHERE key = 'predictor.active_name'"
            )
        else:
            await db.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES ('predictor.active_name', $1, now())
                ON CONFLICT (key) DO UPDATE SET
                  value = EXCLUDED.value,
                  updated_at = EXCLUDED.updated_at
                """,
                name,
            )
    except Exception as e:
        log.exception("active model persist failed")
        raise HTTPException(status_code=502, detail=f"persist failed: {e}")
    predictor.set_active_override(name)
    try:
        import json
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor.user_id,
                actor_email,
                "system",
                None,
                "set_active_model",
                json.dumps({"name": before}),
                json.dumps({"name": name}),
            )
    except Exception as e:
        log.warning("audit log insert (active model) failed: %s", e)
    # Trigger the GitHub recovery mirror in the background. This is the
    # canonical "make active" promotion event; spec calls for every such
    # promotion to push the artifact + manifest to the private mirror so
    # we can recover from a Supabase outage. Best-effort: never blocks.
    if github_mirror.is_enabled():
        asyncio.create_task(github_mirror.mirror_active_model())
    return {
        "items": _model_inventory_payload(),
        "active_predictor": predictor.active_model_info(),
        "active_override": predictor.get_active_override(),
        "mirror_enabled": github_mirror.is_enabled(),
    }


# ---------------------------------------------------------------------------
# Crypto-binary backtest (admin-only).  Wraps `app.crypto.backtest.run_backtest`
# so the operator dashboard can run a fresh expanding-window backtest on
# demand and render the Brier / log-loss summary plus the reliability bin
# table — early warning that the live crypto stack is drifting versus its
# historical performance.
# ---------------------------------------------------------------------------


from .crypto import backtest as crypto_backtest  # noqa: E402
from .crypto.tickers import (  # noqa: E402
    SUPPORTED_SYMBOLS as _CRYPTO_SYMBOLS,
    SUPPORTED_TIMEFRAMES as _CRYPTO_TIMEFRAMES,
)


@app.get("/api/admin/crypto/backtest")
async def admin_crypto_backtest(
    symbol: str = Query("BTC"),
    timeframe: str = Query("1h"),
    days: int = Query(14, ge=1, le=90),
    lookback: int = Query(50, ge=10, le=500),
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Run an expanding-window backtest against historical OHLCV and
    return the structured `BacktestResult` payload.

    Pulls bars from `crypto_ohlcv` for the requested window, scores
    every bar against the symmetric strike grid, and returns
    accuracy / Brier / log-loss plus the per-bin reliability table.
    """
    sym = symbol.strip().upper()
    if sym not in _CRYPTO_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported symbol {sym!r}; expected one of {list(_CRYPTO_SYMBOLS)}",
        )
    tf = timeframe.strip().lower()
    if tf not in _CRYPTO_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported timeframe {tf!r}; expected one of {list(_CRYPTO_TIMEFRAMES)}",
        )
    try:
        result = await crypto_backtest.run_backtest(
            symbol=sym,
            timeframe=tf,
            days=days,
            lookback=lookback,
        )
    except Exception:
        log.exception("crypto backtest failed")
        raise HTTPException(status_code=502, detail="backtest failed")
    return result.as_dict()


@app.get("/api/health")
async def health_full(_: object = Depends(require_approved_user)) -> dict[str, object]:
    """Full readiness check across DB and Storage."""
    settings = get_settings()
    out: dict[str, object] = {
        "status": "ok",
        "supabase_project": settings.supabase_url,
        "paper_mode": settings.paper_mode,
        "kalshi_env": settings.kalshi_env,
        "secrets_present": {
            "openai": bool(settings.openai_api_key),
            "anthropic": bool(settings.anthropic_api_key),
            "kalshi": bool(settings.kalshi_api_key_id and settings.kalshi_private_key),
        },
        "active_predictor": predictor.active_model_info(),
        "verticals": dashboard._registered_verticals_snapshot(),
        "active_verticals": list(settings.orchestrator_verticals),
    }
    # Postgres
    try:
        ver = await db.fetchval("SELECT version()")
        n_tables = await db.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        )
        out["postgres"] = {"ok": True, "version": ver, "public_tables": int(n_tables)}
    except Exception as e:
        out["status"] = "degraded"
        out["postgres"] = {"ok": False, "error": str(e)}
    # Kalshi reachability
    out["kalshi"] = await kalshi_client_mod.health_probe()
    if not out["kalshi"].get("ok"):
        out["status"] = "degraded"
    # Cluster auto-detect result captured at boot.  Surfaces "your prod
    # creds were rejected by demo — set KALSHI_ENV=prod" loudly here
    # rather than buried in startup logs.
    out["kalshi_cluster"] = kalshi_client_mod.cluster_probe_state()
    if out["kalshi_cluster"].get("checked") and not out["kalshi_cluster"].get("ok"):
        out["status"] = "degraded"
    out["polymarket"] = await polymarket_client_mod.health_probe()
    if not out["polymarket"].get("ok"):
        out["status"] = "degraded"
    pws = polymarket_ws.get_ws_subscriber()
    out["polymarket_ws"] = {
        "connected": bool(pws and pws.connected),
        "last_error": pws.last_error if pws else None,
        "subscribers": polymarket_bus.bus.subscriber_count,
    }
    out["trading_mode"] = settings.trading_mode
    ws = kalshi_ws.get_ws_subscriber()
    out["kalshi_ws"] = {
        "connected": bool(ws and ws.connected),
        "last_error": ws.last_error if ws else None,
        "subscribers": kalshi_bus.subscriber_count,
        "env": settings.kalshi_env,
        "url": settings.kalshi_ws_url,
    }
    out["kill_switch"] = kalshi_lifecycle.kill_switch_state()
    # Hosted log search shipper — surfaces whether structured JSON logs
    # are being forwarded to Better Stack / Loki / etc., plus any recent
    # transport errors so an operator can fix a misconfigured intake
    # before the trace history goes dark.
    out["log_shipper"] = get_shipper_status()
    # Storage
    try:
        entries = await storage.list_path("", limit=5)
        out["storage"] = {
            "ok": True,
            "bucket": settings.supabase_bucket,
            "sample": [{"name": e.name, "folder": e.is_folder} for e in entries],
        }
    except Exception as e:
        out["status"] = "degraded"
        out["storage"] = {"ok": False, "error": str(e)}
    # External data feeds — render a compact summary so /api/health stays
    # one screen.  Full per-feed detail lives at /api/feeds/status.
    try:
        snap = feed_registry.snapshot()
        any_failing = any(
            (s.get("failure_count") or 0) > 0
            and (s.get("last_ok_at") is None
                 or (s.get("last_error_at") or "") > (s.get("last_ok_at") or ""))
            for s in snap
        )
        out["feeds"] = {
            "ok": not any_failing,
            "registered": len(snap),
            "enabled": sum(1 for s in snap if s.get("enabled")),
            "skipped": sum(1 for s in snap if not s.get("enabled")),
            # Per-feed detail (last poll time + last error) so an operator
            # can read /api/health alone and see *which* feed is sick;
            # /api/feeds/status carries the same info plus durations and
            # success/failure counters.
            "items": [
                {
                    "name": s["name"],
                    "enabled": s["enabled"],
                    "skipped_reason": s.get("skipped_reason"),
                    "last_started_at": s.get("last_started_at"),
                    "last_finished_at": s.get("last_finished_at"),
                    "last_ok_at": s.get("last_ok_at"),
                    "last_error_at": s.get("last_error_at"),
                    "last_error": s.get("last_error"),
                    "last_records_written": s.get("last_records_written"),
                    "records_written_total": s.get("records_written_total") or 0,
                    "rate_limited_until": s.get("rate_limited_until"),
                    "rate_limited_count": s.get("rate_limited_count"),
                    # Per-feed counters so the Health page can render
                    # last-pull + error-rate without a second round trip.
                    "success_count": s.get("success_count") or 0,
                    "failure_count": s.get("failure_count") or 0,
                    "poll_count": s.get("poll_count") or 0,
                    "error_rate": (
                        round((s.get("failure_count") or 0) / (s.get("poll_count") or 1), 4)
                        if (s.get("poll_count") or 0) > 0 else 0.0
                    ),
                }
                for s in snap
            ],
        }
        if any_failing:
            out["status"] = "degraded"
    except Exception as e:
        out["feeds"] = {"ok": False, "error": str(e)}
    # Models — startup smoke test: report ready/in-progress/error, the
    # predictor catalog versions, the canonical-predictor load results
    # (each model is actually deserialized at boot), and disk usage.
    state = models_loader.get_state()
    canonical_ok = sum(1 for v in state.loaded.values() if v.get("ok"))
    active = predictor.active_model_info()
    mcheck = state.manifest_check or {}
    manifest_ok = bool(mcheck.get("ok")) if mcheck else False
    bad_files = [m["name"] for m in mcheck.get("missing", [])] + [
        m["name"] for m in mcheck.get("mismatch", [])
    ]
    out["models"] = {
        "ok": (
            state.ready
            and state.error is None
            and canonical_ok > 0
            and manifest_ok
        ),
        "ready": state.ready,
        "in_progress": state.in_progress,
        "error": state.error,
        "elapsed_seconds": (
            round(state.completed_at - state.started_at, 2)
            if state.started_at and state.completed_at else None
        ),
        "predictor_count": len(state.predictors),
        "loaded_canonical": state.loaded,
        "loaded_canonical_ok": canonical_ok,
        "active_predictor": active,
        "active_model_version": active.get("version") if active else None,
        "versions": models_loader.list_predictor_versions(),
        "disk_usage": models_loader.disk_usage_summary(),
        "manifest": state.manifest,
        "manifest_ok": manifest_ok,
        "manifest_check": mcheck,
        "bad_files": bad_files,
    }
    if not state.ready and state.error:
        out["status"] = "degraded"
    if state.ready and canonical_ok == 0:
        out["status"] = "degraded"
    if mcheck and not manifest_ok:
        out["status"] = "degraded"
    return out


# ---------------------------------------------------------------------------
# System health page + Postmark alert notifications
# ---------------------------------------------------------------------------


@app.get("/api/health/snapshot")
async def health_snapshot(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Latest health snapshot — populated on a 30s loop by the health
    collector.  Render this directly on the /health page."""
    return health.get_snapshot()


_DEBUG_PROXY_HEADERS: tuple[str, ...] = (
    # Set by Replit's edge proxy and by virtually every reverse proxy in
    # existence (nginx, GCLB, AWS ALB, Cloudflare, Caddy, ...). Their
    # presence is conclusive evidence that the request was forwarded by
    # an upstream — i.e. it did NOT originate from a loopback shell on
    # this container — even when the TCP peer happens to be 127.0.0.1.
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-real-ip",
    "forwarded",
    # Replit-specific markers added by the edge proxy on every request
    # it forwards to the container.
    "x-request-id",
    "x-replit-user-id",
    "x-replit-user-name",
    "replit-cluster",
)


@app.get("/api/_debug/health-snapshot")
async def health_snapshot_debug(request: Request) -> dict[str, object]:
    """Loopback-only mirror of /api/health/snapshot for in-shell diagnostics.

    Auth-bypass is gated to **two** signals that must BOTH hold:

      1. The request's TCP peer is a loopback address (127.0.0.1 / ::1).
      2. The request carries no proxy/forwarded headers (X-Forwarded-*,
         X-Real-IP, Forwarded, X-Request-Id, Replit-Cluster, etc.).

    Either signal alone is insufficient. In the current Replit deployment
    the edge proxy connects to uvicorn from a non-loopback address, so
    (1) suffices, but (1) alone could become spoofable if the platform
    ever switched to a loopback proxy. (2) catches that case because the
    edge proxy stamps Replit-specific request headers on every forwarded
    request. Operators driving curl from inside the container's shell
    pass both checks.
    """
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=404, detail="not found")
    for header_name in _DEBUG_PROXY_HEADERS:
        if header_name in request.headers:
            raise HTTPException(status_code=404, detail="not found")
    return health.get_snapshot()


@app.get("/api/health/errors")
async def health_errors(
    limit: int = 100,
    since_seconds: float | None = None,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    return {
        "items": health.recent_errors(
            limit=max(1, min(500, int(limit))),
            since_seconds=since_seconds,
        ),
        "errors_24h": health.errors_in_last_24h(),
    }


@app.get("/api/health/model-alerts")
async def list_model_health_alerts(
    ack: bool = False,
    limit: int = 100,
    user: AuthedUser = Depends(require_approved_user),
) -> dict[str, object]:
    """List drift / model-health alerts.

    `ack=false` (default) returns only currently-open alerts —
    `acknowledged=false` AND (snooze_until IS NULL OR snooze_until <= now()).
    `ack=true` returns the full recent log including acknowledged + snoozed
    rows for audit purposes.
    """
    lim = max(1, min(500, int(limit)))
    pool = await db.get_pool()
    if ack:
        rows = await pool.fetch(
            """
            SELECT id, vertical, alert_type, severity, metric_name,
                   metric_value, threshold, message, acknowledged,
                   acknowledged_by, acknowledged_at, snooze_until,
                   created_at,
                   EXTRACT(EPOCH FROM (now() - created_at))::bigint AS age_seconds
              FROM model_health_alerts
             ORDER BY created_at DESC
             LIMIT $1
            """,
            lim,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, vertical, alert_type, severity, metric_name,
                   metric_value, threshold, message, acknowledged,
                   acknowledged_by, acknowledged_at, snooze_until,
                   created_at,
                   EXTRACT(EPOCH FROM (now() - created_at))::bigint AS age_seconds
              FROM model_health_alerts
             WHERE acknowledged = false
               AND (snooze_until IS NULL OR snooze_until <= now())
             ORDER BY created_at DESC
             LIMIT $1
            """,
            lim,
        )
    items: list[dict[str, object]] = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "vertical": r["vertical"],
                "alert_type": r["alert_type"],
                "severity": r["severity"],
                "metric_name": r["metric_name"],
                "metric_value": float(r["metric_value"]) if r["metric_value"] is not None else None,
                "threshold": float(r["threshold"]) if r["threshold"] is not None else None,
                "message": r["message"],
                "acknowledged": bool(r["acknowledged"]),
                "acknowledged_by": r["acknowledged_by"],
                "acknowledged_at": r["acknowledged_at"].isoformat() if r["acknowledged_at"] else None,
                "snooze_until": r["snooze_until"].isoformat() if r["snooze_until"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "age_seconds": int(r["age_seconds"]) if r["age_seconds"] is not None else 0,
            }
        )
    return {"items": items, "count": len(items)}


class ModelAlertAckRequest(BaseModel):
    snooze_minutes: int | None = Field(
        default=None,
        ge=1,
        le=60 * 24 * 7,
        description=(
            "If set, snooze the alert for this many minutes instead of "
            "permanently acknowledging it.  Range: 1..10080 (7d)."
        ),
    )


class AcknowledgeAlertRequest(BaseModel):
    alert_id: int = Field(..., ge=1, description="model_health_alerts.id to acknowledge")
    snooze_minutes: int | None = Field(
        default=None,
        ge=1,
        le=60 * 24 * 7,
        description=(
            "If set, snooze the alert for this many minutes instead of "
            "permanently acknowledging it.  Range: 1..10080 (7d)."
        ),
    )


@app.post("/api/health/acknowledge-alert")
async def acknowledge_model_health_alert(
    body: AcknowledgeAlertRequest,
    user: AuthedUser = Depends(require_approved_user),
) -> dict[str, object]:
    """Dashboard-friendly variant of `/api/health/model-alerts/{id}/ack`
    that takes the alert_id in the request body so the operator console
    can call a single stable URL for the "Acknowledge" button."""
    return await ack_model_health_alert(
        alert_id=body.alert_id,
        body=ModelAlertAckRequest(snooze_minutes=body.snooze_minutes),
        user=user,
    )


@app.post("/api/health/model-alerts/{alert_id}/ack")
async def ack_model_health_alert(
    alert_id: int,
    body: ModelAlertAckRequest | None = None,
    user: AuthedUser = Depends(require_approved_user),
) -> dict[str, object]:
    """Acknowledge or snooze a single model health alert.

    Without a body (or `snooze_minutes` unset), the alert is permanently
    acknowledged.  With `snooze_minutes`, the alert is hidden from the
    open list until that many minutes have elapsed.
    """
    _, email = await check_approval(user)
    operator = email or user.user_id
    snooze_minutes = body.snooze_minutes if body is not None else None
    pool = await db.get_pool()
    if snooze_minutes:
        row = await pool.fetchrow(
            """
            UPDATE model_health_alerts
               SET snooze_until = now() + ($2 || ' minutes')::interval,
                   acknowledged_by = COALESCE(acknowledged_by, $1)
             WHERE id = $3
            RETURNING id, vertical, severity, snooze_until, acknowledged
            """,
            operator, str(int(snooze_minutes)), alert_id,
        )
    else:
        row = await pool.fetchrow(
            """
            UPDATE model_health_alerts
               SET acknowledged = true,
                   acknowledged_by = $1,
                   acknowledged_at = now()
             WHERE id = $2
            RETURNING id, vertical, severity, snooze_until, acknowledged
            """,
            operator, alert_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="alert not found")
    log.info(
        "model_health_alerts %s %s by %s (snooze_minutes=%s)",
        alert_id,
        "snoozed" if snooze_minutes else "acknowledged",
        operator,
        snooze_minutes,
    )
    return {
        "ok": True,
        "id": row["id"],
        "vertical": row["vertical"],
        "severity": row["severity"],
        "acknowledged": bool(row["acknowledged"]),
        "snooze_until": row["snooze_until"].isoformat() if row["snooze_until"] else None,
        "acknowledged_by": operator,
    }


@app.get("/api/notifications/status")
async def notifications_status(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    return {
        "postmark": notif_postmark.status(),
        "recent_sends": notif_postmark.recent_sends(limit=25),
        "suppressions": await notif_suppression.list_recent(limit=50),
        "dedupe_suppressions": notif_alerts.suppression_counts(),
    }


@app.post("/api/notifications/postmark-webhook")
async def notifications_postmark_webhook(request: Request) -> dict[str, object]:
    """Receive bounce / complaint / subscription-change events from Postmark.

    Authenticated via HTTP Basic auth.  Postmark supports embedding
    ``user:pass`` in the configured webhook URL and forwards them as a
    standard ``Authorization: Basic <b64>`` header on every delivery.
    The expected credentials live in ``POSTMARK_WEBHOOK_USER`` and
    ``POSTMARK_WEBHOOK_PASSWORD``; when either is unset the endpoint
    refuses with 503 — same posture as the Stripe and Clerk webhooks
    — so an unconfigured deployment can't be tricked by forged events.
    """
    import base64 as _b64
    import hmac as _hmac

    expected_user = os.environ.get("POSTMARK_WEBHOOK_USER")
    expected_pass = os.environ.get("POSTMARK_WEBHOOK_PASSWORD")
    if not expected_user or not expected_pass:
        raise HTTPException(status_code=503, detail="postmark webhook not configured")

    auth_header = request.headers.get("authorization") or ""
    scheme, _, encoded = auth_header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        raise HTTPException(status_code=401, detail="missing basic auth")
    try:
        decoded = _b64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except Exception:
        raise HTTPException(status_code=401, detail="invalid basic auth")
    user, _, password = decoded.partition(":")
    if not (
        _hmac.compare_digest(user, expected_user)
        and _hmac.compare_digest(password, expected_pass)
    ):
        raise HTTPException(status_code=401, detail="invalid basic auth")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected json object")
    return await notif_suppression.apply_postmark_event(payload)


@app.get("/api/notifications/unsubscribe")
async def notifications_unsubscribe(
    email: str, token: str
) -> dict[str, object]:
    """Public one-click unsubscribe endpoint.

    The link in every user-facing email is HMAC-signed with
    ``AXIOM_UNSUBSCRIBE_SECRET`` so we can verify the request without
    storing per-user tokens.  Returning a JSON body keeps the test
    surface tiny — the marketing site can render a friendlier confirmation
    page if needed.
    """
    if not notif_user_email.verify_unsubscribe_token(email, token):
        raise HTTPException(status_code=400, detail="invalid token")
    ok = await notif_suppression.suppress(
        email, "unsubscribe", detail="user clicked unsubscribe link"
    )
    return {"ok": bool(ok), "email": email, "unsubscribed": True}


_ALERT_TYPES = {
    "halt", "drift", "tier_change", "live_trade", "daily_pnl_summary",
    "weekly_roundup", "persona_daily_brier",
    "churn_digest", "user_payment_failed", "user_will_cancel", "user_canceled",
}


class NotificationsTestRequest(BaseModel):
    type: str = Field(..., description=f"One of: {sorted(_ALERT_TYPES)}")
    dry_run: bool = True


@app.post("/api/notifications/test")
async def notifications_test(
    body: NotificationsTestRequest,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Manually fire each alert type so the operator can verify wiring
    end-to-end without waiting for a real halt / drift / etc.

    `dry_run=True` (default) builds the email and returns the preview
    *without* actually sending — flip to false to send it for real."""
    if body.type not in _ALERT_TYPES:
        raise HTTPException(
            status_code=400, detail=f"unknown alert type {body.type!r}"
        )
    if body.type == "halt":
        return notif_alerts.halt(reason="test halt", dry_run=body.dry_run)
    if body.type == "drift":
        return notif_alerts.drift(
            vertical="sports",
            metric="brier_score_24h",
            value=0.31,
            threshold=0.28,
            note="test drift",
            dry_run=body.dry_run,
        )
    if body.type == "tier_change":
        return notif_alerts.tier_change(
            user_email="test@axiom-predict.app",
            user_id="user_test",
            old_tier="free",
            new_tier="pro",
            status="active",
            dry_run=body.dry_run,
        )
    if body.type == "live_trade":
        return notif_alerts.live_trade(
            market_ticker="TEST-MARKET",
            side="yes",
            action="buy",
            contracts=1,
            price_cents=50,
            order_id="test-order",
            fill_id="test-fill",
            vertical="sports",
            dry_run=body.dry_run,
        )
    if body.type == "user_payment_failed":
        result = notif_user_email.payment_failed(
            email="test@axiom-predict.app",
            user_id="user_test",
            invoice_id="in_test",
            amount_cents=4900,
            currency="usd",
            dry_run=body.dry_run,
        )
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Task):
            return await result
        return result
    if body.type == "user_will_cancel":
        result = notif_user_email.subscription_will_cancel(
            email="test@axiom-predict.app",
            user_id="user_test",
            stripe_subscription_id="sub_test",
            tier="pro",
            dry_run=body.dry_run,
        )
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Task):
            return await result
        return result
    if body.type == "user_canceled":
        result = notif_user_email.subscription_canceled(
            email="test@axiom-predict.app",
            user_id="user_test",
            stripe_subscription_id="sub_test",
            tier="pro",
            dry_run=body.dry_run,
        )
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Task):
            return await result
        return result
    if body.type == "churn_digest":
        return await notif_churn.fire_now(dry_run=body.dry_run)
    if body.type == "persona_daily_brier":
        import datetime as _dt
        return notif_alerts.persona_brier_drift(
            persona_id="skeptic",
            date_utc=_dt.date.today().isoformat(),
            brier=0.34,
            debates=8,
            abs_threshold=0.30,
            jump_threshold=0.10,
            severity="warn",
            reason="absolute",
            baseline=0.22,
            jump=0.12,
            note="test persona brier drift",
            dry_run=body.dry_run,
        )
    if body.type == "weekly_roundup":
        import datetime as _dt
        return notif_alerts.weekly_roundup(
            week_ending_utc=_dt.date.today().isoformat(),
            realized_pnl_cents=12345,
            unrealized_pnl_cents=-678,
            trades_settled=42,
            win_rate_pct=58.3,
            notes="test weekly roundup",
            dry_run=body.dry_run,
        )
    # daily_pnl_summary
    return await daily_summary.fire_now(dry_run=body.dry_run)


@app.get("/api/notifications/daily-summary/preview")
async def notifications_daily_summary_preview(
    date: str | None = None,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Render today's (or a past day's) daily-summary email as HTML for
    the operator console preview pane.

    Re-uses the same `collect_summary` + `daily_pnl_summary(dry_run=True)`
    pipeline the scheduler invokes, so the iframe shows operators the
    exact body subscribers would receive — no out-of-band template
    rendering that could drift from production.

    `date` is an optional `YYYY-MM-DD` (UTC) — defaults to today UTC.
    Past dates simply re-aggregate from the historical tables;
    aggregations that have no data for that day collapse to the same
    "no rows" placeholders the email itself would render, so the
    preview never errors on a quiet day.
    """
    import datetime as _dt
    if date is None or not date.strip():
        target = _dt.datetime.now(tz=_dt.timezone.utc).date()
    else:
        try:
            target = _dt.date.fromisoformat(date.strip())
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="date must be YYYY-MM-DD",
            )
    today_utc = _dt.datetime.now(tz=_dt.timezone.utc).date()
    if target > today_utc:
        raise HTTPException(
            status_code=400,
            detail="date cannot be in the future",
        )
    summary = await daily_summary.collect_summary(target.isoformat())
    rendered = notif_alerts.daily_pnl_summary(**summary, dry_run=True)
    return {
        "date_utc": target.isoformat(),
        "is_today": target == today_utc,
        "subject": rendered.get("subject"),
        "body_preview": rendered.get("body_preview"),
        "html_preview": rendered.get("html_preview"),
        "generated_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
    }


@app.get("/api/feeds/status")
async def feeds_status(_: object = Depends(require_auth)) -> dict[str, object]:
    """Per-feed liveness snapshot for the operator console."""
    return {"items": feed_registry.snapshot()}


@app.post("/api/admin/feeds/{name}/poll-now")
async def admin_feeds_poll_now(
    name: str,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Run a single feed poll out-of-band, bypassing the cadence wait.

    Useful after rotating a key or fixing a configuration: the operator
    no longer has to wait up to ``cadence_seconds`` for the freshness
    card to refresh.  Wraps the poll with the same ``record_start`` /
    ``record_success`` / ``record_failure`` accounting the scheduler
    uses, so metrics stay coherent.
    """
    feed = next(
        (f for f in feed_registry.feeds() if f.name == name),
        None,
    )
    if feed is None:
        raise HTTPException(status_code=404, detail=f"unknown feed {name!r}")
    if not feed.enabled:
        raise HTTPException(
            status_code=409,
            detail=f"feed {name!r} disabled: {feed.skipped_reason or 'unknown reason'}",
        )
    started = await feed_registry.record_start(name)
    try:
        written = await feed.poll()
    except Exception as e:  # noqa: BLE001
        await feed_registry.record_failure(name, started, e)
        raise HTTPException(status_code=502, detail=f"poll failed: {e}") from e
    await feed_registry.record_success(name, started, int(written or 0))
    return {"name": name, "written": int(written or 0)}


@app.get("/api/features/registry")
async def features_registry(
    _: object = Depends(require_auth),
) -> dict[str, object]:
    """Centralised feature catalogue.

    Returns the ordered list of every feature the live predictor
    consumes, with name, source, transform description, dtype, and
    training-time default.  Powers the operator console + lets
    AutoResearch / backtest tools enumerate the same schema as prod.
    """
    return {
        "count": len(feature_registry),
        "items": feature_registry.metadata(),
    }


@app.get("/api/features/drift")
async def features_drift(
    _: object = Depends(require_auth),
) -> dict[str, object]:
    """Live vs training-time distribution drift, per feature.

    Pull-based snapshot of the in-process rolling stats; cheap enough
    to poll from the dashboard.  See ``app.features.drift`` for the
    z-score definition and threshold semantics.
    """
    return feature_drift.report()


# ---------------------------------------------------------------------------
# Dashboard read endpoints
# ---------------------------------------------------------------------------


@app.get("/api/dashboard/overview")
async def dashboard_overview(
    user: AuthedUser = Depends(require_approved_user),
) -> dict[str, object]:
    """Aggregate counts and headline stats for the dashboard landing tab.

    Free callers don't have anything time-sensitive redacted here (the
    overview is a rolled-up snapshot, not a live feed), but we still
    emit the same ``tier`` / ``delayed`` / ``delay_minutes`` shape that
    the list endpoints return so the mobile and web dashboards can
    render the free-tier upsell banner on the very first screen.
    """
    overview = await dashboard.get_overview()
    sub = await billing.get_user_subscription(user)
    delayed = (
        billing._TIER_RANK[sub.tier] < billing._TIER_RANK[billing.TIER_PRO]
    )
    overview["tier"] = sub.tier
    overview["delayed"] = delayed
    overview["delay_minutes"] = (
        free_tier.FREE_TIER_DELAY_MINUTES if delayed else 0
    )
    return overview


@app.get("/api/dashboard/recent-trades")
async def dashboard_recent_trades(
    limit: int = 25,
    user: AuthedUser = Depends(require_approved_user),
) -> dict[str, object]:
    """Recent fills feed.

    Free callers see the same shape, but only rows whose ``executed_at``
    is at least 15 minutes old — the live edge is reserved for paid
    tiers.  Pro+ (and founders) get the unfiltered feed.
    """
    limit = max(1, min(200, int(limit)))
    items = await dashboard.get_recent_trades(limit)
    sub = await billing.get_user_subscription(user)
    delayed = False
    if billing._TIER_RANK[sub.tier] < billing._TIER_RANK[billing.TIER_PRO]:
        items = free_tier.apply_signal_delay(items, timestamp_key="executed_at")
        delayed = True
    return {
        "items": items,
        "tier": sub.tier,
        "delayed": delayed,
        "delay_minutes": free_tier.FREE_TIER_DELAY_MINUTES if delayed else 0,
    }


@app.get("/api/dashboard/swarm-personas")
async def dashboard_swarm_personas(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    return {"items": await dashboard.get_swarm_personas()}


SWARM_PERSONAS_TODAY_LOOKBACK_DAYS = 14


@app.get("/api/dashboard/swarm-personas-today")
async def dashboard_swarm_personas_today(
    date_utc: str | None = Query(
        default=None,
        description=(
            "Optional UTC calendar day (YYYY-MM-DD) to aggregate. "
            f"Bounded to today and the previous {SWARM_PERSONAS_TODAY_LOOKBACK_DAYS} days."
        ),
    ),
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Today's per-persona aggregate (debates, Brier, accuracy, lean
    breakdown) computed from settled `swarm_debates` rows.

    Shares the same aggregator as the daily summary email so operators
    can spot a misbehaving persona before the morning digest arrives.

    Accepts an optional ``date_utc=YYYY-MM-DD`` so operators can scrub
    through recent days inline without waiting for the morning email.
    The window is bounded to today and the previous
    ``SWARM_PERSONAS_TODAY_LOOKBACK_DAYS`` calendar days (UTC).
    """
    from datetime import date, datetime, timedelta, timezone

    if date_utc is not None:
        try:
            requested = datetime.strptime(date_utc, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="date_utc must be formatted as YYYY-MM-DD",
            )
        today: date = datetime.now(tz=timezone.utc).date()
        earliest = today - timedelta(days=SWARM_PERSONAS_TODAY_LOOKBACK_DAYS)
        if requested > today or requested < earliest:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"date_utc must be between {earliest.isoformat()} "
                    f"and {today.isoformat()} (inclusive)"
                ),
            )
        return await dashboard.get_swarm_personas_today(requested.isoformat())
    return await dashboard.get_swarm_personas_today()


@app.get("/api/dashboard/swarm-fallbacks-today")
async def dashboard_swarm_fallbacks_today(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Per-persona count of cross-provider LLM fallbacks today (UTC).

    Operators use this to spot a primary provider that is silently
    degrading: if a single persona's daily fallback count spikes, its
    configured model is failing more often than usual even though the
    swarm still produced an answer via the configured backup.
    """
    return await dashboard.get_swarm_fallbacks_today()


@app.get("/api/dashboard/llm-fallback-rates")
async def dashboard_llm_fallback_rates(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Per-provider LLM fallback rate over the alert's trailing window.

    Surfaces the same data the ``llm-fallback-spike`` alert evaluates so
    operators can watch a provider's failure rate creep up *before* the
    alert actually pages, and confirm "things are healthy" again after
    an incident clears without waiting for the next alert tick.
    """
    return await dashboard.get_provider_fallback_rates()


@app.get("/api/market-title/{ticker}")
async def market_title_lookup(
    ticker: str,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Resolve a Kalshi ticker to a human-readable market title.

    Backed by the in-process + DB cache, so repeated lookups are cheap
    and survive a restart without re-hitting Kalshi.
    """
    from .kalshi.market_titles import get_title as _get_title

    ticker = (ticker or "").strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")
    title = await _get_title(ticker)
    return {"ticker": ticker, "title": title}


@app.get("/api/dashboard/runtime-settings")
async def dashboard_runtime_settings(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Active aggression mode + the per-(persona, league, market_type)
    weights the swarm runner is currently applying.  The dashboard
    surfaces this so operators can see the swarm's bias at a glance."""
    from .swarm import snapshot_persona_weights

    state = await runtime_settings_mod.get_state()
    state["persona_weights"] = await snapshot_persona_weights()
    return state


@app.get("/api/dashboard/trades/{trade_ref}")
async def dashboard_trade_detail(
    trade_ref: str,
    user: AuthedUser = Depends(require_approved_user),
) -> dict[str, object]:
    """Single-trade detail + market-vs-model time series.

    `trade_ref` accepts either the integer `trades.id` or the UUID-ish
    `trades.trade_id` so the operator can deep-link from either column.

    Free callers count one "market evaluation" per distinct market_id
    per UTC day.  The 6th distinct market in a day returns HTTP 402
    with an ``upgrade_required`` payload (see :mod:`app.free_tier`).
    Re-opening a market the user already viewed today is free.
    """
    detail = await dashboard.get_trade_detail(trade_ref)
    if detail is None:
        raise HTTPException(status_code=404, detail="trade not found")

    sub = await billing.get_user_subscription(user)
    if billing._TIER_RANK[sub.tier] < billing._TIER_RANK[billing.TIER_PRO]:
        # Apply the same 15-minute signal delay that the list endpoints
        # use: free callers must not be able to read sub-15-min points
        # by deep-linking into a market.  Trades whose own
        # ``executed_at`` is too fresh are 404'd outright (the row
        # exists, but the signal isn't due to the free tier yet); the
        # series is filtered point-by-point.
        trade_row = detail.get("trade") or {}
        if not free_tier.apply_signal_delay(
            [trade_row], timestamp_key="executed_at"
        ):
            raise HTTPException(status_code=404, detail="trade not found")
        detail["series"] = free_tier.apply_signal_delay(
            detail.get("series") or [], timestamp_key="t"
        )
        detail["delayed"] = True
        detail["delay_minutes"] = free_tier.FREE_TIER_DELAY_MINUTES
        market_id = trade_row.get("market_id")
        if market_id:
            viewed = await free_tier.enforce_market_view_quota(
                user.user_id, str(market_id)
            )
            detail["quota"] = {
                "tier": sub.tier,
                "viewed_today": viewed,
                "limit": free_tier.FREE_TIER_DAILY_MARKET_LIMIT,
            }
    return detail


@app.get("/api/search")
async def dashboard_search(
    q: str = "",
    limit: int = 10,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Global search across trades, debates, and cycles.

    Query syntax: ticker / market id substring, or an ISO date
    (YYYY-MM-DD).  Fuzzy / NL search is intentionally out of scope.
    """
    limit = max(1, min(50, int(limit)))
    return await dashboard.search_global(q, limit)


@app.get("/api/dashboard/nfl-exposure")
async def dashboard_nfl_exposure(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """NFL per-league + per-game + Sunday-window exposure with cap headroom.

    Reads the same `_open_position_aggregates` snapshot the trading
    cycle uses, so the operator dashboard can see how close the next
    bet is to tripping the per-league (5%), per-game (1.5%), or Sunday
    window (3%) sub-caps before it submits.
    """
    return await dashboard.get_nfl_exposure()


@app.get("/api/dashboard/calibration/per-league")
async def dashboard_calibration_per_league(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Most-recent per-league probability calibrators for the operator
    dashboard. Returns one entry per supported league with a sampled
    raw→calibrated curve and the most recent Brier-before / Brier-after
    pair carried in the snapshot's ``config`` JSONB.
    """
    return await dashboard.get_per_league_calibration()


@app.get("/api/dashboard/vertical-performance")
async def dashboard_vertical_performance(
    days: int = 30, _: object = Depends(require_approved_user)
) -> dict[str, object]:
    days = max(1, min(365, int(days)))
    return await dashboard.get_vertical_performance(days)


@app.get("/api/dashboard/verticals")
async def dashboard_verticals_overview(
    days: int = 7, _: object = Depends(require_approved_user)
) -> dict[str, object]:
    """Per-vertical overview card backing the new ``/verticals`` page +
    mobile tab. Returns one entry per registered vertical with rolling
    P&L, trade counts, win-rate, latest 24h Brier, annualized Sharpe,
    open positions, predicted vs realized edge, and the latest cycle
    status — plus the operator-set hidden / pinned visibility lists.
    """
    return await dashboard.get_verticals_overview(days)


@app.get("/api/dashboard/vertical-visibility")
async def dashboard_vertical_visibility(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Read-only view of the operator-set vertical visibility lists.

    Used by the web + mobile dashboard to filter the strip in a way
    that matches what the admin configured, so a hidden vertical
    actually disappears for non-admins.
    """
    return await runtime_settings_mod.get_vertical_visibility()


class AdminSetVerticalVisibilityRequest(BaseModel):
    hidden: list[str] | None = Field(
        default=None,
        description="If set, REPLACES the hidden list. Pass [] to clear.",
    )
    pinned: list[str] | None = Field(
        default=None,
        description="If set, REPLACES the pinned list (order matters).",
    )


@app.post("/api/admin/vertical-visibility")
async def admin_set_vertical_visibility(
    body: AdminSetVerticalVisibilityRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    actor_email = (await check_approval(actor))[1]
    try:
        return await runtime_settings_mod.set_vertical_visibility(
            hidden=body.hidden,
            pinned=body.pinned,
            actor_user_id=actor.user_id,
            actor_email=actor_email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/admin/vertical-visibility")
async def admin_get_vertical_visibility(
    _: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    return await runtime_settings_mod.get_vertical_visibility()


# ---------------------------------------------------------------------------
# Per-vertical halt toggle (Task #271). Surface on the operator console so
# a single bad vertical can be sat out without an EMERGENCY STOP (which
# halts everything) or a manual SQL edit on ``system_settings``.
# ---------------------------------------------------------------------------


class AdminHaltVerticalRequest(BaseModel):
    reason: str | None = Field(
        default=None,
        description="Optional free-text reason surfaced in audit log and pipeline events.",
    )


class AdminResumeVerticalRequest(BaseModel):
    reason: str | None = Field(
        default=None,
        description="Optional free-text reason recorded in the audit log.",
    )


async def _cancel_resting_for_vertical(vertical: str) -> dict[str, int]:
    """Cancel every resting Kalshi order attached to ``vertical``.

    Mirrors the cycle-start stale sweep (task #270) — filter by
    ``rec.request.vertical`` so a halt of one vertical can never cancel
    another's resting orders. Cancel failures are swallowed so a single
    rejected cancel doesn't prevent the halt from taking effect; the
    counts are returned for the audit trail.
    """
    try:
        resting = await kalshi_lifecycle.list_resting_orders()
    except Exception as e:
        log.warning("halt_vertical: list_resting_orders failed: %s", e)
        return {"scanned": 0, "matched": 0, "cancelled": 0, "failed": 0}

    matched = [
        r for r in resting
        if (getattr(r.request, "vertical", None) or "").strip().lower() == vertical
    ]
    cancelled = 0
    failed = 0
    for rec in matched:
        try:
            await kalshi_lifecycle.cancel_order(rec.order_id)
            cancelled += 1
        except Exception as e:
            failed += 1
            log.warning(
                "halt_vertical cancel failed for %s (%s): %s",
                rec.order_id, vertical, e,
            )
    return {
        "scanned": len(resting),
        "matched": len(matched),
        "cancelled": cancelled,
        "failed": failed,
    }


@app.get("/api/admin/verticals/halts")
async def admin_get_vertical_halts(
    _: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Operator-console view of which verticals are currently halted."""
    halts = await runtime_settings_mod.get_vertical_halts()
    return {"halts": halts}


@app.post("/api/admin/verticals/{vertical}/halt")
async def admin_halt_vertical(
    vertical: str,
    body: AdminHaltVerticalRequest | None = None,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Halt a single vertical.

    Persists the halt to ``system_settings`` (so the risk-gate chain
    rejects on the next cycle), cancels every resting Kalshi order
    attached to this vertical, and writes an ``admin_audit_log`` row.
    Idempotent — a double-halt returns ``changed=False`` and does not
    re-fire the cancel sweep or the audit row.
    """
    actor_email: str | None = None
    try:
        actor_email = (await check_approval(actor))[1]
    except Exception:
        pass
    reason = body.reason if body else None
    try:
        meta, changed = await runtime_settings_mod.set_vertical_halted(
            vertical,
            actor_user_id=actor.user_id,
            actor_email=actor_email,
            reason=reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    cancel_summary: dict[str, int] = {
        "scanned": 0, "matched": 0, "cancelled": 0, "failed": 0,
    }
    if changed:
        cancel_summary = await _cancel_resting_for_vertical(
            vertical.strip().lower()
        )
    return {
        "vertical": vertical.strip().lower(),
        "halted": True,
        "changed": changed,
        "meta": meta,
        "resting_orders": cancel_summary,
    }


@app.post("/api/admin/verticals/{vertical}/resume")
async def admin_resume_vertical(
    vertical: str,
    body: AdminResumeVerticalRequest | None = None,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Resume a single vertical. Idempotent — no-op when already live.

    Accepts an optional ``reason`` (mirrors the halt flow) so operators
    can capture *why* something was resumed in the audit trail.
    """
    actor_email: str | None = None
    try:
        actor_email = (await check_approval(actor))[1]
    except Exception:
        pass
    reason = body.reason if body else None
    try:
        prev_meta, changed = await runtime_settings_mod.clear_vertical_halt(
            vertical,
            actor_user_id=actor.user_id,
            actor_email=actor_email,
            reason=reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "vertical": vertical.strip().lower(),
        "halted": False,
        "changed": changed,
        "previous_meta": prev_meta,
    }


@app.get("/api/dashboard/recent-cycles")
async def dashboard_recent_cycles(
    limit: int = 10, _: object = Depends(require_approved_user)
) -> dict[str, object]:
    limit = max(1, min(100, int(limit)))
    return {"items": await dashboard.get_recent_cycles(limit)}


@app.get("/api/dashboard/slippage-guard-rejects")
async def dashboard_slippage_guard_rejects(
    cycle_id: int | None = None,
    since_hours: int = 24,
    limit: int = 100,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Detail rows for orders the pre-submit slippage / liquidity guard
    rejected — by cycle when ``cycle_id`` is supplied, otherwise the last
    ``since_hours`` (capped at 168). Scoped to approved operators, same
    gating as `/api/dashboard/recent-cycles`.
    """
    limit = max(1, min(500, int(limit)))
    items = await dashboard.get_slippage_guard_rejects(
        cycle_id=cycle_id, since_hours=since_hours, limit=limit,
    )
    return {"items": items, "cycle_id": cycle_id}


@app.get("/api/dashboard/uw-signals")
async def dashboard_uw_signals(
    limit: int = 20,
    since_hours: int = 24,
    signal_type: str | None = None,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Recent Unusual Whales signals for the operator research feed.

    Mobile research tab + web equivalents read from this endpoint.  Approved
    operators only — same gating as `/api/dashboard/recent-cycles`.
    """
    return {
        "items": await dashboard.get_recent_uw_signals(
            limit=limit,
            since_hours=since_hours,
            signal_type=signal_type,
        ),
    }


@app.get("/api/dashboard/over-filled-orders-today")
async def dashboard_over_filled_orders_today(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """GTC orders Kalshi over-filled in the current UTC day.

    Backed by ``positions.over_filled = TRUE`` (set by
    :mod:`app.kalshi.lifecycle`) joined with the immutable
    ``order_fills`` ledger so the operator console can flag
    reconciliation discrepancies at a glance.
    """
    return await dashboard.get_over_filled_orders_today()


@app.get("/api/dashboard/crypto-ohlcv-retention")
async def dashboard_crypto_ohlcv_retention(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Operator-facing snapshot of the ``crypto_ohlcv`` retention sweep.

    Surfaces the same ``state()`` the loop tracks internally
    (running/enabled flags, last tick, last error, total + last + per-
    granularity deletion counts, and the resolved retention windows)
    plus two derived fields the dashboard uses to flag a misbehaving
    sweep at a glance: ``seconds_since_last_tick`` and ``health``
    (``ok`` / ``stale`` / ``error`` / ``disabled``).
    """
    return await dashboard.get_crypto_ohlcv_retention_status()


@app.get("/api/dashboard/bankroll/recent")
async def dashboard_bankroll_recent(
    limit: int = 200,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Recent ``bankroll_events`` rows + the current live balance + the
    Kalshi balance-sync freshness indicator.

    The continuous Kalshi balance sync (see ``app.bankroll_sync``) writes
    a row every time the live exchange balance moves; this endpoint
    surfaces that feed so the operator dashboard can render a live
    balance tile, a 24h delta sparkline, and a fresh / stale indicator
    for the underlying sync loop.
    """
    from . import bankroll_sync as _bankroll_sync

    feed = await dashboard.get_recent_bankroll_events(limit=int(limit))
    sync_state = _bankroll_sync.state()
    return {**feed, "sync": sync_state}


@app.get("/api/dashboard/recent-debates")
async def dashboard_recent_debates(
    limit: int = 10,
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    """LLM swarm debates feed.

    Free / unpaid callers receive a redacted view: consensus, agreement,
    verdict, and timing — but no per-persona reasoning and no moderator
    narrative.  Pro+ (and founders) get the full transcript.  We never
    403 here; the dashboard is meant to render *something* for every
    signed-in user, with an upsell rendered against ``redacted=true``
    items.
    """
    limit = max(1, min(100, int(limit)))
    items = await dashboard.get_recent_swarm_debates(limit)
    sub = await billing.get_user_subscription(user)
    delayed = False
    if billing._TIER_RANK[sub.tier] < billing._TIER_RANK[billing.TIER_PRO]:
        items = free_tier.apply_signal_delay(items, timestamp_key="created_at")
        items = dashboard.redact_swarm_debate_transcripts(items)
        delayed = True
    else:
        for it in items:
            it.setdefault("redacted", False)
    return {
        "items": items,
        "tier": sub.tier,
        "delayed": delayed,
        "delay_minutes": free_tier.FREE_TIER_DELAY_MINUTES if delayed else 0,
    }


# ---------------------------------------------------------------------------
# Swarm: 6-persona LLM debate + gatekeeper
# ---------------------------------------------------------------------------


class SwarmDebateRequest(BaseModel):
    game_id: str = Field(..., min_length=1, max_length=128)
    matchup: str | None = None
    league: str | None = None
    market_type: str | None = "moneyline"
    side: str | None = "YES"
    current_price: float | None = None
    context: str | None = None
    # Aggression-mode bias: conservative | balanced | aggressive.  Drives
    # both the moderator's edge/agreement thresholds and the gatekeeper's
    # veto criteria; see app/swarm/personas.py.
    aggression_mode: str | None = None


@app.post("/api/swarm/debate")
async def swarm_debate(
    body: SwarmDebateRequest,
    _: object = Depends(billing.require_tier(billing.TIER_PRO)),
) -> dict[str, object]:
    settings = get_settings()
    if not (settings.openai_api_key and settings.anthropic_api_key):
        raise HTTPException(
            status_code=503,
            detail=(
                "swarm requires both OPENAI_API_KEY and ANTHROPIC_API_KEY; "
                f"openai={bool(settings.openai_api_key)} "
                f"anthropic={bool(settings.anthropic_api_key)}"
            ),
        )
    payload = body.model_dump()
    # Default to the global operator-tunable aggression mode so direct
    # debates stay consistent with the orchestrator cycle.  Callers may
    # still override per-request.
    if not payload.get("aggression_mode"):
        payload["aggression_mode"] = await runtime_settings_mod.get_aggression_mode()
    try:
        return await run_debate(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("swarm debate failed")
        raise HTTPException(status_code=502, detail=f"swarm error: {e}")


@app.websocket("/api/ws/llm-logs")
async def ws_llm_logs(ws: WebSocket) -> None:
    """Live stream of swarm events: persona starts/deltas/verdicts,
    moderator/gatekeeper outcomes, and final debate completion.

    Auth: Clerk JWT passed via the `?token=` query param (browsers cannot
    set Authorization headers on WebSocket upgrades).  When auth is not
    configured (dev mode), the socket is allowed open.
    """
    from .auth import ws_authenticate  # local import to avoid cycle on boot

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    # Real-time persona deltas leak the same reasoning the redacted
    # transcript endpoint hides — gate the live stream to Pro+ so
    # watching the WS isn't a free-tier escape hatch.
    if user is not None:
        try:
            sub = await billing.get_user_subscription(user)
        except Exception as e:
            log.warning("ws/llm-logs subscription lookup failed: %s", e)
            await ws.close(code=1011)
            return
        if billing._TIER_RANK[sub.tier] < billing._TIER_RANK[billing.TIER_PRO]:
            # 4403 = "policy violation, subscription required"; matches
            # the convention used elsewhere for tier-gated WebSockets.
            await ws.close(code=4403, reason="pro_tier_required")
            return
    q = llm_log_hub.subscribe(label="llm-logs")
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws send failed: %s", e)
    finally:
        llm_log_hub.unsubscribe(q)


# ---------------------------------------------------------------------------
# Trading orchestrator: cycle scheduler + 12 risk gates + paper lifecycle
# ---------------------------------------------------------------------------


class OrchestratorRunCycleRequest(BaseModel):
    vertical: str = Field("sports", min_length=1, max_length=64)


@app.get("/api/orchestrator/status")
async def orchestrator_status(_: object = Depends(require_auth)) -> dict[str, object]:
    return orchestrator_scheduler.status()


@app.post("/api/orchestrator/run-cycle")
async def orchestrator_run_cycle(
    body: OrchestratorRunCycleRequest,
    _: object = Depends(billing.require_tier(billing.TIER_PRO)),
) -> dict[str, object]:
    """Manually fire a single cycle.  Useful for ops + smoke testing
    without waiting for the next scheduler tick.

    Refuses (503) until the READY gate has flipped — the same gate the
    background scheduler parks on. Without this, an operator could
    trigger a trading cycle against half-loaded predictors or before
    migrations have applied, which is exactly the regression the
    READY-gate work exists to prevent.
    """
    if not ready_gate.is_ready():
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", **ready_gate.status()},
        )
    try:
        cr = await run_cycle(body.vertical)
    except Exception as e:
        log.exception("manual cycle failed")
        raise HTTPException(status_code=502, detail=f"cycle error: {e}")
    return {
        "cycle_id": cr.cycle_id,
        "vertical": cr.vertical,
        "started_at": cr.started_at,
        "completed_at": cr.completed_at,
        "duration_seconds": cr.duration_seconds,
        "picks_evaluated": cr.picks_evaluated,
        "trades_placed": cr.trades_placed,
        "fresh_debates": cr.fresh_debates,
        "errors": cr.errors,
    }


class LiveExecuteToggleRequest(BaseModel):
    enabled: bool


async def _read_live_execute_db_mode() -> str | None:
    """Operator override of ``trading_mode`` from ``system_settings``.

    Same key the kill-switch gate consults via ``_load_operator_overrides``
    so the dashboard reflects whatever the gate is about to honour.
    Returns ``None`` if the row is missing or the table doesn't exist.
    """
    try:
        v = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = 'risk.trading_mode'"
        )
    except Exception as e:
        log.debug("live-execute db read failed (best-effort): %s", e)
        return None
    return str(v).lower() if v is not None else None


def _live_execute_payload(
    db_mode: str | None,
    sub_tier: str,
) -> dict[str, object]:
    settings = get_settings()
    env_mode = (settings.trading_mode or "paper").lower()
    effective = (db_mode or env_mode).lower()
    return {
        "enabled": effective == "live",
        "effective_mode": effective,
        "env_mode": env_mode,
        "db_mode": db_mode,
        "source": "db" if db_mode else "env",
        "caller_tier": sub_tier,
        "can_toggle": (
            billing._TIER_RANK[sub_tier] >= billing._TIER_RANK[billing.TIER_ELITE]
        ),
    }


@app.get("/api/risk/daily-loss")
async def risk_daily_loss_state(
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    """Current daily-loss snapshot + thresholds.

    Drives the Daily-Loss card on ``/system -> controls``. Readable by
    every signed-in user so the dashboard can render today's position
    against the caps; the actual trip + halt side-effect is owned by
    the ``gate_daily_loss`` risk gate, not this endpoint.
    """
    from .risk import daily_loss as _dl
    from .orchestrator.cycle import _load_operator_overrides
    try:
        overrides = await _load_operator_overrides()
    except Exception:
        overrides = {}
    snap = await _dl.compute_snapshot(overrides)
    used_pct_abs: float | None = None
    if snap.threshold_usd is not None and snap.threshold_usd > 0:
        used_pct_abs = round(
            100.0 * max(0.0, snap.loss_usd) / snap.threshold_usd, 2
        )
    used_pct_pct: float | None = None
    if snap.threshold_pct_usd is not None and snap.threshold_pct_usd > 0:
        used_pct_pct = round(
            100.0 * max(0.0, snap.loss_usd) / snap.threshold_pct_usd, 2
        )
    # Most-restrictive budget used so the UI can show a single gauge.
    used_pct_max: float | None = None
    for v in (used_pct_abs, used_pct_pct):
        if v is None:
            continue
        used_pct_max = v if used_pct_max is None else max(used_pct_max, v)
    return {
        **snap.to_dict(),
        "used_pct_abs": used_pct_abs,
        "used_pct_pct": used_pct_pct,
        "used_pct_max": used_pct_max,
        "configured": bool(
            snap.threshold_usd is not None or snap.threshold_pct is not None
        ),
    }


@app.get("/api/orchestrator/live-execute")
async def orchestrator_live_execute_state(
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    """Current live-execute state.

    Readable by every signed-in user (so the dashboard can render the
    indicator), but the ``can_toggle`` flag is only true for Elite+ —
    the same tier the ``POST`` requires.
    """
    sub = await billing.get_user_subscription(user)
    db_mode = await _read_live_execute_db_mode()
    return _live_execute_payload(db_mode, sub.tier)


@app.post("/api/orchestrator/live-execute")
async def orchestrator_live_execute_toggle(
    body: LiveExecuteToggleRequest,
    sub: billing.UserSubscription = Depends(billing.require_tier(billing.TIER_ELITE)),
) -> dict[str, object]:
    """Flip live-execute on or off.  Elite-only — sub-Elite callers get
    the standard ``tier_required`` 403 from :func:`billing.require_tier`.

    Persists into ``system_settings.risk.trading_mode`` which the
    kill-switch gate already honours (operator override beats env).
    """
    new_mode = "live" if body.enabled else "paper"
    try:
        await db.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ('risk.trading_mode', $1, now())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value,
                  updated_at = EXCLUDED.updated_at
            """,
            new_mode,
        )
    except Exception as e:
        log.exception("live-execute toggle failed")
        raise HTTPException(status_code=502, detail=f"toggle write failed: {e}")
    log.info(
        "live-execute toggled to %s by %s (tier=%s)",
        new_mode,
        sub.user_id,
        sub.tier,
    )
    db_mode = await _read_live_execute_db_mode()
    settings = get_settings()
    env_mode = (settings.trading_mode or "paper").lower()
    effective = (db_mode or env_mode).lower()
    return {
        "enabled": effective == "live",
        "effective_mode": effective,
        "env_mode": env_mode,
        "db_mode": db_mode,
        "source": "db" if db_mode else "env",
        "caller_tier": sub.tier,
        "can_toggle": True,
    }


@app.get("/api/orchestrator/recent-events")
async def orchestrator_recent_events(
    cycles: int = 5,
    _: object = Depends(require_auth),
) -> dict[str, object]:
    """Return buffered pipeline events from the most recent ``cycles``
    distinct cycle ids so /pipeline can hydrate immediately on mount
    instead of staring at an empty buffer until the next WS event lands.

    Each event carries a server-assigned ``_seq`` so the dashboard can
    dedupe against any frames the WebSocket replays.
    """
    n = max(1, min(20, int(cycles)))
    events = await pipeline_event_hub.recent_events_with_fallback(max_cycles=n)
    return {"cycles": n, "events": events}


@app.websocket("/api/ws/pipeline")
async def ws_pipeline(ws: WebSocket) -> None:
    """Live stream of orchestrator pipeline events: cycle_start, gate
    pass/fail, candidate_complete (placed/rejected/place_error),
    cycle_complete.  Auth via Clerk JWT in `?token=` (same contract as
    `/api/ws/llm-logs` since browsers can't set Authorization on WS upgrades).
    """
    from .auth import ws_authenticate

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    q = pipeline_event_hub.subscribe(label="pipeline")
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws pipeline send failed: %s", e)
    finally:
        pipeline_event_hub.unsubscribe(q)


# ---------------------------------------------------------------------------
# Settlement endpoints
# ---------------------------------------------------------------------------


class SettlementSettleMarketRequest(BaseModel):
    market_id: str = Field(..., min_length=1, max_length=128)
    outcome_yes: int = Field(..., ge=0, le=1)
    settle_price: float | None = Field(None, ge=0.0, le=100.0)


_RECENT_SETTLEMENTS_LIMIT = 10


async def _settlement_recent_tap() -> None:
    """Long-lived subscriber on the settlement event hub that persists
    `market_settle_complete` events to the `settlement_events` table so
    the dashboard's recent-settlements panel survives API restarts and
    is consistent across replicas.  Restarted on lifespan; cancellation
    is the normal exit."""
    q = settlement_event_hub.subscribe(label="recent-tap")
    try:
        while True:
            ev = await q.get()
            if isinstance(ev, dict) and ev.get("type") == "market_settle_complete":
                await _record_recent_settlement(ev)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # pragma: no cover
        log.warning("settlement recent tap exited: %s", e)
    finally:
        settlement_event_hub.unsubscribe(q)


async def _record_recent_settlement(event: dict[str, object]) -> None:
    """Persist a market_settle_complete event so the dashboard can show
    recent settlements after a page reload or API restart.  Stores the
    full event payload as JSONB plus a few hot columns for indexing /
    debugging.  Failures are logged but do not propagate — the WS feed
    still delivers the live event."""
    try:
        verticals = event.get("verticals")
        if not isinstance(verticals, list):
            verticals = None
        else:
            verticals = [str(v) for v in verticals]
        outcome = event.get("outcome_yes")
        outcome_int = int(outcome) if isinstance(outcome, (int, float)) else None
        trades = event.get("trades_settled")
        trades_int = int(trades) if isinstance(trades, (int, float)) else None
        alerts = event.get("alerts_raised")
        alerts_int = int(alerts) if isinstance(alerts, (int, float)) else None
        market_id = event.get("market_id")
        market_str = str(market_id) if market_id is not None else None
        await db.execute(
            """
            INSERT INTO settlement_events (
                event_type, market_id, outcome_yes, trades_settled,
                alerts_raised, verticals, payload
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            str(event.get("type") or "market_settle_complete"),
            market_str,
            outcome_int,
            trades_int,
            alerts_int,
            verticals,
            json.dumps(event, default=str),
        )
    except Exception as e:  # pragma: no cover
        log.warning("settlement event persist failed: %s", e)


async def _load_recent_settlements(limit: int = _RECENT_SETTLEMENTS_LIMIT) -> list[dict[str, object]]:
    """Load the most recent persisted market_settle_complete events for
    the dashboard polling fallback.  Returns most-recent-first so the UI
    matches the previous in-memory buffer ordering."""
    try:
        rows = await db.fetch(
            """
            SELECT recorded_at, payload
              FROM settlement_events
             WHERE event_type = 'market_settle_complete'
             ORDER BY recorded_at DESC
             LIMIT $1
            """,
            int(limit),
        )
    except Exception as e:  # pragma: no cover
        log.warning("settlement recent load failed: %s", e)
        return []
    out: list[dict[str, object]] = []
    for r in rows:
        raw = r["payload"]
        if isinstance(raw, str):
            try:
                ev = json.loads(raw)
            except Exception:
                continue
        elif isinstance(raw, dict):
            ev = dict(raw)
        else:
            continue
        ev.setdefault("recorded_at", r["recorded_at"].isoformat() if r["recorded_at"] else None)
        out.append(ev)
    return out


async def _settlement_today_summary() -> dict[str, object]:
    """Aggregate today's settled-trade activity for the operator panel.
    Returns trade count, vertical breakdown, and total realized PnL."""
    out: dict[str, object] = {
        "trades_settled": 0,
        "realized_pnl": 0.0,
        "by_vertical": [],
    }
    try:
        pool = await db.get_pool()
        row = await pool.fetchrow(
            """
            SELECT COUNT(*)::int AS n,
                   COALESCE(SUM(pnl), 0)::float AS pnl
              FROM trades
             WHERE settled_at >= date_trunc('day', now())
            """
        )
        if row is not None:
            out["trades_settled"] = int(row["n"] or 0)
            out["realized_pnl"] = float(row["pnl"] or 0.0)
        rows = await pool.fetch(
            """
            SELECT vertical,
                   COUNT(*)::int AS trades_settled,
                   COALESCE(SUM(pnl), 0)::float AS realized_pnl
              FROM trades
             WHERE settled_at >= date_trunc('day', now())
             GROUP BY vertical
             ORDER BY vertical NULLS LAST
            """
        )
        out["by_vertical"] = [
            {
                "vertical": r["vertical"],
                "trades_settled": int(r["trades_settled"] or 0),
                "realized_pnl": float(r["realized_pnl"] or 0.0),
            }
            for r in rows
        ]
    except Exception as e:  # pragma: no cover
        log.warning("settlement today summary failed: %s", e)
    return out


@app.get("/api/settlement/status")
async def settlement_status(_: object = Depends(require_auth)) -> dict[str, object]:
    """Scheduler status + today's settled-trade summary + recent
    market_settle_complete events.  The recent-events buffer is the WS
    fallback when the live socket drops or the page is fresh."""
    base = settlement_scheduler.status()
    base["today"] = await _settlement_today_summary()
    base["recent_settlements"] = await _load_recent_settlements()
    return base


@app.post("/api/settlement/run-cycle")
async def settlement_run_cycle(_: object = Depends(require_auth)) -> dict[str, object]:
    """Manually trigger one settlement sweep.

    Refuses (503) until the READY gate has flipped, mirroring the
    orchestrator manual-trigger guard so a settlement sweep can't run
    against an un-migrated DB or partially-loaded process.
    """
    if not ready_gate.is_ready():
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", **ready_gate.status()},
        )
    try:
        cr = await run_settlement_cycle()
    except Exception as e:
        log.exception("manual settlement cycle failed")
        raise HTTPException(status_code=502, detail=f"settlement cycle error: {e}")
    return {
        "started_at": cr.started_at,
        "completed_at": cr.completed_at,
        "duration_seconds": cr.duration_seconds,
        "markets_examined": cr.markets_examined,
        "markets_settled": cr.markets_settled,
        "trades_settled": cr.trades_settled,
        "persona_metrics_updated": cr.persona_metrics_updated,
        "alerts_raised": cr.alerts_raised,
        "errors": cr.errors,
    }


@app.post("/api/settlement/settle-market")
async def settlement_settle_market(
    body: SettlementSettleMarketRequest, _: object = Depends(require_auth)
) -> dict[str, object]:
    """Manual / backfill settlement of a specific market.  Used by ops
    when Kalshi's `/markets/{ticker}` lags or for paper-mode markets the
    exchange never resolves."""
    try:
        result = await settle_market(
            body.market_id,
            outcome_yes=body.outcome_yes,
            settle_price=body.settle_price,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("manual settle_market failed")
        raise HTTPException(status_code=502, detail=str(e))
    return {k: v for k, v in result.items() if k != "details"}


class SettlementBackfillRequest(BaseModel):
    csv_path: str | None = Field(
        None,
        description=(
            "Optional path to an operator-supplied CSV "
            "(`market_id,outcome_yes[,settle_price]`) for markets Kalshi "
            "no longer reports. When omitted, results are pulled from Kalshi."
        ),
    )
    limit: int | None = Field(
        None, ge=1, le=10000,
        description="Cap how many un-settled markets to process this run.",
    )


@app.get("/api/settlement/backfill-status")
async def settlement_backfill_status(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Snapshot of the in-process backfill job (idle, running, or completed).

    Safe to poll from the operator console while the job is running.
    """
    return get_backfill_status()


@app.post("/api/settlement/backfill")
async def settlement_backfill_start(
    body: SettlementBackfillRequest,
    _: object = Depends(require_admin),
) -> dict[str, object]:
    """Kick off a one-shot backfill of historical Kalshi settlements.

    Runs in the background; poll `/api/settlement/backfill-status` for
    progress.  Returns 409 if another run is already in flight.
    """
    snap = get_backfill_status()
    if snap.get("state") == "running" or backfill_is_running():
        raise HTTPException(status_code=409, detail="backfill already running")

    async def _runner() -> None:
        try:
            await run_backfill(csv_path=body.csv_path, limit=body.limit)
        except Exception:
            log.exception("background backfill failed")

    asyncio.create_task(_runner(), name="settlement-backfill")
    return {"started": True, "status": get_backfill_status()}


@app.websocket("/api/ws/settlement")
async def ws_settlement(ws: WebSocket) -> None:
    """Live stream of settlement events: cycle_start, market_settle_start,
    trade_settled, market_settle_complete, cycle_complete, plus the
    historical-backfill lifecycle (backfill_run_start, backfill_progress,
    backfill_run_complete) emitted by ``app.settlement.backfill`` so the
    operator console can render live progress without polling
    ``/api/settlement/backfill-status``.  Auth via Clerk JWT in
    ``?token=`` (same contract as ``/api/ws/pipeline``).
    """
    from .auth import ws_authenticate

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    q = settlement_event_hub.subscribe(label="settlement")
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws settlement send failed: %s", e)
    finally:
        settlement_event_hub.unsubscribe(q)


# ---------------------------------------------------------------------------
# Kalshi exchange endpoints
# ---------------------------------------------------------------------------


def _kalshi_error_to_http(e: Exception) -> HTTPException:
    if isinstance(e, kalshi_client_mod.KalshiAPIError):
        status = 502 if e.status == 0 else min(max(e.status, 400), 599)
        return HTTPException(status_code=status, detail={"kalshi_status": e.status, "body": e.body})
    if isinstance(e, kalshi_auth.KalshiAuthError):
        return HTTPException(status_code=503, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


@app.get("/api/kalshi/balance")
async def kalshi_balance(_: object = Depends(require_auth)) -> dict[str, object]:
    try:
        async with kalshi_client_mod.KalshiClient() as c:
            bal = await c.get_balance()
    except Exception as e:
        raise _kalshi_error_to_http(e)
    return {
        "balance_cents": bal.balance_cents,
        "balance_dollars": bal.balance_dollars,
        "payout_cents": bal.payout_cents,
        "raw": bal.raw,
    }


_KALSHI_MARKET_PRICE_CACHE: dict[str, tuple[float, dict]] = {}
_KALSHI_MARKET_PRICE_TTL = 10.0  # seconds — short, since this powers live MTM


async def _kalshi_fetch_market_cached(
    client: "kalshi_client_mod.KalshiClient", ticker: str
) -> dict | None:
    """Fetch a Kalshi market with a small TTL cache.

    The /api/kalshi/positions endpoint may be polled every few seconds
    by the dashboard; without a cache we'd hammer Kalshi's per-market
    endpoint once per ticker per poll.  A 10-second TTL is short enough
    that mark-to-market remains live and long enough that a rapid poll
    cycle reuses the previous fetch.
    """
    now = time.time()
    hit = _KALSHI_MARKET_PRICE_CACHE.get(ticker)
    if hit and now - hit[0] < _KALSHI_MARKET_PRICE_TTL:
        return hit[1]
    try:
        data = await client.get_market(ticker)
    except Exception as e:
        log.debug("kalshi market fetch failed for %s: %s", ticker, e)
        return None
    market = data.get("market") if isinstance(data, dict) else None
    if isinstance(market, dict):
        _KALSHI_MARKET_PRICE_CACHE[ticker] = (now, market)
        return market
    return None


async def _kalshi_resolve_market_prices(
    client: "kalshi_client_mod.KalshiClient", tickers: list[str]
) -> dict[str, dict]:
    """Concurrently fetch live market data for a batch of tickers."""
    sem = asyncio.Semaphore(8)
    out: dict[str, dict] = {}

    async def one(t: str) -> None:
        async with sem:
            m = await _kalshi_fetch_market_cached(client, t)
            if m is not None:
                out[t] = m

    await asyncio.gather(*[one(t) for t in tickers])
    return out


def _kalshi_yes_mid_cents(market: dict | None) -> float | None:
    """Read the live YES-leg mid-price for a Kalshi market, in cents 0..100.

    Kalshi's modern ``/markets/{ticker}`` payload uses dollar-denominated
    string fields (``yes_bid_dollars``, ``yes_ask_dollars``,
    ``last_price_dollars``) — the legacy integer-cent fields
    (``yes_bid``, ``yes_ask``, ``last_price``) are present but
    **always None** on accounts that have any fractional activity.
    We prefer the dollar fields, falling back to the legacy fields and
    the previous-tick fields so a thinly-quoted market still shows
    *something*.
    """
    if not market:
        return None

    def _dollars_to_cents(x) -> float | None:
        if x is None or x == "":
            return None
        try:
            return float(x) * 100.0
        except (TypeError, ValueError):
            return None

    def _cents(x) -> float | None:
        if x is None or x == "":
            return None
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def _first_present(*candidates: float | None) -> float | None:
        # Explicit ``is not None`` so a legitimate 0.0¢ quote (deep
        # out-of-the-money or freshly-resolved leg) isn't silently
        # discarded by Python's ``or`` truthiness — that would mark
        # near-100¢ NO positions at the previous tick rather than the
        # actual 0.0¢ YES quote.
        for c in candidates:
            if c is not None:
                return c
        return None

    # Modern (dollar-suffixed) first, then legacy integer cents,
    # then previous-tick fallbacks.
    yb = _first_present(
        _dollars_to_cents(market.get("yes_bid_dollars")),
        _cents(market.get("yes_bid")),
        _dollars_to_cents(market.get("previous_yes_bid_dollars")),
    )
    ya = _first_present(
        _dollars_to_cents(market.get("yes_ask_dollars")),
        _cents(market.get("yes_ask")),
        _dollars_to_cents(market.get("previous_yes_ask_dollars")),
    )
    last = _first_present(
        _dollars_to_cents(market.get("last_price_dollars")),
        _cents(market.get("last_price")),
        _dollars_to_cents(market.get("previous_price_dollars")),
    )
    if yb is not None and ya is not None:
        return (yb + ya) / 2.0
    if last is not None:
        return last
    if yb is not None:
        return yb
    if ya is not None:
        return ya
    return None


def _kalshi_current_value_cents(position: float, market: dict | None) -> int | None:
    """Mark-to-market value in cents for a Kalshi position.

    YES contracts pay $1 if the market resolves YES; NO contracts pay
    $1 if NO.  We mark each side at its current mid-price (or last
    price as a fallback), so:

    * YES position: ``position * yes_mid_cents``
    * NO  position: ``|position| * (100 - yes_mid_cents)``

    Returns ``None`` when the position is flat or no live price is
    available, signalling the UI to hide the mark instead of showing
    a misleading zero.
    """
    if position == 0:
        return None
    yes_price = _kalshi_yes_mid_cents(market)
    if yes_price is None:
        return None
    if position > 0:
        return int(round(position * yes_price))
    return int(round(abs(position) * (100.0 - yes_price)))


@app.get("/api/kalshi/positions")
async def kalshi_positions(limit: int = 100, _: object = Depends(require_auth)) -> dict[str, object]:
    limit = max(1, min(500, int(limit)))
    try:
        async with kalshi_client_mod.KalshiClient() as c:
            pos = await c.get_positions(limit=limit)
            # Enrich each position with a human-readable market title (cached
            # in `market_title_cache`, falls back to Kalshi `/markets/{ticker}`)
            # and a derived bet_type. The UI shows the title prominently and
            # keeps the raw ticker as a small secondary line.
            tickers = [p.market_ticker for p in pos if p.market_ticker]
            titles: dict[str, str] = {}
            if tickers:
                try:
                    titles = await kalshi_market_titles.resolve_titles(tickers)
                except Exception as e:
                    log.warning("market title enrichment failed for /api/kalshi/positions: %s", e)
            # Live mark-to-market: only fetch prices for positions with
            # non-zero size to avoid pointless round-trips for flat markets.
            open_tickers = [
                p.market_ticker for p in pos if p.market_ticker and p.position != 0
            ]
            markets: dict[str, dict] = {}
            if open_tickers:
                try:
                    markets = await _kalshi_resolve_market_prices(c, open_tickers)
                except Exception as e:
                    log.warning("kalshi market price enrichment failed: %s", e)
    except Exception as e:
        raise _kalshi_error_to_http(e)
    items: list[dict[str, object]] = []
    for p in pos:
        if p.position > 0:
            bet_type = "YES"
        elif p.position < 0:
            bet_type = "NO"
        else:
            bet_type = "FLAT"
        market = markets.get(p.market_ticker)
        cv = _kalshi_current_value_cents(p.position, market)
        # Entry-side reference (cents 0..100) — used by the UI as the
        # entry/cost basis for the position's chosen side.  Cost basis
        # per contract = market_exposure_cents / |position|.
        entry_per_contract: int | None = None
        if p.position != 0 and p.market_exposure_cents:
            try:
                entry_per_contract = int(round(
                    p.market_exposure_cents / abs(p.position)
                ))
            except (ZeroDivisionError, TypeError):
                entry_per_contract = None
        # Live mid-price for the YES leg in cents (for chart/diagnostic
        # use).  Uses the same dollar-suffixed-first resolution as
        # _kalshi_yes_mid_cents so the UI's per-contract Mark column
        # stays in sync with the live MTM.
        yes_mid = _kalshi_yes_mid_cents(market) if market is not None else None
        last_price_cents = int(round(yes_mid)) if yes_mid is not None else None
        items.append({
            "market_ticker": p.market_ticker,
            "market_title": titles.get(p.market_ticker),
            "bet_type": bet_type,
            "position": p.position,
            "market_exposure_cents": p.market_exposure_cents,
            "realized_pnl_cents": p.realized_pnl_cents,
            "fees_paid_cents": p.fees_paid_cents,
            "current_value_cents": cv,
            "entry_price_cents": entry_per_contract,
            "last_price_cents": last_price_cents,
        })
    return {"items": items, "count": len(pos)}


@app.get("/api/kalshi/markets")
async def kalshi_markets(
    limit: int = 50, status: str = "open", _: object = Depends(require_auth)
) -> dict[str, object]:
    try:
        async with kalshi_client_mod.KalshiClient() as c:
            data = await c.list_markets(limit=max(1, min(200, limit)), status=status or None)
    except Exception as e:
        raise _kalshi_error_to_http(e)
    return data


@app.get("/api/kalshi/orders/resting")
async def kalshi_resting_orders(_: object = Depends(require_auth)) -> dict[str, object]:
    rows = await kalshi_lifecycle.list_resting_orders()
    return {"items": [r.to_dict() for r in rows], "count": len(rows)}


@app.post("/api/kalshi/paper-order")
async def kalshi_paper_order(
    payload: dict,
    _: object = Depends(billing.require_tier(billing.TIER_PRO)),
) -> dict[str, object]:
    """Submit a paper order via the lifecycle. Always paper, even when
    AXIOM_TRADING_MODE=live, so this endpoint is safe to expose to
    operators — but it is still a Pro-tier feature ("manual cycle
    execution" on the pricing page).  Free callers get the standard
    ``tier_required`` 403 from :func:`billing.require_tier`.
    """
    try:
        req = OrderRequest(
            market_ticker=str(payload.get("market_ticker") or payload.get("ticker") or "").strip(),
            side=str(payload.get("side") or "yes").lower(),  # type: ignore[arg-type]
            action=str(payload.get("action") or "buy").lower(),  # type: ignore[arg-type]
            count=int(payload.get("count") or payload.get("contracts") or 1),
            limit_price_cents=(int(payload["limit_price_cents"])
                               if payload.get("limit_price_cents") is not None else None),
            type=str(payload.get("type") or "limit"),  # type: ignore[arg-type]
            time_in_force=str(payload.get("time_in_force") or "GTC"),  # type: ignore[arg-type]
            vertical=str(payload.get("vertical") or "sports"),
            context=dict(payload.get("context") or {}),
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"bad order payload: {e}")
    if not req.market_ticker:
        raise HTTPException(status_code=422, detail="market_ticker is required")
    try:
        rec = await kalshi_lifecycle.submit_order(req, force_paper=True)
    except kalshi_lifecycle.OrderRejected as e:
        raise HTTPException(status_code=409, detail=str(e))
    return rec.to_dict()


class EmergencyStopRequest(BaseModel):
    reason: str | None = None


@app.post("/api/admin/emergency-stop")
async def admin_emergency_stop(
    body: EmergencyStopRequest | None = None,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """High-priority "kill all" for the operator on call.

    Cancels every resting Kalshi order, trips the process-wide kill
    switch, persists ``risk.trading_mode = 'halt'`` so the cycle gate
    keeps refusing to place orders even after a process restart, writes
    a single ``emergency_stop`` row to the admin audit log, and emails
    the on-call operator.

    Per task #104. The body is shared with the Telegram concierge
    (task #111) — see :func:`app.telegram.estop.execute_emergency_stop`.
    """
    from .telegram import estop as _estop
    reason = (body.reason if body else None) or "operator emergency stop"
    actor_email: str | None = None
    try:
        actor_email = (await check_approval(actor))[1]
    except Exception:
        pass
    return await _estop.execute_emergency_stop(
        actor_user_id=actor.user_id,
        actor_email=actor_email,
        reason=reason,
    )


class RearmTradingRequest(BaseModel):
    mode: str = Field(..., pattern="^(paper|live)$")


@app.post("/api/admin/rearm-trading")
async def admin_rearm_trading(
    body: RearmTradingRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Clear an emergency-stop halt without an `UPDATE system_settings`.

    The emergency-stop endpoint persists ``risk.trading_mode='halt'`` so
    the cycle gate keeps refusing orders across restarts. The existing
    live-execute toggle only flips between ``paper``/``live`` and
    ``/api/kalshi/resume`` only resets the in-memory kill switch, so
    today an admin would have to hand-edit ``system_settings`` to undo
    a halt. This endpoint:

      1. Writes ``risk.trading_mode`` to ``paper`` or ``live`` (the
         operator's choice — they re-arm into whichever mode they want).
      2. Calls :func:`kalshi_lifecycle.resume` so the in-memory kill
         switch is also cleared.
      3. Writes a single ``rearm_trading`` row to the admin audit log
         capturing actor, previous mode, and new mode.
    """
    new_mode = body.mode.lower()
    actor_email: str | None = None
    try:
        actor_email = (await check_approval(actor))[1]
    except Exception:
        pass

    previous_mode: str | None
    try:
        prev_val = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = $1",
            "risk.trading_mode",
        )
        previous_mode = str(prev_val).lower() if prev_val is not None else None
    except Exception as e:
        log.debug("rearm-trading: previous_mode read failed: %s", e)
        previous_mode = None

    try:
        await db.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value,
                  updated_at = EXCLUDED.updated_at
            """,
            "risk.trading_mode",
            new_mode,
        )
    except Exception as e:
        log.exception("rearm-trading: trading_mode persist failed")
        raise HTTPException(status_code=502, detail=f"rearm write failed: {e}")

    kill_state = kalshi_lifecycle.resume()

    try:
        await db.execute(
            """
            INSERT INTO admin_audit_log (
                actor_user_id, actor_email,
                target_user_id, target_email,
                action, before_value, after_value
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
            """,
            actor.user_id,
            actor_email,
            "system",
            None,
            "rearm_trading",
            json.dumps({"trading_mode": previous_mode}),
            json.dumps({"trading_mode": new_mode}),
        )
    except Exception as e:
        log.warning("rearm-trading: audit log insert failed: %s", e)

    log.warning(
        "REARM TRADING by %s (%s): %s -> %s",
        actor_email or actor.user_id, actor.user_id,
        previous_mode, new_mode,
    )

    return {
        "ok": True,
        "trading_mode": new_mode,
        "previous_mode": previous_mode,
        "kill_switch": kill_state,
    }


@app.post("/api/kalshi/halt")
async def kalshi_halt(reason: str | None = None) -> dict[str, object]:
    """Trip the kill switch. Intentionally NO middleware deps — operators must
    be able to halt trading even when downstream auth is broken.
    """
    return kalshi_lifecycle.halt(reason)


@app.post("/api/kalshi/resume")
async def kalshi_resume(_: object = Depends(require_auth)) -> dict[str, object]:
    return kalshi_lifecycle.resume()


@app.get("/api/kalshi/kill-switch")
async def kalshi_kill_state(_: object = Depends(require_auth)) -> dict[str, object]:
    return kalshi_lifecycle.kill_switch_state()


@app.websocket("/api/ws/kalshi")
async def kalshi_event_stream(websocket: WebSocket):  # pragma: no cover - covered by manual integration
    """Stream the in-process Kalshi event bus to a connected client.

    Auth: pass a Clerk session JWT as the `?token=` query parameter (browsers
    can't set headers on the WS upgrade, so the query parameter is the
    practical path). Verified with the same JWKS used by `require_auth`.
    """
    from .auth import ws_authenticate

    user = await ws_authenticate(websocket)
    if user is None and auth_configured():
        return
    q = await kalshi_bus.subscribe(label="kalshi-multiplex")
    try:
        while True:
            msg = await q.get()
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws/kalshi error: %s", e)
    finally:
        await kalshi_bus.unsubscribe(q)


# ---------------------------------------------------------------------------
# Polymarket exchange endpoints (mirror of /api/kalshi/*)
# ---------------------------------------------------------------------------


def _polymarket_error_to_http(e: Exception) -> HTTPException:
    if isinstance(e, polymarket_client_mod.PolymarketAPIError):
        status = 502 if e.status == 0 else min(max(e.status, 400), 599)
        return HTTPException(status_code=status, detail={"polymarket_status": e.status, "body": e.body})
    if isinstance(e, polymarket_auth.PolymarketAuthError):
        return HTTPException(status_code=503, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


@app.get("/api/polymarket/balance")
async def polymarket_balance(_: object = Depends(require_auth)) -> dict[str, object]:
    try:
        async with polymarket_client_mod.PolymarketClient() as c:
            bal = await c.get_balance()
    except Exception as e:
        raise _polymarket_error_to_http(e)
    return {
        "balance_cents": bal.balance_cents,
        "balance_dollars": bal.balance_dollars,
        "allowance_cents": bal.allowance_cents,
        "raw": bal.raw,
    }


@app.get("/api/polymarket/positions")
async def polymarket_positions(
    limit: int = 100, _: object = Depends(require_auth)
) -> dict[str, object]:
    limit = max(1, min(500, int(limit)))
    try:
        async with polymarket_client_mod.PolymarketClient() as c:
            pos = await c.get_positions(limit=limit)
    except Exception as e:
        raise _polymarket_error_to_http(e)
    return {
        "items": [
            {
                "market_id": p.market_id,
                "token_id": p.token_id,
                "outcome": p.outcome,
                "size": p.size,
                "avg_price": p.avg_price,
                "realized_pnl_cents": p.realized_pnl_cents,
            }
            for p in pos
        ],
        "count": len(pos),
    }


@app.get("/api/polymarket/markets")
async def polymarket_markets(
    limit: int = 50,
    next_cursor: str | None = None,
    _: object = Depends(require_auth),
) -> dict[str, object]:
    try:
        async with polymarket_client_mod.PolymarketClient() as c:
            data = await c.list_markets(limit=max(1, min(200, limit)), next_cursor=next_cursor)
    except Exception as e:
        raise _polymarket_error_to_http(e)
    return data


@app.get("/api/polymarket/orders/resting")
async def polymarket_resting_orders(_: object = Depends(require_auth)) -> dict[str, object]:
    rows = await polymarket_lifecycle.list_resting_orders()
    return {"items": [r.to_dict() for r in rows], "count": len(rows)}


@app.post("/api/polymarket/paper-order")
async def polymarket_paper_order(
    payload: dict,
    _: object = Depends(billing.require_tier(billing.TIER_PRO)),
) -> dict[str, object]:
    """Submit a paper order via the Polymarket lifecycle. Always paper,
    even when AXIOM_TRADING_MODE=live, so this endpoint is safe to expose
    to operators — but it is still a Pro-tier feature ("manual cycle
    execution" on the pricing page).  Free callers get the standard
    ``tier_required`` 403 from :func:`billing.require_tier`."""
    try:
        req = PolymarketOrderRequest(
            market_id=str(payload.get("market_id") or payload.get("conditionId") or "").strip(),
            token_id=str(payload.get("token_id") or payload.get("tokenID") or "").strip(),
            side=str(payload.get("side") or "yes").lower(),  # type: ignore[arg-type]
            action=str(payload.get("action") or "buy").lower(),  # type: ignore[arg-type]
            count=int(payload.get("count") or payload.get("contracts") or payload.get("size") or 1),
            limit_price_cents=(int(payload["limit_price_cents"])
                               if payload.get("limit_price_cents") is not None else None),
            type=str(payload.get("type") or "limit"),  # type: ignore[arg-type]
            time_in_force=str(payload.get("time_in_force") or "GTC"),  # type: ignore[arg-type]
            vertical=str(payload.get("vertical") or "sports"),
            context=dict(payload.get("context") or {}),
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"bad order payload: {e}")
    if not req.market_id:
        raise HTTPException(status_code=422, detail="market_id is required")
    if not req.token_id:
        raise HTTPException(status_code=422, detail="token_id is required")
    try:
        rec = await polymarket_lifecycle.submit_order(req, force_paper=True)
    except polymarket_lifecycle.OrderRejected as e:
        raise HTTPException(status_code=409, detail=str(e))
    return rec.to_dict()


@app.websocket("/api/ws/polymarket")
async def polymarket_event_stream(websocket: WebSocket):  # pragma: no cover
    """Stream the in-process Polymarket event bus to a connected client.

    Auth: pass a Clerk session JWT as the `?token=` query parameter.
    """
    from .auth import ws_authenticate

    user = await ws_authenticate(websocket)
    if user is None and auth_configured():
        return
    q = await polymarket_bus.bus.subscribe(label="polymarket-multiplex")
    try:
        while True:
            msg = await q.get()
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws/polymarket error: %s", e)
    finally:
        await polymarket_bus.bus.unsubscribe(q)


# ---------------------------------------------------------------------------
# Storage / model artifact discovery
# ---------------------------------------------------------------------------


@app.get("/api/storage/list")
async def storage_list(
    prefix: str = "",
    limit: int = 50,
    offset: int = 0,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    try:
        entries = await storage.list_path(prefix, limit=limit, offset=offset)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"storage error: {e}")
    return {
        "prefix": prefix,
        "items": [
            {"name": e.name, "is_folder": e.is_folder, "size": e.size, "updated_at": e.updated_at}
            for e in entries
        ],
    }


@app.get("/api/models/status")
async def models_status(_: object = Depends(require_auth)) -> dict[str, object]:
    """Detailed loader state: archives, predictor catalog, disk usage,
    plus the currently-bound active predictor + version (mirrors what
    /api/health reports, so the diagnostics endpoints agree)."""
    state = models_loader.get_state()
    active = predictor.active_model_info()
    return {
        **state.as_dict(),
        "disk_usage": models_loader.disk_usage_summary(),
        "active_predictor": active,
        "active_model_version": active.get("version") if active else None,
    }


async def _reload_and_mirror() -> None:
    """Reload model artifacts and, on success, mirror them to GitHub.

    The mirror is best-effort: if `GITHUB_MIRROR_REPO`/`GITHUB_TOKEN` aren't
    set or the push fails, we log and continue — model promotion must not
    block on the mirror.
    """
    state = await models_loader.ensure_artifacts(force=True)
    if not state.ready:
        log.warning(
            "[models_reload] skipping github mirror; loader not ready (error=%s)",
            state.error,
        )
        return
    if not github_mirror.is_enabled():
        log.info("[models_reload] github mirror disabled (no GITHUB_MIRROR_REPO/TOKEN)")
        return
    try:
        result = await github_mirror.mirror_active_model()
        log.info("[models_reload] github mirror result: %s", result.as_dict())
    except Exception:
        log.exception("[models_reload] github mirror raised unexpectedly")


@app.post("/api/models/reload")
async def models_reload(_: object = Depends(require_auth)) -> dict[str, object]:
    """Force a full reload: clears the in-memory state + cached predictor
    objects and re-runs the reassemble/extract/scan/smoke-load pipeline.
    Cached on-disk archives are reused if intact.

    On a successful reload, the loaded artifacts + manifest are mirrored to
    the configured private GitHub repo (best-effort, non-blocking).
    """
    predictor.invalidate()
    asyncio.create_task(_reload_and_mirror())
    return {
        "started": True,
        "state": models_loader.get_state().as_dict(),
        "mirror_enabled": github_mirror.is_enabled(),
    }


@app.get("/api/admin/models/mirror/status")
async def admin_models_mirror_status(
    _: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Report whether the GitHub mirror is configured and the result of the
    most recent push attempt. Admin-only because the response surfaces
    repo identity + push errors that include credentials in some failure
    paths."""
    return {
        "enabled": github_mirror.is_enabled(),
        "config": github_mirror.config_summary(),
        "last_result": github_mirror.last_result(),
        "persisted_state": github_mirror.last_persisted_state(),
    }


@app.post("/api/admin/models/mirror/push")
async def admin_models_mirror_push(
    _: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Force-push the currently-loaded model artifacts to the GitHub mirror,
    ignoring the last-mirrored version state. Synchronous so the caller
    sees the outcome directly; intended for manual recovery / first-time
    setup. Admin-only because each call burns GitHub Contents API quota
    against a privileged backend token — exposing this to general
    authenticated users would let any signed-in user exhaust the rate
    limit or force unwanted writes."""
    if not github_mirror.is_enabled():
        raise HTTPException(
            status_code=400,
            detail="github mirror is not configured (set GITHUB_MIRROR_REPO and GITHUB_TOKEN)",
        )
    result = await github_mirror.mirror_active_model(force=True)
    return result.as_dict()


class PredictRequest(BaseModel):
    """Optional payload for `/api/models/predict`.

    `features` may be a list of floats or a {name: value} mapping. When
    omitted the endpoint runs a smoke prediction with a zero-vector of
    the model's expected width.
    """
    features: list[float] | dict[str, float] | None = None


@app.post("/api/models/predict")
async def models_predict(
    body: PredictRequest | None = None,
    _: object = Depends(require_auth),
) -> dict[str, object]:
    """Run the active canonical predictor and return the prediction
    plus the model version that produced it. Acts as a smoke test for
    the predictor service when called with no payload."""
    features = body.features if body is not None else None
    try:
        result = await predictor.predict(features)
    except predictor.PredictorUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except predictor.PredictorInputError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        log.exception("/api/models/predict failed")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    return {
        "ok": True,
        "prediction": result.as_dict(),
        "active_predictor": predictor.active_model_info(),
    }


@app.get("/api/models/inventory")
async def models_inventory(_: object = Depends(require_approved_user)) -> dict[str, object]:
    """Report the artifact archives present in the storage bucket.

    The production bucket layout is:
        exports/models_part_{aa,ab,ac}   trained-model archive (multi-part)
        exports/source_part_{aa,ab,ac}   full Python source archive (multi-part)
        cutover/                         cutover scripts
    """
    try:
        roots = await storage.list_path("", limit=200)
        exports = await storage.list_path("exports", limit=500)
        cutover = await storage.list_path("cutover", limit=200)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"storage error: {e}")

    def total(items, prefix):
        return sum((it.size or 0) for it in items if it.name.startswith(prefix))

    return {
        "buckets": [{"name": e.name, "is_folder": e.is_folder} for e in roots],
        "exports": {
            "files": [
                {"name": e.name, "size": e.size, "updated_at": e.updated_at}
                for e in exports if not e.is_folder
            ],
            "models_archive_total_bytes": total(exports, "models_part_"),
            "source_archive_total_bytes": total(exports, "source_part_"),
        },
        "cutover": [
            {"name": e.name, "size": e.size, "updated_at": e.updated_at}
            for e in cutover if not e.is_folder
        ],
    }


# ---------------------------------------------------------------------------
# Per-channel WebSocket split (task #245 step 4)
# ---------------------------------------------------------------------------
#
# These endpoints are thin filters on top of the shared
# `pipeline_event_hub` + `settlement_event_hub` + `kalshi_bus` hubs.
# Each one subscribes once, then forwards only the event types its
# channel cares about.  A buggy or stuck client on any one channel
# can't impact the multiplexer or its sibling channels — the bounded
# subscriber queue + slow-subscriber-drop policy in the hubs takes
# care of that.
#
# We keep `/api/ws/pipeline` as the multiplexer for backwards compat;
# everything below is purely additive.


_POSITIONS_TYPES = frozenset({
    "candidate_complete",   # placed = true means a position changed
    "trade_settled",        # closes / partial closes a position
})
_TRADES_TYPES = frozenset({
    "candidate_complete",
    "trade_settled",
    "market_settle_complete",
})
_ALERTS_TYPES = frozenset({
    "cycle_halted",
    "market_settle_error",
    "retrain_rolled_back",
    "retrain_failed",
})


async def _filtered_ws(
    ws: WebSocket,
    *,
    hub_subscribe,
    hub_unsubscribe,
    keep: callable,
    label: str,
) -> None:
    """Generic filtered WebSocket loop used by the per-channel splits.

    ``hub_subscribe`` returns an asyncio.Queue (sync or coroutine);
    ``hub_unsubscribe`` releases it.  ``keep`` is a predicate over
    each event dict — only matching events are forwarded to the
    client.
    """
    from .auth import ws_authenticate

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    sub = hub_subscribe()
    if asyncio.iscoroutine(sub):
        q = await sub
    else:
        q = sub
    try:
        while True:
            event = await q.get()
            try:
                if keep(event):
                    await ws.send_json(event)
            except Exception as e:
                log.debug("ws/%s filter/send failed: %s", label, e)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws/%s send failed: %s", label, e)
    finally:
        try:
            res = hub_unsubscribe(q)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            pass


@app.websocket("/api/ws/positions")
async def ws_positions(ws: WebSocket) -> None:
    """Filtered stream of position-affecting events (placed orders +
    settled trades).  Subscribes to the pipeline + settlement hubs
    and forwards only the relevant event types.
    """
    from .auth import ws_authenticate
    from .ws_backpressure import SubscriberOverflow, OVERFLOW_CLOSE_CODE, OVERFLOW_CLOSE_REASON

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    p_q = pipeline_event_hub.subscribe(label="positions")
    s_q = settlement_event_hub.subscribe(label="positions")
    try:
        async def _drain(q, types):
            while True:
                ev = await q.get()
                t = ev.get("type")
                if isinstance(t, str) and t in types:
                    placed = ev.get("placed")
                    if t == "candidate_complete" and placed is False:
                        continue
                    await ws.send_json(ev)

        await asyncio.gather(
            _drain(p_q, _POSITIONS_TYPES),
            _drain(s_q, _POSITIONS_TYPES),
        )
    except WebSocketDisconnect:
        pass
    except SubscriberOverflow as e:
        log.warning("ws/positions overflow: %s", e)
        try:
            await ws.close(code=OVERFLOW_CLOSE_CODE, reason=OVERFLOW_CLOSE_REASON)
        except Exception:
            pass
    except Exception as e:
        log.warning("ws/positions error: %s", e)
    finally:
        pipeline_event_hub.unsubscribe(p_q)
        settlement_event_hub.unsubscribe(s_q)


@app.websocket("/api/ws/trades")
async def ws_trades(ws: WebSocket) -> None:
    """Filtered stream of trade lifecycle events: placed, settled,
    market resolved.  Mirror of /ws/positions with a wider filter.
    """
    from .auth import ws_authenticate
    from .ws_backpressure import SubscriberOverflow, OVERFLOW_CLOSE_CODE, OVERFLOW_CLOSE_REASON

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    p_q = pipeline_event_hub.subscribe(label="trades")
    s_q = settlement_event_hub.subscribe(label="trades")
    try:
        async def _drain(q):
            while True:
                ev = await q.get()
                t = ev.get("type")
                if isinstance(t, str) and t in _TRADES_TYPES:
                    await ws.send_json(ev)

        await asyncio.gather(_drain(p_q), _drain(s_q))
    except WebSocketDisconnect:
        pass
    except SubscriberOverflow as e:
        log.warning("ws/trades overflow: %s", e)
        try:
            await ws.close(code=OVERFLOW_CLOSE_CODE, reason=OVERFLOW_CLOSE_REASON)
        except Exception:
            pass
    except Exception as e:
        log.warning("ws/trades error: %s", e)
    finally:
        pipeline_event_hub.unsubscribe(p_q)
        settlement_event_hub.unsubscribe(s_q)


@app.websocket("/api/ws/alerts")
async def ws_alerts(ws: WebSocket) -> None:
    """Filtered stream of operator-facing alerts: kill-switch trips,
    settlement errors, drift rollbacks.  Sub-Pro tier OK so any
    signed-in user can tail their own alerts page.
    """
    from .auth import ws_authenticate
    from .ws_backpressure import SubscriberOverflow, OVERFLOW_CLOSE_CODE, OVERFLOW_CLOSE_REASON

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    # Alerts are low-rate but high-importance — close on overflow so a
    # stuck operator dashboard stops silently dropping kill-switch
    # notifications.
    p_q = pipeline_event_hub.subscribe(label="alerts", policy="close")
    s_q = settlement_event_hub.subscribe(label="alerts", policy="close")
    try:
        async def _drain(q):
            while True:
                ev = await q.get()
                t = ev.get("type")
                if isinstance(t, str) and t in _ALERTS_TYPES:
                    await ws.send_json(ev)

        await asyncio.gather(_drain(p_q), _drain(s_q))
    except WebSocketDisconnect:
        pass
    except SubscriberOverflow as e:
        log.warning("ws/alerts overflow: %s", e)
        try:
            await ws.close(code=OVERFLOW_CLOSE_CODE, reason=OVERFLOW_CLOSE_REASON)
        except Exception:
            pass
    except Exception as e:
        log.warning("ws/alerts error: %s", e)
    finally:
        pipeline_event_hub.unsubscribe(p_q)
        settlement_event_hub.unsubscribe(s_q)


@app.websocket("/api/ws/orderbook/{ticker}")
async def ws_orderbook(ws: WebSocket, ticker: str) -> None:
    """Per-ticker orderbook stream.  Subscribes to the in-process
    Kalshi event bus and forwards only orderbook deltas for the
    requested ticker.  Auth identical to /ws/kalshi.
    """
    from .auth import ws_authenticate
    from .ws_backpressure import SubscriberOverflow, OVERFLOW_CLOSE_CODE, OVERFLOW_CLOSE_REASON

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    target = (ticker or "").strip().upper()
    if not target:
        try:
            await ws.send_json({"type": "error", "error": "missing ticker"})
        except Exception:
            pass
        await ws.close(code=4400)
        return
    # Stale orderbook deltas are worse than no orderbook, so close on
    # overflow rather than silently dropping the head.
    q = await kalshi_bus.subscribe(label=f"orderbook:{target}", policy="close")
    try:
        while True:
            msg = await q.get()
            mt = msg.get("type") if isinstance(msg, dict) else None
            if mt not in {"orderbook_snapshot", "orderbook_delta", "orderbook"}:
                continue
            mticker = (
                msg.get("market_ticker") or msg.get("ticker") or ""
            ).upper()
            if mticker == target:
                await ws.send_json(msg)
    except WebSocketDisconnect:
        pass
    except SubscriberOverflow as e:
        log.warning("ws/orderbook overflow: %s", e)
        try:
            await ws.close(code=OVERFLOW_CLOSE_CODE, reason=OVERFLOW_CLOSE_REASON)
        except Exception:
            pass
    except Exception as e:
        log.warning("ws/orderbook error: %s", e)
    finally:
        await kalshi_bus.unsubscribe(q)


@app.get("/api/weather/freshness")
async def weather_freshness(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    """Per-city × per-source freshness snapshot for the operator
    Weather page.  Surfaces the latest observation timestamp for each
    source (open-meteo / nws / tomorrow.io / accuweather / hrrr) plus
    a fresh / stale / missing badge classification, and — for HRRR —
    the NOMADS run cycle (e.g. ``"12Z"``) we last pulled.
    """
    from .feeds.weather_payload import build_freshness_summary
    return await build_freshness_summary()


@app.websocket("/api/ws/temperature/{city}")
async def ws_temperature(ws: WebSocket, city: str) -> None:
    """Per-city weather stream.  Polls the latest open-meteo /
    NWS / HRRR row for the requested city every 30s and pushes the
    delta when any field changes.  This is intentionally a polling
    loop on the WS thread rather than a hub fanout — the underlying
    feed schedulers run at 5-minute cadence so a higher rate would
    just be re-sending the same row.
    """
    from .auth import ws_authenticate

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    city_code = (city or "").strip().upper()
    if not city_code:
        await ws.close(code=4400)
        return
    last_payload: dict | None = None
    try:
        while True:
            try:
                # `weather_open_meteo` is a feed-registry name, not a
                # table; the actual rows live in `weather_observations`
                # (temperature) joined to `weather_extended` (wind /
                # precipitation / cloud cover etc).  Pull the most
                # recent observation for the requested city across all
                # sources and join the matching extended row by source +
                # observed_at when available.
                row = await db.fetchrow(
                    """
                    SELECT  o.city_code,
                            o.observed_at,
                            o.temperature_f,
                            o.source,
                            x.wind_mph,
                            x.precip_1hr_in AS precip_in,
                            x.cloud_cover_pct,
                            x.humidity_pct
                      FROM  weather_observations o
                       LEFT JOIN weather_extended x
                              ON x.city_code   = o.city_code
                             AND x.source      = o.source
                             AND x.observed_at = o.observed_at
                     WHERE  o.city_code = $1
                     ORDER BY o.observed_at DESC
                     LIMIT  1
                    """,
                    city_code,
                )
            except Exception as e:
                log.debug("ws/temperature db read failed: %s", e)
                row = None
            payload = (
                {
                    "type": "temperature",
                    "city": city_code,
                    "observed_at": (
                        row["observed_at"].isoformat()
                        if row and row.get("observed_at") else None
                    ),
                    "temperature_f": (
                        float(row["temperature_f"])
                        if row and row.get("temperature_f") is not None else None
                    ),
                    "wind_mph": (
                        float(row["wind_mph"])
                        if row and row.get("wind_mph") is not None else None
                    ),
                    "precip_in": (
                        float(row["precip_in"])
                        if row and row.get("precip_in") is not None else None
                    ),
                    "conditions": row.get("conditions") if row else None,
                }
                if row
                else {"type": "temperature", "city": city_code, "status": "no_data"}
            )
            if payload != last_payload:
                await ws.send_json(payload)
                last_payload = payload
            await asyncio.sleep(30.0)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws/temperature error: %s", e)


# ---------------------------------------------------------------------------
# Clerk webhook (task #245 step 5)
# ---------------------------------------------------------------------------


@app.post("/api/webhooks/clerk")
async def webhooks_clerk(request: Request) -> dict[str, object]:
    """Clerk webhook receiver.  Verifies the Svix signature and keeps
    `app_users` in sync with profile changes (`user.updated`,
    `user.deleted`) so the user does not have to log out and back in
    for an email/name change to be reflected in the operator console.

    The webhook secret is `CLERK_WEBHOOK_SECRET`.  When unset, this
    endpoint refuses with 503 — same posture as the Stripe webhook —
    so an unconfigured deployment can't be tricked by a forged event.
    """
    secret = os.environ.get("CLERK_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="clerk webhook not configured")

    raw = await request.body()
    headers = {
        "svix-id": request.headers.get("svix-id") or "",
        "svix-timestamp": request.headers.get("svix-timestamp") or "",
        "svix-signature": request.headers.get("svix-signature") or "",
    }
    if not all(headers.values()):
        raise HTTPException(status_code=400, detail="missing svix headers")

    # Manual Svix HMAC-SHA256 verification.  Avoids adding the `svix`
    # package to the runtime — Svix's signature scheme is a stable
    # contract: secret is `whsec_<base64>`; signed content is
    # `${id}.${timestamp}.${raw_body}`; signature header carries one
    # or more space-separated `v1,<base64sig>` values.
    import base64 as _b64
    import hashlib
    import hmac as _hmac

    sec = secret
    if sec.startswith("whsec_"):
        sec = sec[len("whsec_"):]
    try:
        secret_bytes = _b64.b64decode(sec)
    except Exception:
        log.error("CLERK_WEBHOOK_SECRET malformed (not base64)")
        raise HTTPException(status_code=503, detail="webhook secret malformed")
    # Reject obviously stale events to defeat replay attacks.
    try:
        ts = int(headers["svix-timestamp"])
    except Exception:
        raise HTTPException(status_code=400, detail="invalid svix-timestamp")
    if abs(time.time() - ts) > 5 * 60:
        raise HTTPException(status_code=400, detail="svix-timestamp out of window")
    # Concatenate raw bytes — Svix signs the original request body, so a
    # UTF-8 round-trip on `raw` would corrupt the signature for any payload
    # containing multi-byte characters (e.g. emoji in a display name).
    signed_payload = (
        headers["svix-id"].encode("ascii")
        + b"."
        + str(ts).encode("ascii")
        + b"."
        + raw
    )
    expected = _b64.b64encode(
        _hmac.new(secret_bytes, signed_payload, hashlib.sha256).digest()
    ).decode()
    presented = [
        s.split(",", 1)[1]
        for s in headers["svix-signature"].split(" ")
        if s.startswith("v1,")
    ]
    if not any(_hmac.compare_digest(expected, p) for p in presented):
        log.warning("clerk webhook signature mismatch")
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        import json as _json
        evt = _json.loads(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad json: {e}")

    event_type = (evt.get("type") if isinstance(evt, dict) else "") or ""
    data = (evt.get("data") if isinstance(evt, dict) else {}) or {}
    user_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(user_id, str) or not user_id:
        return {"ok": True, "ignored": True, "reason": "no user id"}

    if event_type == "user.deleted":
        try:
            await db.execute(
                "UPDATE app_users SET approved = false, is_admin = false, "
                "updated_at = now() WHERE clerk_user_id = $1",
                user_id,
            )
        except Exception as e:
            log.warning("clerk webhook delete failed: %s", e)
        return {"ok": True, "type": event_type, "user_id": user_id}

    if event_type in {"user.created", "user.updated"}:
        primary_id = data.get("primary_email_address_id")
        addresses = data.get("email_addresses") or []
        email: str | None = None
        if isinstance(addresses, list):
            for entry in addresses:
                if not isinstance(entry, dict):
                    continue
                if entry.get("id") == primary_id or email is None:
                    v = entry.get("email_address")
                    if isinstance(v, str) and v.strip():
                        email = v.strip().lower()
                        if entry.get("id") == primary_id:
                            break
        first = (data.get("first_name") or "").strip()
        last = (data.get("last_name") or "").strip()
        name: str | None = (first + " " + last).strip() or None
        if not name:
            uname = data.get("username")
            if isinstance(uname, str) and uname.strip():
                name = uname.strip()
        try:
            await admin_module.upsert_seen_user(user_id, email, name)
        except Exception as e:
            log.warning("clerk webhook upsert failed: %s", e)
        # Invalidate the per-user Clerk cache so the next /api/me/access
        # call sees the fresh email/name without waiting for the 60s TTL.
        try:
            from .auth import _user_cache
            _user_cache.pop(user_id, None)
        except Exception:
            pass
        return {"ok": True, "type": event_type, "user_id": user_id, "email": email}

    return {"ok": True, "type": event_type, "ignored": True}


# ---------------------------------------------------------------------------
# Calibration + Research dashboards (task #245 steps 6 + 7)
# ---------------------------------------------------------------------------


@app.get("/api/calibration/snapshots")
async def calibration_snapshots(
    vertical: str | None = None,
    limit: int = 50,
    _: object = Depends(require_auth),
) -> dict[str, object]:
    """Recent calibration snapshots from the settlement loop.

    Optional ``vertical`` filter narrows the result set.  Each row
    carries the post-calibration Brier, ECE/MCE proxies, sample
    count, drift_status, and the isotonic knot points (so the page
    can render a reliability diagram client-side).
    """
    n = max(1, min(200, int(limit)))
    try:
        if vertical:
            rows = await db.fetch(
                """
                SELECT id, vertical, sub_category, ece, mce, brier_score,
                       reliability, resolution, sample_count,
                       calibrator_state, drift_status, config, created_at
                  FROM calibration_snapshots
                 WHERE vertical = $1
                 ORDER BY created_at DESC
                 LIMIT $2
                """,
                vertical, n,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, vertical, sub_category, ece, mce, brier_score,
                       reliability, resolution, sample_count,
                       calibrator_state, drift_status, config, created_at
                  FROM calibration_snapshots
                 ORDER BY created_at DESC
                 LIMIT $1
                """,
                n,
            )
    except Exception as e:
        log.warning("calibration snapshots query failed: %s", e)
        return {"items": [], "count": 0, "error": str(e)[:240]}
    out: list[dict[str, object]] = []
    for r in rows:
        out.append({
            "id": int(r["id"]),
            "vertical": r["vertical"],
            "sub_category": r["sub_category"],
            "ece": float(r["ece"]) if r["ece"] is not None else None,
            "mce": float(r["mce"]) if r["mce"] is not None else None,
            "brier_score": (
                float(r["brier_score"]) if r["brier_score"] is not None else None
            ),
            "sample_count": int(r["sample_count"] or 0),
            "drift_status": r["drift_status"],
            "calibrator_state": r["calibrator_state"],
            "config": r["config"],
            "created_at": (
                r["created_at"].isoformat() if r["created_at"] else None
            ),
        })
    return {"items": out, "count": len(out)}


@app.get("/api/research/directives")
async def research_directives_list(
    status: str | None = None,
    limit: int = 100,
    _: object = Depends(require_auth),
) -> dict[str, object]:
    """List research directives (open / investigating / resolved /
    dismissed).  Default returns the most-recent ``limit`` rows
    regardless of status; pass ``?status=open`` for the queue view.
    """
    n = max(1, min(500, int(limit)))
    try:
        if status:
            rows = await db.fetch(
                """
                SELECT id, vertical, title, hypothesis, evidence,
                       status, priority, source, created_at,
                       updated_at, resolved_at, resolution_note
                  FROM research_directives
                 WHERE status = $1
                 ORDER BY created_at DESC
                 LIMIT $2
                """,
                status, n,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, vertical, title, hypothesis, evidence,
                       status, priority, source, created_at,
                       updated_at, resolved_at, resolution_note
                  FROM research_directives
                 ORDER BY created_at DESC
                 LIMIT $1
                """,
                n,
            )
    except Exception as e:
        log.warning("research directives query failed: %s", e)
        return {"items": [], "count": 0, "error": str(e)[:240]}
    return {
        "items": [
            {
                "id": int(r["id"]),
                "vertical": r["vertical"],
                "title": r["title"],
                "hypothesis": r["hypothesis"],
                "evidence": r["evidence"],
                "status": r["status"],
                "priority": r["priority"],
                "source": r["source"],
                "created_at": (
                    r["created_at"].isoformat() if r["created_at"] else None
                ),
                "updated_at": (
                    r["updated_at"].isoformat() if r["updated_at"] else None
                ),
                "resolved_at": (
                    r["resolved_at"].isoformat() if r["resolved_at"] else None
                ),
                "resolution_note": r["resolution_note"],
            }
            for r in rows
        ],
        "count": len(rows),
    }


class ResearchDirectiveDecisionRequest(BaseModel):
    decision: str = Field(..., min_length=1)
    note: str | None = Field(None, max_length=2000)


@app.post("/api/research/directives/{directive_id}/decision")
async def research_directives_decide(
    directive_id: int,
    body: ResearchDirectiveDecisionRequest,
    actor: AuthedUser = Depends(require_admin),
) -> dict[str, object]:
    """Operator decision on a research directive.

    ``decision`` is one of ``investigating``, ``resolved``, ``dismissed``,
    ``reopen``.  ``note`` is captured in ``resolution_note`` for the
    terminal states; ignored for ``investigating`` / ``reopen``.
    """
    decision = (body.decision or "").strip().lower()
    if decision not in {"investigating", "resolved", "dismissed", "reopen"}:
        raise HTTPException(status_code=422, detail=f"unknown decision: {decision}")
    next_status = {
        "investigating": "investigating",
        "resolved": "resolved",
        "dismissed": "dismissed",
        "reopen": "open",
    }[decision]
    is_terminal = decision in {"resolved", "dismissed"}
    try:
        row = await db.fetchrow(
            """
            UPDATE research_directives
               SET status = $1,
                   updated_at = now(),
                   resolved_at = CASE WHEN $2 THEN now() ELSE NULL END,
                   resolution_note = CASE WHEN $2 THEN $3 ELSE NULL END
             WHERE id = $4
             RETURNING id, status, updated_at
            """,
            next_status, is_terminal, body.note, directive_id,
        )
    except Exception as e:
        log.exception("research directive decision failed")
        raise HTTPException(status_code=502, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail="directive not found")
    log.info(
        "research directive %s -> %s by %s", directive_id, next_status, actor.user_id,
    )
    return {
        "ok": True,
        "id": int(row["id"]),
        "status": row["status"],
        "updated_at": (
            row["updated_at"].isoformat() if row["updated_at"] else None
        ),
    }
