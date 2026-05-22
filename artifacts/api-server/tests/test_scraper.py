"""Edge Terminal scraper fixture tests (task #847).

We can't drive a real Playwright browser inside the unit-test loop,
so instead we exercise the scraper's parsing layer — ``_extract_tile``
— against captured HTML fixtures under
``tests/fixtures/edge_terminal/``.

For each fixture we:
  1. Parse the HTML with stdlib ``html.parser``.
  2. Find every element matching the same selector contract the
     live scraper uses (``data-axiom-signal='true'`` OR class
     ``signal-badge`` / ``has-signal`` OR ``data-signal='true'``).
  3. Wrap each tile in a fake Playwright handle exposing the two
     async accessors ``get_attribute`` / ``inner_text`` that
     ``_extract_tile`` calls.
  4. Run the same per-tile pipeline ``scrape_signals`` would and
     assert the resulting signal payload.

This covers the NBA, NHL, and empty-page contracts called out in
task #844's done criteria and pinned by task #847.
"""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "edge_terminal"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------
# Minimal HTML → element tree, only as much as the scraper needs.
# ---------------------------------------------------------------------

class _Element:
    __slots__ = ("tag", "attrs", "children", "text_parts")

    def __init__(self, tag: str, attrs: dict[str, str]):
        self.tag = tag
        self.attrs = attrs
        self.children: list["_Element"] = []
        self.text_parts: list[str] = []

    def inner_text(self) -> str:
        out: list[str] = []
        for part in self.text_parts:
            out.append(part)
        for c in self.children:
            out.append(c.inner_text())
        return " ".join(p.strip() for p in out if p and p.strip())

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class _DOMBuilder(HTMLParser):
    # html.parser doesn't auto-close void elements, but our fixtures
    # only use block tags so a simple stack is sufficient.
    _VOID = {"br", "img", "meta", "link", "input", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Element("__root__", {})
        self.stack: list[_Element] = [self.root]

    def handle_starttag(self, tag, attrs):
        el = _Element(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(el)
        if tag not in self._VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        el = _Element(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(el)

    def handle_endtag(self, tag):
        # Pop until we find a matching tag (tolerant of unbalanced
        # HTML, which real-world dashboards routinely emit).
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data and self.stack:
            self.stack[-1].text_parts.append(data)


def _parse(html: str) -> _Element:
    b = _DOMBuilder()
    b.feed(html)
    return b.root


def _is_signal_tile(el: _Element) -> bool:
    """Mirror the scraper's tile_selector:
        [data-axiom-signal='true'], .signal-badge, .has-signal,
        [data-signal='true']
    """
    if el.attrs.get("data-axiom-signal") == "true":
        return True
    if el.attrs.get("data-signal") == "true":
        return True
    classes = (el.attrs.get("class") or "").split()
    return "signal-badge" in classes or "has-signal" in classes


def _collect_tiles(root: _Element) -> list[_Element]:
    # Dedupe: a tile may match both ``data-axiom-signal`` AND a
    # class — the live Playwright selector returns one handle per
    # element regardless.
    seen: set[int] = set()
    out: list[_Element] = []
    for el in root.walk():
        if _is_signal_tile(el) and id(el) not in seen:
            seen.add(id(el))
            out.append(el)
    return out


class _FakeHandle:
    """Implements just enough of Playwright's ElementHandle API
    (``get_attribute`` / ``inner_text``) for ``_extract_tile``."""

    def __init__(self, el: _Element):
        self._el = el

    async def get_attribute(self, name: str) -> str | None:
        return self._el.attrs.get(name)

    async def inner_text(self) -> str:
        return self._el.inner_text()


async def _parse_fixture(name: str) -> list[dict[str, Any]]:
    from app.edge_terminal.scraper import _extract_tile

    html = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    tiles = _collect_tiles(_parse(html))
    out: list[dict[str, Any]] = []
    for t in tiles:
        row = await _extract_tile(_FakeHandle(t))
        if row is not None:
            out.append(row)
    return out


# ---------------------------------------------------------------------
# NBA fixture
# ---------------------------------------------------------------------

def test_scraper_nba_fixture_yields_two_signals_with_correct_sides():
    rows = _run(_parse_fixture("nba.html"))
    assert len(rows) == 2, f"expected 2 NBA signals, got {len(rows)}: {rows}"

    by_sel = {r["selection"]: r for r in rows}
    assert set(by_sel) == {"Lakers", "Under 220.5"}

    lakers = by_sel["Lakers"]
    assert lakers["vertical"] == "nba"  # lowercased by _extract_tile
    assert lakers["matchup"] == "Lakers @ Celtics"
    assert lakers["side"] == "YES"
    assert lakers["kalshi_ticker"] == "KX-NBA-LAL-BOS"
    assert lakers["market_title"] == "Lakers vs Celtics — Lakers win"
    assert lakers["edge_pct"] == 7.5
    assert lakers["model_prob"] == 0.58
    assert lakers["market_price"] == 0.50

    under = by_sel["Under 220.5"]
    assert under["vertical"] == "nba"
    assert under["matchup"] == "Heat @ Nuggets"
    # data-side="NO" must survive parsing — this is the wrong-side-
    # trade bug the audit caught.
    assert under["side"] == "NO"


# ---------------------------------------------------------------------
# NHL fixture
# ---------------------------------------------------------------------

def test_scraper_nhl_fixture_handles_text_fallback_and_explicit_no_side():
    rows = _run(_parse_fixture("nhl.html"))
    # Two valid signals; the malformed third tile is silently dropped.
    assert len(rows) == 2, f"expected 2 NHL signals, got {len(rows)}: {rows}"

    by_sel = {r["selection"]: r for r in rows}
    assert set(by_sel) == {"Rangers", "No Goalie Pulled"}

    # Text-fallback parse: "NHL | Rangers @ Bruins | Rangers".
    rangers = by_sel["Rangers"]
    assert rangers["vertical"] == "nhl"
    assert rangers["matchup"] == "Rangers @ Bruins"
    # No explicit data-side; selection doesn't start with under/no/below
    # so default-YES kicks in.
    assert rangers["side"] == "YES"

    # Explicit data-side="NO" attribute-path tile.
    no_pull = by_sel["No Goalie Pulled"]
    assert no_pull["vertical"] == "nhl"
    assert no_pull["matchup"] == "Maple Leafs @ Canadiens"
    assert no_pull["side"] == "NO"
    assert no_pull["edge_pct"] == 3.1


# ---------------------------------------------------------------------
# Empty-page fixture
# ---------------------------------------------------------------------

def test_scraper_empty_fixture_returns_no_signals():
    rows = _run(_parse_fixture("empty.html"))
    assert rows == [], f"expected zero signals on quiet day, got {rows}"
