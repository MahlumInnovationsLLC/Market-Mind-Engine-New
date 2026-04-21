"""Polymarket exchange integration: REST client, WebSocket subscriber,
order lifecycle (paper-mode interceptor), shared kill switch.

Mirrors `app/kalshi/` so the trading orchestrator can route between
Kalshi and Polymarket through identical primitives.
"""
from .client import PolymarketClient, PolymarketAPIError
from .lifecycle import (
    OrderRequest,
    OrderRecord,
    OrderRejected,
    submit_order,
    cancel_order,
    list_resting_orders,
)
from ..exchange_kill_switch import (
    halt,
    resume,
    is_halted,
    kill_switch_state,
)
from .bus import bus
from .ws import PolymarketWebSocket, get_ws_subscriber

__all__ = [
    "PolymarketClient",
    "PolymarketAPIError",
    "OrderRequest",
    "OrderRecord",
    "OrderRejected",
    "submit_order",
    "cancel_order",
    "halt",
    "resume",
    "is_halted",
    "kill_switch_state",
    "list_resting_orders",
    "bus",
    "PolymarketWebSocket",
    "get_ws_subscriber",
]
