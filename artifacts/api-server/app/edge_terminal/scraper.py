"""Playwright-driven scrape of https://app.edgeterminal.ai/dashboard.

This module is intentionally isolated from the rest of the package
behind a single public coroutine :func:`scrape_signals` so the poller
treats scraper failures as transient (logged, status row updated,
nothing fires).

Auth comes from the ``EDGE_TERMINAL_EMAIL`` / ``EDGE_TERMINAL_PASSWORD``
environment secrets only — credentials are never read from the DB or
written to the repo.

DOM contract (verified live against app.edgeterminal.ai 2026-05-26):

  * Sport sections render as a ``.section-header`` element (e.g.
    text "NHL — PICKS RANKED BY EDGE") followed by a sequence of
    ``div.game-row`` siblings/descendants.  We walk the document
    in source order to map each row to its preceding header.
  * Signal-badged rows are ``div.game-row`` elements that contain
    a ``span.signal-tag`` child whose text reads "SIGNAL".  Non-
    signal rows are ignored.
  * Each signal row exposes its data positionally via class names:

      .matchup .matchup-text   "Carolina Hurricanes @ Montréal Canadiens"
      .market (first)          "SPREAD" / "TOTAL" / "ML"
      .odds                    "-184"   (American odds)
      .pick                    "Canadiens +1.5" / "OVER 25.5"
      .market (second)         "81%"    (model win probability)
      .edge-bar .ev            "+25.1%" (edge / EV%)

  * The legacy ``data-axiom-signal`` / ``data-*`` attribute
    contract is also still supported via :func:`_extract_tile`,
    which the fixture tests exercise.  If the live site ever
    adds those attributes back we'll prefer them over the
    positional parse.

The scraper is intentionally tolerant: missing optional fields are
left as ``None``.  The poller still records the row so an operator
can audit what the scraper saw.

If Playwright is not installed or chromium is unavailable the
scraper returns an empty list and the poller surfaces a clear
``last_error`` so the dashboard greys the tile.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any


log = logging.getLogger(__name__)


EDGE_TERMINAL_URL = "https://app.edgeterminal.ai/dashboard"
EDGE_TERMINAL_LOGIN_URL = "https://app.edgeterminal.ai/login"
# Pin the navigation budget so a hung site never wedges the 60s poller.
_NAV_TIMEOUT_MS = 60_000


# ---------------------------------------------------------------------
# Scrape outcome categories (task #950).
#
# Every poll tick resolves to exactly one of these so the operator can
# tell *why* a tick produced no trades without grepping logs.  Surfaced
# on the poller status row (``last_error_category``), in poller logs, on
# the dashboard tile, and by the admin self-check.
# ---------------------------------------------------------------------
SCRAPE_OK = "ok"                       # logged in, rows present (>=0 signals)
SCRAPE_NOT_CONFIGURED = "not_configured"  # creds missing
SCRAPE_BROWSER_MISSING = "browser_missing"  # playwright/chromium unavailable
SCRAPE_LOGIN_FAILED = "login_failed"   # creds rejected / still on /login
SCRAPE_NO_ROWS = "no_rows"             # logged in but 0 game rows → likely DOM change
SCRAPE_ERROR = "error"                 # anything else

VALID_SCRAPE_CATEGORIES = (
    SCRAPE_OK,
    SCRAPE_NOT_CONFIGURED,
    SCRAPE_BROWSER_MISSING,
    SCRAPE_LOGIN_FAILED,
    SCRAPE_NO_ROWS,
    SCRAPE_ERROR,
)


class ScraperNotConfigured(RuntimeError):
    """Raised when credentials are missing.  The poller catches this
    and surfaces it on the status row so the operator sees a clear
    "set EDGE_TERMINAL_EMAIL/PASSWORD" message instead of a stack."""

    category = SCRAPE_NOT_CONFIGURED


class ScraperUnavailable(RuntimeError):
    """Raised when Playwright / chromium isn't installed or the browser
    fails to launch.  Recoverable via ``pip install playwright &&
    playwright install chromium`` — the poller logs and the dashboard
    greys the tile."""

    category = SCRAPE_BROWSER_MISSING


class ScraperLoginFailed(ScraperUnavailable):
    """Raised when the browser launched fine but the Edge Terminal
    login did not take (bad credentials, captcha, or the post-login
    redirect never left ``/login``).  Subclasses ``ScraperUnavailable``
    so existing ``except ScraperUnavailable`` callers still catch it,
    but the poller handles it first to record the precise category."""

    category = SCRAPE_LOGIN_FAILED


class ScraperDOMChanged(RuntimeError):
    """Raised when login succeeded and the dashboard loaded but zero
    ``div.game-row`` elements rendered.  Edge Terminal is a 24/7 board,
    so an empty grid almost always means the source markup/selectors
    changed — surface it loudly instead of silently returning ``[]``."""

    category = SCRAPE_NO_ROWS


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

            # Verify the login actually succeeded — if the page is
            # still on the login URL (bad credentials, captcha, etc.)
            # raise a clear error so the dashboard greys the tile with
            # an actionable message instead of silently returning [].
            try:
                final_url = page.url or ""
            except Exception:
                final_url = ""
            if "/login" in final_url.lower():
                raise ScraperLoginFailed(
                    "edge terminal login failed — check EDGE_TERMINAL_EMAIL "
                    "/ EDGE_TERMINAL_PASSWORD secrets"
                )

            # ---- Scrape signal-badged tiles ----
            #
            # Wait for the dashboard's row grid to render at all.  We
            # don't gate on the signal selector here because "no
            # signals right now" is a valid steady state and we want
            # to return cleanly in that case, not stall the poller.
            try:
                await page.wait_for_selector("div.game-row", timeout=10_000)
            except Exception as e:
                raise ScraperDOMChanged(
                    "logged in but found 0 game rows (div.game-row) — Edge "
                    "Terminal markup/selectors likely changed"
                ) from e
            # The row grid renders before the per-row `.signal-tag`
            # chips hydrate — gating only on `div.game-row` causes the
            # poller to fire the JS extract too early and find 0
            # signals on every cycle.  Wait briefly for at least one
            # signal chip; a timeout here is a legitimate
            # "no signals right now" state, so swallow it and let the
            # JS extract return [].
            try:
                await page.wait_for_selector(".signal-tag", timeout=5_000)
            except Exception:
                pass
            # Single JS round-trip pulls every signal row + its
            # preceding section header in document order.  Doing this
            # in one evaluate (vs. one round-trip per handle/attr) is
            # ~50x faster on a dashboard with ~40 signals and avoids
            # serialising Playwright IPC for every field.
            structured = await page.evaluate(_SIGNAL_EXTRACT_JS)
            for raw in structured:
                row = _row_from_structured(raw)
                if row is None:
                    continue
                out.append(row)
        finally:
            # Bound the close: a wedged chromium ``browser.close()`` has no
            # internal timeout and will hang the awaiting poller task
            # *forever* (the rest of the process — incl. the healthz
            # sidecar — keeps answering, so the watchdog never restarts and
            # the poller silently stops scraping with status frozen on its
            # last successful tick).  Cap it so a stuck close can't wedge us.
            try:
                await asyncio.wait_for(browser.close(), timeout=20.0)
            except (asyncio.TimeoutError, Exception):
                pass
    return out


async def self_check() -> dict[str, Any]:
    """Run a live, on-demand scrape and report a categorized outcome.

    Unlike the 60s poller this launches a fresh browser every call, so
    it's an admin-triggered action (a button on the operator console),
    not something to put on the 30s health loop.  Returns:

        {ok, category, detail, signal_count}

    where ``category`` is one of ``VALID_SCRAPE_CATEGORIES`` and
    ``ok`` is true only when the scraper logged in and saw the row grid
    (``signal_count`` may legitimately be 0 on a quiet board).
    """
    try:
        signals = await scrape_signals()
    except ScraperNotConfigured as e:
        return {
            "ok": False,
            "category": SCRAPE_NOT_CONFIGURED,
            "detail": str(e),
            "signal_count": 0,
        }
    except ScraperLoginFailed as e:
        return {
            "ok": False,
            "category": SCRAPE_LOGIN_FAILED,
            "detail": str(e),
            "signal_count": 0,
        }
    except ScraperUnavailable as e:
        return {
            "ok": False,
            "category": SCRAPE_BROWSER_MISSING,
            "detail": str(e),
            "signal_count": 0,
        }
    except ScraperDOMChanged as e:
        return {
            "ok": False,
            "category": SCRAPE_NO_ROWS,
            "detail": str(e),
            "signal_count": 0,
        }
    except Exception as e:  # noqa: BLE001 — self-check must always answer
        return {
            "ok": False,
            "category": SCRAPE_ERROR,
            "detail": f"{type(e).__name__}: {e}"[:240],
            "signal_count": 0,
        }
    n = len(signals)
    return {
        "ok": True,
        "category": SCRAPE_OK,
        "detail": (
            f"logged in; saw {n} badged signal(s)"
            if n
            else "logged in; row grid present but 0 badged signals right now"
        ),
        "signal_count": n,
    }


# ---------------------------------------------------------------------
# Live-DOM extraction (real app.edgeterminal.ai contract)
# ---------------------------------------------------------------------

# JS that walks the dashboard in document order, tracks the most recent
# .section-header (so each row maps to its sport vertical), and emits a
# structured record for every game-row carrying a .signal-tag chip.
# Returned shape: list of dicts with header/matchup/market/odds/pick/
# model_prob_text/edge_text/time_text/data_attrs.
_SIGNAL_EXTRACT_JS = r"""
() => {
  const nodes = Array.from(
    document.querySelectorAll('.section-header, div.game-row')
  );
  const txt = (el) => (el && el.innerText ? el.innerText.trim() : null);
  let header = null;
  const out = [];
  for (const n of nodes) {
    if (n.classList.contains('section-header')) {
      const first = (n.innerText || '').split('\n')[0].trim();
      header = first || null;
      continue;
    }
    if (!n.querySelector('.signal-tag')) continue;
    // Both the market kind ("SPREAD") and the model probability
    // ("81%") share class="market" in the live DOM; we disambiguate
    // by index: the first .market child is the market kind, the
    // last is the probability.
    const markets = n.querySelectorAll(':scope > .market');
    const marketKind = markets.length > 0 ? txt(markets[0]) : null;
    const modelProbText =
      markets.length > 1 ? txt(markets[markets.length - 1]) : null;
    // Capture any data-* attributes on the row or any ancestor (up
    // to 4 levels) so we can prefer them if Edge Terminal ever
    // restores the structured contract.
    const dataAttrs = {};
    let a = n;
    for (let i = 0; i < 5 && a; i++, a = a.parentElement) {
      for (const at of a.attributes || []) {
        if (at.name.startsWith('data-') && !(at.name in dataAttrs)) {
          dataAttrs[at.name] = at.value;
        }
      }
    }
    out.push({
      header,
      matchup: txt(n.querySelector('.matchup .matchup-text')),
      market_kind: marketKind,
      odds: txt(n.querySelector(':scope > .odds')),
      pick: txt(n.querySelector(':scope > .pick')),
      model_prob_text: modelProbText,
      edge_text: txt(n.querySelector('.edge-bar .ev')),
      time_text: txt(n.querySelector(':scope > .time')),
      data_attrs: dataAttrs,
    });
  }
  return out;
}
"""


# ``NHL — PICKS RANKED BY EDGE`` → ``nhl``.  Tolerant to either em-dash
# or hyphen and to extra trailing copy ("· 4 GAMES · 2 RECOMMENDED").
_HEADER_VERTICAL_RE = re.compile(r"^\s*([A-Za-z0-9]+)\b")

_MARKET_KIND_TO_PICK_TYPE = {
    "SPREAD": "spread",
    "TOTAL": "total",
    "ML": "moneyline",
    "MONEYLINE": "moneyline",
}

_PCT_RE = re.compile(r"-?\+?[\d.]+")


def _parse_pct_to_fraction(s: str | None) -> float | None:
    """Parse "81%" → 0.81; "+25.1%" → 0.251.  Returns None on junk."""
    if not s:
        return None
    m = _PCT_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0)) / 100.0
    except ValueError:
        return None


def _parse_signed_pct(s: str | None) -> float | None:
    """Parse "+25.1%" → 25.1; "-2.3%" → -2.3.  Returns None on junk."""
    if not s:
        return None
    m = _PCT_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _parse_american_odds(s: str | None) -> float | None:
    if not s:
        return None
    cleaned = s.strip().replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _infer_side(selection: str | None) -> str:
    """Same mapping the data-attr path uses: Under/No/Below → NO."""
    s = (selection or "").strip().lower()
    if (
        s.startswith("under ")
        or s.startswith("no ")
        or s.startswith("below ")
        or s in ("under", "no")
    ):
        return "NO"
    return "YES"


def _vertical_from_header(header: str | None) -> str | None:
    if not header:
        return None
    m = _HEADER_VERTICAL_RE.match(header)
    if not m:
        return None
    return m.group(1).strip().lower() or None


def _row_from_structured(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a JS-extracted signal record into the executor's shape.

    Returns ``None`` if the row is missing any of the dedup keys
    (vertical / matchup / selection) — the poller drops these.
    """
    if not isinstance(raw, dict):
        return None

    data_attrs = raw.get("data_attrs") or {}

    # Data-attr contract wins when present (Edge Terminal may restore
    # it in the future); otherwise parse the positional class layout.
    vertical = (
        (data_attrs.get("data-vertical") or "").strip().lower()
        or _vertical_from_header(raw.get("header"))
    )
    matchup = (data_attrs.get("data-matchup") or "").strip() or (
        raw.get("matchup") or ""
    ).strip()
    selection = (data_attrs.get("data-selection") or "").strip() or (
        raw.get("pick") or ""
    ).strip()
    if not (vertical and matchup and selection):
        return None

    market_kind_raw = (raw.get("market_kind") or "").strip().upper()
    pick_type = (
        (data_attrs.get("data-pick-type") or "").strip().lower()
        or _MARKET_KIND_TO_PICK_TYPE.get(market_kind_raw)
    )

    side_attr = (data_attrs.get("data-side") or "").strip().upper()
    side = side_attr if side_attr in ("YES", "NO") else _infer_side(selection)

    # ``data-model-prob`` is historically a 0-1 fraction in fixtures.
    model_prob: float | None
    if data_attrs.get("data-model-prob"):
        try:
            model_prob = float(data_attrs["data-model-prob"])
        except (TypeError, ValueError):
            model_prob = _parse_pct_to_fraction(raw.get("model_prob_text"))
    else:
        model_prob = _parse_pct_to_fraction(raw.get("model_prob_text"))

    edge_pct = (
        _parse_signed_pct(data_attrs.get("data-edge"))
        if data_attrs.get("data-edge")
        else _parse_signed_pct(raw.get("edge_text"))
    )
    avg_odds = _parse_american_odds(
        data_attrs.get("data-avg-odds")
    ) or _parse_american_odds(raw.get("odds"))
    ev_pct = (
        _parse_signed_pct(data_attrs.get("data-ev"))
        if data_attrs.get("data-ev")
        else edge_pct
    )

    market_price: float | None
    try:
        market_price = (
            float(data_attrs["data-market-price"])
            if data_attrs.get("data-market-price")
            else None
        )
    except (TypeError, ValueError):
        market_price = None

    return {
        "vertical": vertical,
        "matchup": matchup,
        "selection": selection,
        "side": side,
        "signal_date": _today_utc_iso(),
        "edge_pct": edge_pct,
        "model_prob": model_prob,
        "market_price": market_price,
        "kalshi_ticker": (data_attrs.get("data-kalshi-ticker") or "").strip() or None,
        "market_title": (data_attrs.get("data-market-title") or "").strip() or None,
        "pick_type": pick_type,
        "avg_odds": avg_odds,
        "ev_pct": ev_pct,
        "source_screenshot_url": (
            data_attrs.get("data-screenshot-url") or ""
        ).strip()
        or None,
        "kalshi_market_id": (
            data_attrs.get("data-kalshi-market-id") or ""
        ).strip()
        or None,
        "raw": {
            "header": raw.get("header"),
            "market_kind": raw.get("market_kind"),
            "odds": raw.get("odds"),
            "pick": raw.get("pick"),
            "model_prob_text": raw.get("model_prob_text"),
            "edge_text": raw.get("edge_text"),
            "time_text": raw.get("time_text"),
        },
    }


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

    # Pick direction.  Prefer an explicit ``data-side`` attribute
    # (``YES`` / ``NO``) when present; otherwise infer from the
    # selection text — picks that say "Under", "No", "Below" trade
    # the NO side of the Kalshi market; everything else (team names,
    # "Over", "Yes", spreads) maps to YES.  Defaulting to YES on
    # ambiguous picks is the historic behaviour the executor relied on
    # before this fix; we now at least flip the obvious NO cases.
    side = (await _attr("side") or "").upper().strip()
    if side not in ("YES", "NO"):
        s_lower = (selection or "").strip().lower()
        if (
            s_lower.startswith("under ")
            or s_lower.startswith("no ")
            or s_lower.startswith("below ")
            or s_lower == "under"
            or s_lower == "no"
        ):
            side = "NO"
        else:
            side = "YES"

    return {
        "vertical": vertical.lower(),
        "matchup": matchup,
        "selection": selection,
        "side": side,
        "signal_date": _today_utc_iso(),
        "edge_pct": _maybe_float(await _attr("edge")),
        "model_prob": _maybe_float(await _attr("model-prob")),
        "market_price": _maybe_float(await _attr("market-price")),
        "kalshi_ticker": await _attr("kalshi-ticker"),
        "market_title": await _attr("market-title"),
        # Task #845: richer signal fields.  ``pick_type`` is the
        # operator-visible category from the tile (e.g. "moneyline",
        # "spread", "total", "player_prop"); ``avg_odds`` is the
        # consensus odds the source quoted (American or decimal —
        # stored as numeric, the UI formats); ``ev_pct`` is the
        # source's own expected-value % so we can sanity-check our
        # derived edge%; ``source_screenshot_url`` is an optional
        # link the operator can click to see the original tile;
        # ``kalshi_market_id`` is the Kalshi market UUID (distinct
        # from the human-readable ``kalshi_ticker``) when the tile
        # already maps the pick to a Kalshi market.
        "pick_type": await _attr("pick-type"),
        "avg_odds": _maybe_float(await _attr("avg-odds")),
        "ev_pct": _maybe_float(await _attr("ev")),
        "source_screenshot_url": await _attr("screenshot-url"),
        "kalshi_market_id": await _attr("kalshi-market-id"),
        "raw": {},  # filled by poller with the source tile text if needed
    }
