"""Matching engine: turns an incoming order into trades.

Price-time priority
-------------------
An aggressor sweeps the opposing side best price first; within a price, orders
fill in arrival order (FIFO). Every trade prints at the *resting* order's
price, so an aggressive limit receives price improvement rather than paying
its own limit.

Order handling
--------------
- ``LIMIT``  — sweeps every crossing level, then rests any remainder.
- ``MARKET`` — sweeps until filled or the book is dry; the remainder expires
  rather than resting (a market order has no price to rest at).
- ``CANCEL`` — :meth:`cancel` removes a resting order in O(1).
"""

from __future__ import annotations

from typing import List, Optional

from orderbook.order import Order, OrderStatus, OrderType
from orderbook.order_book import OrderBook
from orderbook.trade import Trade


class MatchResult:
    """Outcome of one submission: the order, the trades it caused, where it landed."""

    __slots__ = ("order", "trades", "resting")

    def __init__(self, order: Order, trades: List[Trade], resting: bool):
        self.order = order
        self.trades = trades
        self.resting = resting

    @property
    def status(self) -> OrderStatus:
        return self.order.status

    @property
    def filled_quantity(self) -> int:
        return self.order.filled

    @property
    def traded_notional(self) -> float:
        return sum(t.notional for t in self.trades)

    @property
    def average_price(self) -> Optional[float]:
        if not self.trades:
            return None
        return self.traded_notional / self.filled_quantity

    def __repr__(self) -> str:
        return (
            f"MatchResult(order={self.order.order_id}, "
            f"{len(self.trades)} trades, filled={self.filled_quantity}, "
            f"status={self.status.value}, resting={self.resting})"
        )


class MatchingEngine:
    """Stateless apart from the trade-id sequence."""

    def __init__(self, first_trade_id: int = 1):
        self._next_trade_id = first_trade_id

    @property
    def trades_generated(self) -> int:
        return self._next_trade_id - 1

    def submit(self, order: Order, book: OrderBook) -> MatchResult:
        """Match ``order`` against ``book``, then rest or expire the remainder."""
        trades = self._sweep(order, book)

        if order.remaining == 0:
            order.status = OrderStatus.FILLED
            return MatchResult(order, trades, resting=False)

        if order.order_type is OrderType.MARKET:
            # Nothing left to trade against and no price to rest at.
            order.status = OrderStatus.EXPIRED
            return MatchResult(order, trades, resting=False)

        order.status = (
            OrderStatus.PARTIALLY_FILLED if trades else OrderStatus.RESTING
        )
        book.add(order)
        return MatchResult(order, trades, resting=True)

    def cancel(self, order_id: int, book: OrderBook) -> Optional[Order]:
        """Pull a resting order. Returns None if it was already gone."""
        return book.cancel(order_id)

    # ------------------------------------------------------------------

    def _sweep(self, order: Order, book: OrderBook) -> List[Trade]:
        trades: List[Trade] = []
        opposing = order.side.opposite

        while order.remaining > 0:
            level = book.best_level(opposing)
            if level is None:
                break
            if not order.crosses(level.price):
                break

            # The head node is consumed directly — no id lookup, no
            # re-validation: consume_level owns fill + unlink + collection.
            node = level.head
            resting = node.order
            quantity = min(order.remaining, resting.remaining)

            trades.append(
                Trade(
                    trade_id=self._next_trade_id,
                    price=resting.price,
                    quantity=quantity,
                    aggressor_side=order.side,
                    aggressor_order_id=order.order_id,
                    resting_order_id=resting.order_id,
                )
            )
            self._next_trade_id += 1

            order.remaining -= quantity
            book.consume_level(level, node, quantity)

        return trades
