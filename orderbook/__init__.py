"""A limit order book and matching engine.

    Order -> OrderBook -> MatchingEngine -> Trade

Nothing here knows about strategies, participants, PnL, or transport. Those
live outside the core, in ``sim/``.
"""

from orderbook.matching_engine import MatchingEngine, MatchResult
from orderbook.order import Order, OrderStatus, OrderType, Side
from orderbook.order_book import (
    BookIntegrityError,
    BookSnapshot,
    LevelView,
    OrderBook,
)
from orderbook.price_level import BookNode, PriceLevel
from orderbook.trade import Trade

__all__ = [
    "BookIntegrityError",
    "BookNode",
    "BookSnapshot",
    "LevelView",
    "MatchResult",
    "MatchingEngine",
    "Order",
    "OrderBook",
    "OrderStatus",
    "OrderType",
    "PriceLevel",
    "Side",
    "Trade",
]
