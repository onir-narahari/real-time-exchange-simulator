"""Core order types.

An ``Order`` is the single input to the engine. It carries no strategy, no
account, and no PnL — only what the book and the matching engine need to
establish price-time priority.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class Side(str, Enum):
    """Order side. Inherits ``str`` so ``Side.BUY == "buy"`` and JSON round-trips."""

    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    NEW = "new"
    RESTING = "resting"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    #: Market order remainder that found no more liquidity.
    EXPIRED = "expired"


class Order:
    """A single order.

    ``quantity`` is the original size and never changes; ``remaining`` is what
    is still live. ``sequence`` is assigned by the book when the order first
    rests and breaks ties at a price level (time priority).
    """

    __slots__ = (
        "order_id",
        "side",
        "order_type",
        "price",
        "quantity",
        "remaining",
        "sequence",
        "status",
    )

    def __init__(
        self,
        order_id: int,
        side: Side,
        order_type: OrderType,
        quantity: int,
        price: Optional[float] = None,
    ):
        side = Side(side)
        order_type = OrderType(order_type)

        if not isinstance(quantity, int) or isinstance(quantity, bool):
            raise ValueError(f"quantity must be an int, got {quantity!r}")
        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity}")

        if order_type is OrderType.LIMIT:
            if price is None:
                raise ValueError("limit orders require a price")
            if price <= 0:
                raise ValueError(f"price must be positive, got {price}")
        else:
            # A market order takes whatever the book offers; a price is meaningless.
            price = None

        self.order_id = order_id
        self.side = side
        self.order_type = order_type
        self.price = price
        self.quantity = quantity
        self.remaining = quantity
        self.sequence: Optional[int] = None
        self.status = OrderStatus.NEW

    # --- Derived state ---

    @property
    def filled(self) -> int:
        return self.quantity - self.remaining

    @property
    def is_filled(self) -> bool:
        return self.remaining == 0

    @property
    def is_limit(self) -> bool:
        return self.order_type is OrderType.LIMIT

    def crosses(self, resting_price: float) -> bool:
        """True if this order is willing to trade at ``resting_price``."""
        if self.order_type is OrderType.MARKET:
            return True
        if self.side is Side.BUY:
            return self.price >= resting_price
        return self.price <= resting_price

    def __repr__(self) -> str:
        price = "MKT" if self.price is None else self.price
        return (
            f"Order(id={self.order_id}, {self.side.value} {self.remaining}"
            f"/{self.quantity} @ {price}, {self.status.value})"
        )
