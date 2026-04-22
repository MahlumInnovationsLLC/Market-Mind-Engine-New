"""6-persona LLM swarm: parallel persona analysis, moderator synthesis,
gatekeeper veto, persistence to swarm_debates + llm_predictions, and live
streaming over WS /ws/llm-logs.

Uses direct OpenAI + Anthropic SDK calls (NOT Replit AI Integrations) per
the task spec.
"""
from .runner import run_debate
from .streams import llm_log_hub
from .weights import (
    get_persona_weight,
    refresh_persona_weights,
    snapshot_persona_weights,
)

__all__ = [
    "run_debate",
    "llm_log_hub",
    "refresh_persona_weights",
    "get_persona_weight",
    "snapshot_persona_weights",
]
