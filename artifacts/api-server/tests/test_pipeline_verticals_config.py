"""Config parsing/validation for ``AXIOM_PIPELINE_VERTICALS``.

Pipeline-exempt verticals (e.g. weather) run the full LLM pipeline and
place trades even while the global ``app.input_mode`` is
``edge_terminal``.  The setting must be a subset of the scheduled
verticals and reject unknown names, mirroring the validation already
applied to ``AXIOM_ORCHESTRATOR_VERTICALS``.
"""
from __future__ import annotations

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_default_is_empty(monkeypatch):
    monkeypatch.delenv("AXIOM_PIPELINE_VERTICALS", raising=False)
    monkeypatch.setenv("AXIOM_ORCHESTRATOR_VERTICALS", "sports,weather")
    get_settings.cache_clear()
    assert get_settings().pipeline_verticals == ()


def test_subset_parsed(monkeypatch):
    monkeypatch.setenv("AXIOM_ORCHESTRATOR_VERTICALS", "sports,weather")
    monkeypatch.setenv("AXIOM_PIPELINE_VERTICALS", "weather")
    get_settings.cache_clear()
    assert get_settings().pipeline_verticals == ("weather",)


def test_whitespace_and_case_normalised(monkeypatch):
    monkeypatch.setenv("AXIOM_ORCHESTRATOR_VERTICALS", "sports,weather")
    monkeypatch.setenv("AXIOM_PIPELINE_VERTICALS", " Weather , ")
    get_settings.cache_clear()
    assert get_settings().pipeline_verticals == ("weather",)


def test_unknown_vertical_rejected(monkeypatch):
    monkeypatch.setenv("AXIOM_ORCHESTRATOR_VERTICALS", "sports,weather")
    monkeypatch.setenv("AXIOM_PIPELINE_VERTICALS", "not_a_vertical")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="unknown vertical"):
        get_settings()


def test_must_be_scheduled(monkeypatch):
    # weather is a valid vertical but NOT in AXIOM_ORCHESTRATOR_VERTICALS,
    # so the scheduler would never spawn a loop for it — reject loudly.
    monkeypatch.setenv("AXIOM_ORCHESTRATOR_VERTICALS", "sports")
    monkeypatch.setenv("AXIOM_PIPELINE_VERTICALS", "weather")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="not in AXIOM_ORCHESTRATOR_VERTICALS"):
        get_settings()


# ---------------------------------------------------------------------------
# _resolve_input_mode — fail-CLOSED control-plane read (live-money safety)
# ---------------------------------------------------------------------------


def test_resolve_input_mode_passes_through(monkeypatch):
    """Normal path returns whatever the control plane reports."""
    import asyncio

    from app.edge_terminal import control as et_control
    from app.orchestrator import cycle as cycle_mod

    async def _ok():
        return "pipeline"

    monkeypatch.setattr(et_control, "get_input_mode", _ok)
    assert asyncio.run(cycle_mod._resolve_input_mode()) == "pipeline"


def test_resolve_input_mode_fails_closed_to_edge_terminal(monkeypatch):
    """A control-plane read error must resolve to ``edge_terminal`` so a
    transient blip never opens the live-money placement path for a
    non-exempt vertical (fail CLOSED, not fail OPEN to ``pipeline``)."""
    import asyncio

    from app.edge_terminal import control as et_control
    from app.orchestrator import cycle as cycle_mod

    async def _boom():
        raise RuntimeError("control-plane unreachable")

    monkeypatch.setattr(et_control, "get_input_mode", _boom)
    assert asyncio.run(cycle_mod._resolve_input_mode()) == "edge_terminal"
