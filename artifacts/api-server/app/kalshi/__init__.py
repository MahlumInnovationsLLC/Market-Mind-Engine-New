"""Kalshi exchange integration: REST client, WebSocket subscriber,
order lifecycle state machine, paper-mode interceptor, kill switch.
"""
from .client import KalshiClient, KalshiAPIError
from .lifecycle import (
    OrderRequest,
    OrderRecord,
    submit_order,
    cancel_order,
    halt,
    resume,
    is_halted,
    list_resting_orders,
)
from .bus import bus
from .market_titles import get_title as get_market_title, resolve_titles
from .ws import KalshiWebSocket, get_ws_subscriber

__all__ = [
    "KalshiClient",
    "KalshiAPIError",
    "OrderRequest",
    "OrderRecord",
    "submit_order",
    "cancel_order",
    "halt",
    "resume",
    "is_halted",
    "list_resting_orders",
    "bus",
    "KalshiWebSocket",
    "get_ws_subscriber",
    "resolve_titles",
    "get_market_title",
]
