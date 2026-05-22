"""Task #847: scraper fixture parsing tests.

The live ``scrape_signals`` coroutine launches Playwright + chromium
which we can't run in the unit-test sandbox.  Instead we exercise the
DOM contract directly: each fixture HTML file is parsed with the
stdlib ``html.parser`` to extract signal-badged tiles, then those
tiles are fed through :func:`scraper._extract_tile` via a tiny
``FakeHandle`` shim that mimics the Playwright ``Locator`` surface
(``get_attribute`` / ``inner_text``).

The fixtures live under ``tests/fixtures/edge_terminal/`` and
intentionally use the same ``data-*`` attribute names the live
scraper depends on so a DOM contract change in the source site
breaks the fixture and the tests immediately.
"""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


FIXTURES = Path(__file__).parent / "fixtures" / "edge_terminal"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------
# Fixture parser
# ---------------------------------------------------------------------

_SIGNAL_CLASSES = {"signal-badge", "has-signal"}


class _SignalCollector(HTMLParser):
    """Walk an Edge Terminal HTML fixture and collect signal tiles.

    A "signal tile" is any element whose attributes match the
    superset the scraper uses (``data-axiom-signal='true'``,
    ``data-signal='true'``, or a class list containing
    ``signal-badge`` / ``has-signal``).
    """

    def __init__(self) -> None:
        super().__init__()
        self.tiles: list[dict[str, Any]] = []
        self._depth = 0
        self._cur: dict[str, Any] | None = None
        self._text: list[str] = []

    @staticmethod
    def _is_signal(attrs: dict[str, str | None]) -> bool:
        if attrs.get("data-axiom-signal") == "true":
            return True
        if attrs.get("data-signal") == "true":
            return True
        classes = (attrs.get("class") or "").split()
        return any(c in _SIGNAL_CLASSES for c in classes)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        adict = {k: v for k, v in attrs}
        if self._cur is None and self._is_signal(adict):
            self._cur = adict
            self._depth = 1
            self._text = []
        elif self._cur is not None:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._cur is None:
            return
        self._depth -= 1
        if self._depth == 0:
            self.tiles.append({
                "attrs": self._cur,
                "text": "".join(self._text).strip(),
            })
            self._cur = None
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._cur is not None:
            self._text.append(data)


def _parse_fixture(name: str) -> list[dict[str, Any]]:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    collector = _SignalCollector()
    collector.feed(html)
    return collector.tiles


class FakeHandle:
    """Minimal Playwright Locator stand-in for ``_extract_tile``."""

    def __init__(self, attrs: dict[str, str | None], text: str) -> None:
        self._attrs = attrs
        self._text = text

    async def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    async def inner_text(self) -> str:
        return self._text


def _extract_all(tiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.edge_terminal import scraper
    out: list[dict[str, Any]] = []
    for t in tiles:
        row = _run(scraper._extract_tile(FakeHandle(t["attrs"], t["text"])))
        if row is not None:
            out.append(row)
    return out


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

def test_nba_fixture_yields_two_signals_with_correct_sides():
    tiles = _parse_fixture("nba_signals.html")
    # 3 elements in the file but only 2 carry the signal-badge class.
    assert len(tiles) == 2

    rows = _extract_all(tiles)
    assert len(rows) == 2

    # First row: spread pick → YES side.
    spread = rows[0]
    assert spread["vertical"] == "nba"
    assert spread["matchup"] == "Oklahoma City Thunder @ San Antonio Spurs"
    assert spread["selection"] == "Thunder +2"
    assert spread["side"] == "YES"
    assert spread["pick_type"] == "spread"
    assert spread["avg_odds"] == -103.0
    assert spread["ev_pct"] == 27.9
    assert spread["kalshi_ticker"] == "KXNBA-2026-OKC-SAS-SPREAD-THU2"

    # Second row: explicit NO side on the Under total.
    under = rows[1]
    assert under["selection"] == "Under 218"
    assert under["side"] == "NO"
    assert under["pick_type"] == "total"
    assert under["avg_odds"] == -107.0
    assert under["ev_pct"] == 18.5


def test_nhl_fixture_yields_one_signal_with_screenshot_url():
    tiles = _parse_fixture("nhl_signals.html")
    assert len(tiles) == 1

    rows = _extract_all(tiles)
    assert len(rows) == 1

    knights = rows[0]
    assert knights["vertical"] == "nhl"
    assert knights["matchup"] == "Vegas Golden Knights @ Colorado Avalanche"
    assert knights["selection"] == "Knights +1.5"
    assert knights["side"] == "YES"
    assert knights["pick_type"] == "spread"
    assert knights["avg_odds"] == -157.0
    assert knights["ev_pct"] == 31.0
    assert (
        knights["source_screenshot_url"]
        == "https://example.com/tile-knights.png"
    )


def test_empty_fixture_yields_no_signals():
    tiles = _parse_fixture("empty_dashboard.html")
    assert tiles == []
    rows = _extract_all(tiles)
    assert rows == []
