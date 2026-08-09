"""Shared fixtures and builders for the core engine tests."""

from __future__ import annotations

import itertools
from typing import Optional

import pytest

from orderbook import MatchingEngine, Order, OrderBook, OrderType, Side


class Market:
    """Book + engine + an order-id sequence, so tests read like order flow."""

    def __init__(self):
        self.book = OrderBook()
        self.engine = MatchingEngine()
        self._ids = itertools.count(1)

    def order(
        self,
        side: Side,
        quantity: int,
        price: Optional[float] = None,
        order_type: OrderType = OrderType.LIMIT,
    ) -> Order:
        return Order(next(self._ids), side, order_type, quantity, price)

    def limit(self, side: Side, quantity: int, price: float):
        return self.engine.submit(self.order(side, quantity, price), self.book)

    def buy(self, quantity: int, price: float):
        return self.limit(Side.BUY, quantity, price)

    def sell(self, quantity: int, price: float):
        return self.limit(Side.SELL, quantity, price)

    def market(self, side: Side, quantity: int):
        order = self.order(side, quantity, None, OrderType.MARKET)
        return self.engine.submit(order, self.book)

    def cancel(self, order_id: int):
        return self.engine.cancel(order_id, self.book)

    def validate(self) -> None:
        self.book.validate()


@pytest.fixture
def market() -> Market:
    return Market()


@pytest.fixture
def book() -> OrderBook:
    return OrderBook()
