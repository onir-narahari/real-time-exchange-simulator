"""Limit order book: price levels, best-price access, and L2 aggregation.

Layout
------
Each side keeps a ``{price: PriceLevel}`` map plus a sorted array of live
prices. Both arrays are ascending, so the best bid is ``bid_prices[-1]`` and
the best ask is ``ask_prices[0]`` — O(1) top-of-book, O(log n) insert.

``orders_by_id`` maps an order id straight to its linked-list node, so cancels
and fills are O(1) with no scan. The book never flattens itself to answer a
question: depth and order counts are maintained incrementally.
"""

from __future__ import annotations

import bisect
from typing import Dict, Iterator, List, NamedTuple, Optional

from orderbook.order import Order, OrderStatus, Side
from orderbook.price_level import BookNode, PriceLevel


class BookIntegrityError(Exception):
    """Raised by :meth:`OrderBook.validate` when an invariant is violated."""


class LevelView(NamedTuple):
    """One aggregated row of an L2 snapshot."""

    price: float
    quantity: int
    order_count: int


class BookSnapshot(NamedTuple):
    """L2 view: bids descending, asks ascending, both best-first."""

    bids: List[LevelView]
    asks: List[LevelView]

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None


class OrderBook:
    def __init__(self):
        self._bid_levels: Dict[float, PriceLevel] = {}
        self._ask_levels: Dict[float, PriceLevel] = {}
        self._bid_prices: List[float] = []  # ascending; best bid is last
        self._ask_prices: List[float] = []  # ascending; best ask is first

        self.orders_by_id: Dict[int, BookNode] = {}

        self.bid_depth = 0
        self.ask_depth = 0

        self._next_sequence = 1
        self.level_creates = 0
        self.level_removes = 0

    # ------------------------------------------------------------------
    # Top of book
    # ------------------------------------------------------------------

    def best_bid(self) -> Optional[float]:
        return self._bid_prices[-1] if self._bid_prices else None

    def best_ask(self) -> Optional[float]:
        return self._ask_prices[0] if self._ask_prices else None

    def best_level(self, side: Side) -> Optional[PriceLevel]:
        """Best *resting* level on ``side`` — i.e. the one an aggressor hits."""
        if side is Side.BUY:
            if not self._bid_prices:
                return None
            return self._bid_levels[self._bid_prices[-1]]
        if not self._ask_prices:
            return None
        return self._ask_levels[self._ask_prices[0]]

    def spread(self) -> Optional[float]:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return ask - bid

    def mid_price(self) -> Optional[float]:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    # ------------------------------------------------------------------
    # L2 view
    # ------------------------------------------------------------------

    def l2(self, depth: Optional[int] = None) -> BookSnapshot:
        """Aggregated book, best-first. ``depth`` caps levels per side."""
        bid_prices = reversed(self._bid_prices)
        ask_prices = iter(self._ask_prices)

        bids: List[LevelView] = []
        for price in bid_prices:
            if depth is not None and len(bids) >= depth:
                break
            level = self._bid_levels[price]
            bids.append(LevelView(price, level.total_quantity, level.order_count))

        asks: List[LevelView] = []
        for price in ask_prices:
            if depth is not None and len(asks) >= depth:
                break
            level = self._ask_levels[price]
            asks.append(LevelView(price, level.total_quantity, level.order_count))

        return BookSnapshot(bids, asks)

    def depth_at(self, side: Side, price: float) -> int:
        levels = self._bid_levels if Side(side) is Side.BUY else self._ask_levels
        level = levels.get(price)
        return level.total_quantity if level is not None else 0

    def level_count(self, side: Side) -> int:
        return len(self._bid_prices if Side(side) is Side.BUY else self._ask_prices)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, order: Order) -> None:
        """Rest ``order`` at the back of its price level (time priority)."""
        if order.price is None:
            raise BookIntegrityError("cannot rest an order without a price")
        if order.remaining <= 0:
            raise BookIntegrityError(
                f"cannot rest order {order.order_id} with remaining={order.remaining}"
            )
        if order.order_id in self.orders_by_id:
            raise BookIntegrityError(f"duplicate order id {order.order_id}")

        level = self._level_for(order.side, order.price)
        node = level.append(order)
        self.orders_by_id[order.order_id] = node

        order.sequence = self._next_sequence
        self._next_sequence += 1
        if order.status is OrderStatus.NEW:
            order.status = OrderStatus.RESTING

        if order.side is Side.BUY:
            self.bid_depth += order.remaining
        else:
            self.ask_depth += order.remaining

    def cancel(self, order_id: int) -> Optional[Order]:
        """Remove a resting order in O(1). Returns None if it isn't on the book."""
        node = self.orders_by_id.get(order_id)
        if node is None:
            return None
        order = self._unlink(node)
        order.status = OrderStatus.CANCELLED
        return order

    def fill(self, order_id: int, quantity: int) -> Order:
        """Apply ``quantity`` of execution to a resting order.

        Fully filled orders leave the book (and empty levels are collected).
        """
        node = self.orders_by_id.get(order_id)
        if node is None:
            raise BookIntegrityError(f"order {order_id} is not on the book")
        order = node.order
        if quantity <= 0 or quantity > order.remaining:
            raise BookIntegrityError(
                f"invalid fill of {quantity} against remaining={order.remaining}"
            )

        if quantity == order.remaining:
            self._unlink(node)
            order.remaining = 0
            order.status = OrderStatus.FILLED
            return order

        node.level.reduce(node, quantity)
        if order.side is Side.BUY:
            self.bid_depth -= quantity
        else:
            self.ask_depth -= quantity
        order.status = OrderStatus.PARTIALLY_FILLED
        return order

    def consume_level(self, level: PriceLevel, node: BookNode, quantity: int) -> Order:
        """Apply ``quantity`` of execution to a known node in a known level.

        This is the matching engine's hot path. The sweep already holds the
        level (from :meth:`best_level`) and the node (its head), so unlike
        :meth:`fill` this does no id lookup and no re-validation: the caller
        guarantees ``0 < quantity <= node.order.remaining``. That collapses
        what used to be ``fill -> _unlink -> level.remove -> is_empty ->
        _remove_level`` — five nested calls re-deriving what the caller knew
        — into one.

        A fully consumed order is unlinked inline and its level collected if
        it emptied; a partially consumed order stays at the head.
        """
        order = node.order
        remaining = order.remaining

        if quantity < remaining:
            order.remaining = remaining - quantity
            level.total_quantity -= quantity
            if order.side is Side.BUY:
                self.bid_depth -= quantity
            else:
                self.ask_depth -= quantity
            order.status = OrderStatus.PARTIALLY_FILLED
            return order

        # Full fill: unlink the node inline, then collect the level if empty.
        level.total_quantity -= quantity
        level.order_count -= 1
        prev, nxt = node.prev, node.next
        if prev is not None:
            prev.next = nxt
        else:
            level.head = nxt
        if nxt is not None:
            nxt.prev = prev
        else:
            level.tail = prev
        node.prev = None
        node.next = None
        del self.orders_by_id[order.order_id]

        if order.side is Side.BUY:
            self.bid_depth -= quantity
        else:
            self.ask_depth -= quantity

        order.remaining = 0
        order.status = OrderStatus.FILLED

        if level.head is None:
            if level.side is Side.BUY:
                levels, prices = self._bid_levels, self._bid_prices
            else:
                levels, prices = self._ask_levels, self._ask_prices
            del levels[level.price]
            idx = bisect.bisect_left(prices, level.price)
            if idx < len(prices) and prices[idx] == level.price:
                prices.pop(idx)
            self.level_removes += 1
        return order

    def contains(self, order_id: int) -> bool:
        return order_id in self.orders_by_id

    def get(self, order_id: int) -> Optional[Order]:
        node = self.orders_by_id.get(order_id)
        return node.order if node is not None else None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _level_for(self, side: Side, price: float) -> PriceLevel:
        if side is Side.BUY:
            levels, prices = self._bid_levels, self._bid_prices
        else:
            levels, prices = self._ask_levels, self._ask_prices

        level = levels.get(price)
        if level is None:
            level = PriceLevel(price, side)
            levels[price] = level
            bisect.insort(prices, price)
            self.level_creates += 1
        return level

    def _unlink(self, node: BookNode) -> Order:
        level = node.level
        order = node.order
        quantity = order.remaining

        level.remove(node)
        del self.orders_by_id[order.order_id]

        if order.side is Side.BUY:
            self.bid_depth -= quantity
        else:
            self.ask_depth -= quantity

        if level.is_empty():
            self._remove_level(order.side, level.price)
        return order

    def _remove_level(self, side: Side, price: float) -> None:
        if side is Side.BUY:
            levels, prices = self._bid_levels, self._bid_prices
        else:
            levels, prices = self._ask_levels, self._ask_prices

        if levels.pop(price, None) is None:
            return
        idx = bisect.bisect_left(prices, price)
        if idx < len(prices) and prices[idx] == price:
            prices.pop(idx)
        self.level_removes += 1

    # ------------------------------------------------------------------
    # Introspection (tests / display — not the hot path)
    # ------------------------------------------------------------------

    def orders(self, side: Side) -> Iterator[Order]:
        """Every resting order on ``side`` in strict price-time priority."""
        if Side(side) is Side.BUY:
            for price in reversed(self._bid_prices):
                yield from self._bid_levels[price]
        else:
            for price in self._ask_prices:
                yield from self._ask_levels[price]

    def __len__(self) -> int:
        return len(self.orders_by_id)

    def validate(self) -> None:
        """Assert every book invariant. O(n) — for tests and debugging only."""
        for side, prices, levels in (
            (Side.BUY, self._bid_prices, self._bid_levels),
            (Side.SELL, self._ask_prices, self._ask_levels),
        ):
            if list(prices) != sorted(prices):
                raise BookIntegrityError(f"{side.value} price array is unsorted")
            if len(set(prices)) != len(prices):
                raise BookIntegrityError(f"{side.value} price array has duplicates")
            if set(prices) != set(levels):
                raise BookIntegrityError(
                    f"{side.value} price array and level map disagree"
                )

            for price in prices:
                level = levels[price]
                if level.is_empty():
                    raise BookIntegrityError(f"empty {side.value} level at {price}")

                counted_qty = 0
                counted_orders = 0
                for order in level:
                    if order.remaining <= 0:
                        raise BookIntegrityError(
                            f"order {order.order_id} rests with "
                            f"remaining={order.remaining}"
                        )
                    if order.price != price or order.side is not side:
                        raise BookIntegrityError(
                            f"order {order.order_id} is filed at the wrong level"
                        )
                    if not self.contains(order.order_id):
                        raise BookIntegrityError(
                            f"order {order.order_id} is linked but not indexed"
                        )
                    counted_qty += order.remaining
                    counted_orders += 1

                if counted_qty != level.total_quantity:
                    raise BookIntegrityError(
                        f"{side.value} level {price} quantity cache is "
                        f"{level.total_quantity}, walked {counted_qty}"
                    )
                if counted_orders != level.order_count:
                    raise BookIntegrityError(
                        f"{side.value} level {price} count cache is "
                        f"{level.order_count}, walked {counted_orders}"
                    )

        walked_bid = sum(lv.total_quantity for lv in self._bid_levels.values())
        walked_ask = sum(lv.total_quantity for lv in self._ask_levels.values())
        if walked_bid != self.bid_depth or walked_ask != self.ask_depth:
            raise BookIntegrityError(
                f"depth cache mismatch: bid {self.bid_depth} vs {walked_bid}, "
                f"ask {self.ask_depth} vs {walked_ask}"
            )

        indexed = sum(len(lv) for lv in self._bid_levels.values())
        indexed += sum(len(lv) for lv in self._ask_levels.values())
        if indexed != len(self.orders_by_id):
            raise BookIntegrityError(
                f"index holds {len(self.orders_by_id)} orders, book holds {indexed}"
            )

        bid, ask = self.best_bid(), self.best_ask()
        if bid is not None and ask is not None and bid >= ask:
            raise BookIntegrityError(f"crossed book: best bid {bid} >= best ask {ask}")

    def __repr__(self) -> str:
        return (
            f"OrderBook(bid {self.bid_depth}@{self.best_bid()} | "
            f"{self.best_ask()}@{self.ask_depth} ask, {len(self)} orders)"
        )
