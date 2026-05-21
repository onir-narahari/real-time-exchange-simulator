"""
Price-level order book with FIFO queues per level and O(1) cancel via node unlink.

Bids/asks are not stored as sorted flat lists. Matching compatibility is preserved
by building a flattened best-first view only when getBuyOrders/getSellOrders run.
"""

from __future__ import annotations

import bisect
from typing import Dict, List, Optional

from order import Order


class MarketHealthError(ValueError):
    """Raised when resting book state violates market integrity rules."""


class BookInstrumentation:
    """Debug counters; should stay zero after price-level refactor."""

    cancel_full_scans: int = 0
    index_rebuilds: int = 0
    list_removals: int = 0
    price_level_creates: int = 0
    price_level_removes: int = 0

    @classmethod
    def reset(cls) -> None:
        cls.cancel_full_scans = 0
        cls.index_rebuilds = 0
        cls.list_removals = 0
        cls.price_level_creates = 0
        cls.price_level_removes = 0


class BookNode:
    __slots__ = ("order", "prev", "next", "level")

    def __init__(self, order: Order, level: "PriceLevel"):
        self.order = order
        self.prev: Optional[BookNode] = None
        self.next: Optional[BookNode] = None
        self.level = level


class PriceLevel:
    """FIFO queue of orders at a single price."""

    __slots__ = ("price", "side", "head", "tail", "total_quantity")

    def __init__(self, price: float, side: str):
        self.price = price
        self.side = side
        self.head: Optional[BookNode] = None
        self.tail: Optional[BookNode] = None
        self.total_quantity = 0

    def is_empty(self) -> bool:
        return self.head is None

    def peek_front(self) -> Optional[Order]:
        return self.head.order if self.head else None

    def append(self, order: Order) -> BookNode:
        node = BookNode(order, self)
        if self.tail is None:
            self.head = self.tail = node
        else:
            node.prev = self.tail
            self.tail.next = node
            self.tail = node
        self.total_quantity += order.quantity
        return node

    def pop_front(self) -> Optional[BookNode]:
        if self.head is None:
            return None
        return self.remove_node(self.head)

    def remove_node(self, node: BookNode) -> BookNode:
        self.total_quantity -= node.order.quantity
        if node.prev is not None:
            node.prev.next = node.next
        else:
            self.head = node.next
        if node.next is not None:
            node.next.prev = node.prev
        else:
            self.tail = node.prev
        node.prev = None
        node.next = None
        return node


