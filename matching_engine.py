"""Match incoming orders against the best opposing price level only (no flat book scan)."""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Union

if TYPE_CHECKING:
    from order import Order
    from order_book import OrderBook

_NO_MATCH: List[Union[bool, int, float, None]] = [False, -1, None, 0, 0, 0]


class MatchingEngine:
    def process_order(self, order: "Order", book: "OrderBook"):
        """
        Try one match at the current best opposing level (FIFO front).

        Returns:
            [matched, resting_order_id, price, executed_qty,
             incoming_remaining, resting_remaining]
        """
        if order.side == "buy":
            return self._match_buy(order, book)
        return self._match_sell(order, book)

    def _match_buy(self, order: "Order", book: "OrderBook"):
        best_ask = book.best_ask_price()
        if best_ask is None:
            return _NO_MATCH

        if order.order_type == "limit" and order.price < best_ask:
            return _NO_MATCH

        resting = book.best_ask_order()
        if resting is None:
            return _NO_MATCH

        return self._execute_match(order, resting)

    def _match_sell(self, order: "Order", book: "OrderBook"):
        best_bid = book.best_bid_price()
        if best_bid is None:
            return _NO_MATCH

        if order.order_type == "limit" and order.price > best_bid:
            return _NO_MATCH

        resting = book.best_bid_order()
        if resting is None:
            return _NO_MATCH

        return self._execute_match(order, resting)

    def _execute_match(self, order: "Order", resting: "Order"):
        incoming_qty = int(order.quantity)
        resting_qty = int(resting.quantity)
        executed_quantity = min(incoming_qty, resting_qty)
        incoming_remaining = incoming_qty - executed_quantity
        resting_remaining = resting_qty - executed_quantity
        return [
            True,
            resting.order_id,
            resting.price,
            executed_quantity,
            incoming_remaining,
            resting_remaining,
        ]
