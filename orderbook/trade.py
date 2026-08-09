"""Trade output — the engine's product.

A trade always prints at the *resting* order's price. The aggressor is the
incoming order that removed liquidity; the resting order provided it.
"""

from __future__ import annotations

from orderbook.order import Side


class Trade:
    __slots__ = (
        "trade_id",
        "price",
        "quantity",
        "aggressor_side",
        "aggressor_order_id",
        "resting_order_id",
    )

    def __init__(
        self,
        trade_id: int,
        price: float,
        quantity: int,
        aggressor_side: Side,
        aggressor_order_id: int,
        resting_order_id: int,
    ):
        self.trade_id = trade_id
        self.price = price
        self.quantity = quantity
        self.aggressor_side = aggressor_side
        self.aggressor_order_id = aggressor_order_id
        self.resting_order_id = resting_order_id

    @property
    def buy_order_id(self) -> int:
        if self.aggressor_side is Side.BUY:
            return self.aggressor_order_id
        return self.resting_order_id

    @property
    def sell_order_id(self) -> int:
        if self.aggressor_side is Side.SELL:
            return self.aggressor_order_id
        return self.resting_order_id

    @property
    def notional(self) -> float:
        return self.price * self.quantity

    def as_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "price": self.price,
            "quantity": self.quantity,
            "aggressor_side": self.aggressor_side.value,
            "buy_order_id": self.buy_order_id,
            "sell_order_id": self.sell_order_id,
        }

    def __repr__(self) -> str:
        return (
            f"Trade(#{self.trade_id} {self.quantity} @ {self.price}, "
            f"{self.aggressor_side.value} aggressor)"
        )
