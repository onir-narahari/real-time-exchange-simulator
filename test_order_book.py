"""Tests for price-level order book (FIFO, O(1) cancel, depth cache)."""

import unittest

from exchange import Exchange
from order import Order
from order_book import BookInstrumentation, OrderBook


class OrderBookTests(unittest.TestCase):
    def setUp(self):
        BookInstrumentation.reset()
        self.book = OrderBook()

    def _buy(self, oid: int, price: float, qty: int) -> Order:
        return Order(oid, "buy", price, qty, "limit")

    def _sell(self, oid: int, price: float, qty: int) -> Order:
        return Order(oid, "sell", price, qty, "limit")

    def test_fifo_same_price_bids(self):
        self.book.add_order(self._buy(1, 100.0, 5))
        self.book.add_order(self._buy(2, 100.0, 7))
        self.book.add_order(self._buy(3, 100.0, 3))
        flat = self.book.getBuyOrders()
        self.assertEqual([o.order_id for o in flat[:3]], [1, 2, 3])

    def test_fifo_same_price_asks(self):
        self.book.add_order(self._sell(1, 101.0, 4))
        self.book.add_order(self._sell(2, 101.0, 6))
        flat = self.book.getSellOrders()
        self.assertEqual([o.order_id for o in flat[:2]], [1, 2])

    def test_cancel_middle_node(self):
        self.book.add_order(self._buy(1, 99.0, 1))
        self.book.add_order(self._buy(2, 99.0, 2))
        self.book.add_order(self._buy(3, 99.0, 3))
        removed = self.book.cancel_order(2)
        self.assertEqual(removed.order_id, 2)
        self.assertFalse(self.book.is_on_book(2))
        ids = [o.order_id for o in self.book.getBuyOrders()]
        self.assertEqual(ids, [1, 3])

    def test_cancel_head_and_tail(self):
        self.book.add_order(self._sell(10, 50.0, 1))
        self.book.add_order(self._sell(11, 50.0, 2))
        self.book.add_order(self._sell(12, 50.0, 3))
        self.book.cancel_order(10)
        self.assertEqual([o.order_id for o in self.book.getSellOrders()], [11, 12])
        self.book.cancel_order(12)
        self.assertEqual([o.order_id for o in self.book.getSellOrders()], [11])

    def test_partial_fill_keeps_front(self):
        ex = Exchange()
        ex.order_book._next_order_id = 1
        sell = ex.manual_order("sell", 10, 100.0, "limit")
        ex.submit_order(sell)
        buy = ex.manual_order("buy", 4, 100.0, "limit")
        ex.submit_order(buy)
        front = ex.order_book.best_ask_order()
        self.assertEqual(front.quantity, 6)
        self.assertEqual(front.order_id, sell.order_id)

    def test_full_fill_removes_level(self):
        ex = Exchange()
        ex.order_book._next_order_id = 1
        sell = ex.manual_order("sell", 5, 100.0, "limit")
        ex.submit_order(sell)
        buy = ex.manual_order("buy", 5, 100.0, "limit")
        ex.submit_order(buy)
        self.assertIsNone(ex.order_book.best_ask_price())
        self.assertEqual(ex.order_book.get_ask_depth(), 0)

    def test_best_prices_when_level_empty(self):
        self.book.add_order(self._buy(1, 100.0, 5))
        self.book.add_order(self._buy(2, 99.0, 5))
        self.book.cancel_order(1)
        self.assertEqual(self.book.best_bid_price(), 99.0)
        self.book.add_order(self._sell(3, 101.0, 2))
        self.book.add_order(self._sell(4, 102.0, 2))
        self.book.cancel_order(3)
        self.assertEqual(self.book.best_ask_price(), 102.0)

    def test_depth_cache(self):
        self.book.add_order(self._buy(1, 100.0, 10))
        self.book.add_order(self._buy(2, 99.0, 20))
        self.book.add_order(self._sell(3, 101.0, 7))
        self.assertEqual(self.book.get_bid_depth(), 30)
        self.assertEqual(self.book.get_ask_depth(), 7)
        self.book.cancel_order(2)
        self.assertEqual(self.book.get_bid_depth(), 10)

    def test_instrumentation_zero_on_hot_path(self):
        BookInstrumentation.reset()
        for i in range(50):
            self.book.add_order(self._buy(i + 1, 100.0 - i * 0.01, 1))
        for i in range(25):
            self.book.cancel_order(i + 1)
        self.assertEqual(BookInstrumentation.cancel_full_scans, 0)
        self.assertEqual(BookInstrumentation.index_rebuilds, 0)
        self.assertEqual(BookInstrumentation.list_removals, 0)


if __name__ == "__main__":
    unittest.main()