class OrderBook:
    def __init__(self):
        self._next_order_id = 1

        self.bid_levels: Dict[float, PriceLevel] = {}
        self.ask_levels: Dict[float, PriceLevel] = {}
        self.bid_prices: List[float] = []  # ascending; best bid = last
        self.ask_prices: List[float] = []  # ascending; best ask = first

        self.orders_by_id: Dict[int, BookNode] = {}

        self.bid_depth = 0
        self.ask_depth = 0

    # --- Best price / depth (fast path) ---

    def best_bid_price(self) -> Optional[float]:
        if not self.bid_prices:
            return None
        return self.bid_prices[-1]

    def best_ask_price(self) -> Optional[float]:
        if not self.ask_prices:
            return None
        return self.ask_prices[0]

    def best_bid_order(self) -> Optional[Order]:
        price = self.best_bid_price()
        if price is None:
            return None
        return self.bid_levels[price].peek_front()

    def best_ask_order(self) -> Optional[Order]:
        price = self.best_ask_price()
        if price is None:
            return None
        return self.ask_levels[price].peek_front()

    def get_best_bid(self) -> Optional[float]:
        return self.best_bid_price()

    def get_best_ask(self) -> Optional[float]:
        return self.best_ask_price()

    def get_bid_depth(self) -> int:
        return self.bid_depth

    def get_ask_depth(self) -> int:
        return self.ask_depth

    # --- Matching-engine compatibility views ---

    def getBuyOrders(self) -> List[Order]:
        """Highest bid first; FIFO within each price level."""
        return self._flatten_bids()

    def getSellOrders(self) -> List[Order]:
        """Lowest ask first; FIFO within each price level."""
        return self._flatten_asks()

    def _flatten_bids(self) -> List[Order]:
        out: List[Order] = []
        for price in reversed(self.bid_prices):
            node = self.bid_levels[price].head
            while node is not None:
                out.append(node.order)
                node = node.next
        return out

    def _flatten_asks(self) -> List[Order]:
        out: List[Order] = []
        for price in self.ask_prices:
            node = self.ask_levels[price].head
            while node is not None:
                out.append(node.order)
                node = node.next
        return out

    def _level_for(self, side: str, price: float) -> PriceLevel:
        if side == "buy":
            level = self.bid_levels.get(price)
            if level is None:
                level = PriceLevel(price, "buy")
                self.bid_levels[price] = level
                bisect.insort(self.bid_prices, price)
                BookInstrumentation.price_level_creates += 1
            return level
        level = self.ask_levels.get(price)
        if level is None:
            level = PriceLevel(price, "sell")
            self.ask_levels[price] = level
            bisect.insort(self.ask_prices, price)
            BookInstrumentation.price_level_creates += 1
        return level

    def _remove_empty_level(self, side: str, price: float) -> None:
        if side == "buy":
            if price not in self.bid_levels:
                return
            del self.bid_levels[price]
            idx = bisect.bisect_left(self.bid_prices, price)
            if idx < len(self.bid_prices) and self.bid_prices[idx] == price:
                self.bid_prices.pop(idx)
        else:
            if price not in self.ask_levels:
                return
            del self.ask_levels[price]
            idx = bisect.bisect_left(self.ask_prices, price)
            if idx < len(self.ask_prices) and self.ask_prices[idx] == price:
                self.ask_prices.pop(idx)
        BookInstrumentation.price_level_removes += 1

    def _unlink_node(self, node: BookNode) -> Order:
        level = node.level
        side = level.side
        price = level.price
        qty = node.order.quantity

        level.remove_node(node)
        self.orders_by_id.pop(node.order.order_id, None)

        if side == "buy":
            self.bid_depth -= qty
        else:
            self.ask_depth -= qty

        if level.is_empty():
            self._remove_empty_level(side, price)

        return node.order

    def add_order(self, order: Order) -> None:
        if order.price is None:
            raise MarketHealthError("Cannot add order without price to book")
        level = self._level_for(order.side, order.price)
        node = level.append(order)
        self.orders_by_id[order.order_id] = node
        if order.side == "buy":
            self.bid_depth += order.quantity
        else:
            self.ask_depth += order.quantity

    def cancel_order(self, order_id: int) -> Optional[Order]:
        node = self.orders_by_id.get(order_id)
        if node is None:
            return None
        return self._unlink_node(node)

    def is_on_book(self, order_id: int) -> bool:
        return order_id in self.orders_by_id

    def assert_not_on_book(self, order_id: int, reason: str) -> None:
        if self.is_on_book(order_id):
            raise MarketHealthError(
                f"Order {order_id} must not be on book ({reason})"
            )

    def apply_resting_fill(self, order_id: int, remaining_qty: int) -> None:
        """Update or remove a resting order after a match (O(1) via node lookup)."""
        node = self.orders_by_id.get(order_id)
        if node is None:
            return
        if remaining_qty <= 0:
            self._unlink_node(node)
            return
        delta = remaining_qty - node.order.quantity
        node.order.quantity = remaining_qty
        node.level.total_quantity += delta
        if node.level.side == "buy":
            self.bid_depth += delta
        else:
            self.ask_depth += delta

    def _node_at_flat_index(self, side: str, idx: int) -> BookNode:
        flat = self._flatten_bids() if side == "buy" else self._flatten_asks()
        return self.orders_by_id[flat[idx].order_id]

    def updateBuyOrders(self, idx: int, qty: int, order: Order) -> None:
        node = self._node_at_flat_index("buy", idx)
        if qty == 0:
            self._unlink_node(node)
            return
        delta = qty - node.order.quantity
        node.order.quantity = qty
        node.level.total_quantity += delta
        self.bid_depth += delta

    def updateSellOrders(self, idx: int, qty: int, order: Order) -> None:
        node = self._node_at_flat_index("sell", idx)
        if qty == 0:
            self._unlink_node(node)
            return
        delta = qty - node.order.quantity
        node.order.quantity = qty
        node.level.total_quantity += delta
        self.ask_depth += delta

    def validate_health(self) -> None:
        """Manual/debug only — scans the full book."""
        BookInstrumentation.cancel_full_scans += 1
        for side, prices, levels in (
            ("buy", self.bid_prices, self.bid_levels),
            ("sell", self.ask_prices, self.ask_levels),
        ):
            for price in prices:
                level = levels[price]
                node = level.head
                while node is not None:
                    if node.order.quantity <= 0:
                        raise MarketHealthError(
                            f"Non-positive quantity on book: {side} "
                            f"order_id={node.order.order_id}"
                        )
                    node = node.next

        best_bid = self.best_bid_price()
        best_ask = self.best_ask_price()
        if best_bid is not None and best_ask is not None:
            if best_bid >= best_ask:
                raise MarketHealthError(
                    f"Crossed or locked market: best_bid={best_bid} "
                    f"must be < best_ask={best_ask}"
                )

        depth_bid = sum(
            self.bid_levels[p].total_quantity for p in self.bid_prices
        )
        depth_ask = sum(
            self.ask_levels[p].total_quantity for p in self.ask_prices
        )
        if depth_bid != self.bid_depth or depth_ask != self.ask_depth:
            raise MarketHealthError(
                f"Depth cache mismatch: bid {self.bid_depth} vs {depth_bid}, "
                f"ask {self.ask_depth} vs {depth_ask}"
            )

    def show_book(self) -> str:
        book_lines = ["Bid"]
        for order in self.getBuyOrders():
            price_display = "MKT" if order.order_type == "market" else order.price
            book_lines.append(f"{order.quantity} @ {price_display}")
        book_lines.append("Ask")
        for order in self.getSellOrders():
            price_display = "MKT" if order.order_type == "market" else order.price
            book_lines.append(f"{order.quantity} @ {price_display}")
        return "\n".join(book_lines)


if __name__ == "__main__":
    book = OrderBook()
    print("Exchange simulator started.")

    while True:
        try:
            result = book.manage_order()
            print(result)
            print("\nUpdated order book:")
            print(book.show_book())
        except ValueError as exc:
            print(f"Input error: {exc}")

        again = input("\nAdd another order? (y/n): ").strip().lower()
        if again != "y":
            print("Exiting exchange simulator.")
            break
