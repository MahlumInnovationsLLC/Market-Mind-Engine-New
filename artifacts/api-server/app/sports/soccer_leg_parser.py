"""Shared Kalshi soccer 3-way leg-orientation parser.

Single source of truth for mapping a Kalshi soccer market title to its
``"HOME"`` / ``"DRAW"`` / ``"AWAY"`` leg label.  Used by both the
producer (:mod:`app.feeds.kalshi_sports_markets`) that stashes the leg
on ``metadata.leg_orientation`` and the live in-game predictor
(:mod:`app.learning.soccer_live`) that falls back to title parsing
for rows upserted before the producer started persisting the label.

Lives under ``app.sports`` (no DB / asyncpg / feed imports) so both
call sites can import it without re-introducing a cycle.
"""
from __future__ import annotations

import re

LEG_HOME = "HOME"
LEG_DRAW = "DRAW"
LEG_AWAY = "AWAY"
VALID_LEGS = frozenset({LEG_HOME, LEG_DRAW, LEG_AWAY})

_DRAW_KEYWORDS: tuple[str, ...] = (
    "draw", " tie", "tied", "ends level", "level after",
    "stalemate", "share the points",
)
# "Will Manchester City beat Arsenal?", "Manchester City beats Arsenal",
# "Manchester City defeats Arsenal", "Will Manchester City win?"
_WIN_SUBJECT_RE = re.compile(
    r"^\s*(?:will\s+)?(?:the\s+)?(?P<subject>.+?)\s+"
    r"(?:beat|beats|defeat|defeats|win|wins)\b",
    re.IGNORECASE,
)


def _normalise_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower()) if s else ""


def parse_leg_orientation(
    title: str | None,
    home_team: str | None,
    away_team: str | None,
) -> str | None:
    """Return ``"HOME"`` / ``"DRAW"`` / ``"AWAY"`` for a Kalshi soccer
    game-market title when we can identify which 3-way leg the YES
    side is pricing; ``None`` when the title isn't a recognised
    moneyline / draw template.

    The match against home/away is normalised (lowercased, alphanum
    only) so "Man. City" matches "Manchester City" loosely.
    """
    if not title:
        return None
    low = title.lower()
    for kw in _DRAW_KEYWORDS:
        if kw in low:
            return LEG_DRAW
    m = _WIN_SUBJECT_RE.match(title)
    if not m:
        return None
    subject = _normalise_name(m.group("subject"))
    if not subject:
        return None
    home_n = _normalise_name(home_team or "")
    away_n = _normalise_name(away_team or "")
    # Substring match either direction so "City" matches "Manchester City"
    # and vice versa.  Home checked first because Kalshi titles are
    # marginally more likely to phrase a home-side YES.
    if home_n and (subject in home_n or home_n in subject):
        return LEG_HOME
    if away_n and (subject in away_n or away_n in subject):
        return LEG_AWAY
    return None
