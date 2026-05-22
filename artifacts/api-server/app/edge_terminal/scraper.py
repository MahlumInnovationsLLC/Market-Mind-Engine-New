"""Playwright-driven scrape of https://app.edgeterminal.ai/dashboard.

This module is intentionally isolated from the rest of the package
behind a single public coroutine :func:`scrape_signals` so the poller
treats scraper failures as transient (logged, status row updated,
nothing fires).

Auth comes from the ``EDGE_TERMINAL_EMAIL`` / ``EDGE_TERMINAL_PASSWORD``
environment secrets only — credentials are never read from the DB or
written to the repo.

DOM contract (subject to refinement against the live site):

  * The dashboard renders one tile per pick.  Tiles carrying a
    "signal" badge are the ones we want to fire on.  Selector:
    ``[data-axiom-signal="true"], .signal-badge, .has-signal`` —
    superset matched with ``OR`` so a UI refresh that renames the
    class still resolves.
  * Each tile exposes ``vertical``, ``matchup``, ``selection``,
    ``model_prob``, ``market_price`` (or the Edge Terminal-native
    edge%).  We read whichever attributes are present and fall back
    to text parsing — see :func:`_extract_tile`.

The scraper is intentionally tolerant: missing optional fields are
left as ``None``.  The poller still records the row so an operator
can audit what the scraper saw.

If Playwright is not installed or chromium is unavailable the
scraper returns an empty list and the poller surfaces a clear
``last_error`` so the dashboard greys the tile.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any


log = logging.getLogger(__name__)


EDGE_TERMINAL_URL = "https://app.edgeterminal.ai/dashboard"
EDGE_TERMINAL_LOGIN_URL = "https://app.edgeterminal.ai/login"
# Pin the navigation budget so a hung site never wedges the 60s poller.
_NAV_TIMEOUT_MS = 25_000


class ScraperNotConfigured(RuntimeError):
    """Raised when credentials are missing.  The poller catches this
    and surfaces it on the status row so the operator sees a clear
    "set EDGE_TERMINAL_EMAIL/PASSWORD" message instead of a stack."""


class ScraperUnavailable(RuntimeError):
    """Raised when Playwright / chromium isn't installed.  Recoverable
    via ``pip install playwright && playwright install chromium`` —
    the poller logs and the dashboard greys the tile."""


def _credentials() -> tuple[str, str]:
    email = (os.environ.get("EDGE_TERMINAL_EMAIL") or "").strip()
    password = os.environ.get("EDGE_TERMINAL_PASSWORD") or ""
    if not email or not password:
        raise ScraperNotConfigured(
            "EDGE_TERMINAL_EMAIL / EDGE_TERMINAL_PASSWORD secrets are not set"
        )
    return email, password


def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def scrape_signals() -> list[dict[str, Any]]:
    """Log in to Edge Terminal, return all signal-badged picks.

    Returns a list of dicts with the dedupe keys (``vertical``,
    ``matchup``, ``selection``, ``signal_date``) plus any optional
    enrichment (``edge_pct``, ``model_prob``, ``market_price``,
    ``market_title``).  Empty list = "scrape ran clean but no
    badged signals right now".
    """
    email, password = _credentials()
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError as e:
        raise ScraperUnavailable(
            "playwright not installed — `pip install playwright && "
            "playwright install chromium`"
        ) from e

    out: list[dict[str, Any]] = []
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as e:
            raise ScraperUnavailable(f"chromium launch failed: {e}") from e
        try:
            ctx = await browser.new_context()
            page = await ctx.new_page()
            page.set_default_timeout(_NAV_TIMEOUT_MS)

            # ---- Login ----
            await page.goto(EDGE_TERMINAL_LOGIN_URL, wait_until="domcontentloaded")
            # Login selector contract (refine against live DOM):
            #   email input        → input[type=email], input[name=email]
            #   password input     → input[type=password], input[name=password]
            #   submit button      → button[type=submit], button:has-text("Sign in")
            for sel in ("input[type=email]", "input[name=email]"):
                if await page.locator(sel).count():
                    await page.fill(sel, email)
                    break
            for sel in ("input[type=password]", "input[name=password]"):
                if await page.locator(sel).count():
                    await page.fill(sel, password)
                    break
            for sel in (
                "button[type=submit]",
                "button:has-text('Sign in')",
                "button:has-text('Log in')",
            ):
                if await page.locator(sel).count():
                    await page.locator(sel).first.click()
                    break

            # Wait for the dashboard to be reachable post-login.
            try:
                await page.wait_for_url("**/dashboard**", timeout=_NAV_TIMEOUT_MS)
            except Exception:
                # Some flows land on `/` first.  Navigate manually.
                await page.goto(EDGE_TERMINAL_URL, wait_until="domcontentloaded")

            # ---- Scrape signal-badged tiles ----
            #
            # Superset selector: any element whose data-axiom-signal
            # attribute is "true" OR whose class list contains a
            # "signal" badge.  Operators can refine this once they
            # see the live DOM.
            tile_selector = (
                "[data-axiom-signal='true'], "
                ".signal-badge, .has-signal, "
                "[data-signal='true']"
            )
            try:
                await page.wait_for_selector(tile_selector, timeout=10_000)
            except Exception:
                # No signals on this poll — return empty cleanly.
                return []
            handles = await page.locator(tile_selector).all()
            for h in handles:
                try:
                    row = await _extract_tile(h)
                except Exception as e:  # pragma: no cover — per-tile guard
                    log.warning("edge_terminal tile parse failed: %s", e)
                    continue
                if row is None:
                    continue
                out.append(row)
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return out


async def _extract_tile(handle: Any) -> dict[str, Any] | None:
    """Pull dedupe keys + optional fields off a single signal tile.

    Returns ``None`` when the tile is missing mandatory fields (the
    poller silently drops these — they show up in scraper logs but
    not in the signals table).
    """
    # Try data-attribute contract first (cheapest + most robust to UI
    # restyles); fall back to text content.
    async def _attr(name: str) -> str | None:
        try:
            v = await handle.get_attribute(f"data-{name}")
        except Exception:
            return None
        return v.strip() if isinstance(v, str) and v.strip() else None

    vertical = await _attr("vertical")
    matchup = await _attr("matchup")
    selection = await _attr("selection")
    if not (vertical and matchup and selection):
        # Text-based fallback — extract from common label patterns.
        try:
            text = await handle.inner_text()
        except Exception:
            text = ""
        # Defensive parse: "<vertical> | <matchup> | <selection>"
        # is a stable substring format some Edge Terminal tiles use.
        parts = [p.strip() for p in (text or "").split("|") if p.strip()]
        if len(parts) >= 3:
            vertical = vertical or parts[0]
            matchup = matchup or parts[1]
            selection = selection or parts[2]
    if not (vertical and matchup and selection):
        return None

    def _maybe_float(v: str | None) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "vertical": vertical.lower(),
        "matchup": matchup,
        "selection": selection,
        "signal_date": _today_utc_iso(),
        "edge_pct": _maybe_float(await _attr("edge")),
        "model_prob": _maybe_float(await _attr("model-prob")),
        "market_price": _maybe_float(await _attr("market-price")),
        "kalshi_ticker": await _attr("kalshi-ticker"),
        "market_title": await _attr("market-title"),
        "raw": {},  # filled by poller with the source tile text if needed
    }
