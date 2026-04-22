"""Per-vertical persona profile registry + toggle filtering (Task #194).

These pin the behaviour the runner depends on:
* every built-in profile carries at least 4 personas with a balanced
  OpenAI/Anthropic mix,
* `select_personas` falls back to the full profile when an over-eager
  toggle deny-list would leave us with zero personas,
* `resolve_for_vertical` honours an explicit deny-list,
* the legacy `sports` profile remains byte-identical to `personas.PERSONAS`
  so existing Brier history continues to apply.
"""
from __future__ import annotations

from app.swarm import personas as personas_mod
from app.swarm.profiles import (
    PROFILES,
    get_profile,
    profile_names,
    resolve_for_vertical,
    select_personas,
)


def test_sports_profile_matches_legacy_personas_tuple():
    sports = get_profile("sports")
    assert tuple(p.id for p in sports.personas) == tuple(
        p.id for p in personas_mod.PERSONAS
    )


def test_default_profile_falls_back_to_sports():
    # Unknown name + None both resolve to the default (sports) stack so
    # an upstream typo never empties the swarm.
    assert get_profile(None).persona_ids() == get_profile("sports").persona_ids()
    assert get_profile("not-a-profile").persona_ids() == get_profile(
        "sports"
    ).persona_ids()


def test_every_profile_meets_minimum_shape():
    # Each profile must keep at least 4 personas and a non-trivial
    # OpenAI/Anthropic mix so a single provider outage cannot silence
    # a whole vertical.
    for name in profile_names():
        profile = PROFILES[name]
        assert len(profile.personas) >= 4, f"{name} has fewer than 4 personas"
        providers = {p.provider for p in profile.personas}
        assert providers == {"openai", "anthropic"}, (
            f"{name}: expected balanced OpenAI/Anthropic mix, got {providers}"
        )


def test_select_personas_with_allow_list_filters():
    sports = get_profile("sports")
    allow = {sports.personas[0].id, sports.personas[2].id}
    out = select_personas("sports", enabled_persona_ids=allow)
    assert {p.id for p in out} == allow


def test_select_personas_falls_back_when_allowlist_empty():
    # All-deny ⇒ falls back to the full profile (never silently
    # degrades to a single LLM call).
    out = select_personas("crypto", enabled_persona_ids=set())
    assert tuple(p.id for p in out) == get_profile("crypto").persona_ids()


def test_resolve_for_vertical_applies_denylist():
    crypto = get_profile("crypto")
    deny = [crypto.personas[1].id]
    resolved = resolve_for_vertical("crypto", disabled_persona_ids=deny)
    assert resolved.profile.name == "crypto"
    assert deny[0] not in {p.id for p in resolved.active}
    assert resolved.disabled_ids == tuple(deny)


def test_resolve_for_vertical_falls_back_when_all_denied():
    crypto = get_profile("crypto")
    deny = [p.id for p in crypto.personas]
    resolved = resolve_for_vertical("crypto", disabled_persona_ids=deny)
    # Falls back to the full profile, drops the deny-list reference so
    # downstream consumers don't claim something was silenced when
    # nothing actually was.
    assert tuple(p.id for p in resolved.active) == crypto.persona_ids()
    assert resolved.disabled_ids == ()


def test_profile_names_includes_new_verticals():
    names = set(profile_names())
    for expected in (
        "sports",
        "weather",
        "crypto",
        "politics",
        "commodities",
        "tech-ai",
        "geopolitics",
        "elections",
        "entertainment",
        "events-econ",
        "default",
    ):
        assert expected in names, f"missing profile: {expected}"
