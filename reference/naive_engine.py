"""A deliberately naive order book and matching engine.

This exists to be *obviously* correct, not fast. Every design choice here is
the dumbest one available:

- the book is one flat Python list of resting orders, in arrival order
- finding the best price is a linear scan with ``min``/``max``
- cancelling is a linear scan and a ``list.remove``
- an L2 view is rebuilt from scratch by grouping the flat list
- nothing is cached, so nothing can go stale

It is O(n) per operation and would fall over under load. That is fine — its
only job is to be a trustworthy answer key for the optimized engine in
``orderbook/``, which it never imports. The two share no code at all, so a bug
would have to occur independently in both to go unnoticed.

Price-time priority is expressed directly as a sort key:

    bids: (-price, sequence)   -> highest price first, then earliest arrival
    asks: ( price, sequence)   -> lowest price first, then earliest arrival
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Tuple

BUY = "buy"
SELL = "sell"
LIMIT = "limit"
MARKET = "market"


class NaiveTrade(NamedTuple):
    trade_id: int
    price: float
    quantity: int
    aggressor_side: str
    aggressor_order_id: int
    resting_order_id: int


class NaiveResult(NamedTuple):
    order_id: int
    trades: List[NaiveTrade]
    remaining: int
    resting: bool


class RestingOrder:
    """A resting order. A plain object; no slots, no tricks."""

    def __init__(self, order_id: int, side: str, price: float, quantity: int, sequence: int):
        self.order_id = order_id
        self.side = side
        self.price = price
        self.quantity = quantity
        self.sequence = sequence

    def __repr__(self) -> str:
        return (
            f"RestingOrder(id={self.order_id}, {self.side} "
            f"{self.quantity} @ {self.price}, seq={self.sequence})"
        )


class NaiveEngine:
    """Flat-list order book with linear-scan matching."""

    def __init__(self):
        self.resting: List[RestingOrder] = []
        self._next_sequence = 1
        self._next_trade_id = 1

    # ------------------------------------------------------------------
    # Order entry
    # ------------------------------------------------------------------

    def submit(
        self,
        order_id: int,
        side: str,
        order_type: str,
        quantity: int,
        price: Optional[float] = None,
    ) -> NaiveResult:
        """Match against the book, then rest the remainder if it is a limit."""
        remaining = quantity
        trades: List[NaiveTrade] = []

        while remaining > 0:
            best = self._best_opposing(side)
            if best is None:
                break
            if not self._willing_to_trade(side, order_type, price, best.price):
                break

            traded = min(remaining, best.quantity)
            trades.append(
                NaiveTrade(
                    trade_id=self._next_trade_id,
                    price=best.price,
                    quantity=traded,
                    aggressor_side=side,
                    aggressor_order_id=order_id,
                    resting_order_id=best.order_id,
                )
            )
            self._next_trade_id += 1

            remaining -= traded
            best.quantity -= traded
            if best.quantity == 0:
                self.resting.remove(best)

        if remaining > 0 and order_type == LIMIT:
            self.resting.append(
                RestingOrder(order_id, side, price, remaining, self._next_sequence)
            )
            self._next_sequence += 1
            return NaiveResult(order_id, trades, remaining, resting=True)

        # A market order's remainder simply disappears.
        return NaiveResult(order_id, trades, remaining, resting=False)

    def cancel(self, order_id: int) -> bool:
        """Remove a resting order. Returns False if it was not on the book."""
        for order in self.resting:
            if order.order_id == order_id:
                self.resting.remove(order)
                return True
        return False

    # ------------------------------------------------------------------
    # Matching helpers
    # ------------------------------------------------------------------

    def _best_opposing(self, side: str) -> Optional[RestingOrder]:
        """The single order an aggressor on ``side`` would hit next."""
        if side == BUY:
            candidates = [o for o in self.resting if o.side == SELL]
            if not candidates:
                return None
            return min(candidates, key=lambda o: (o.price, o.sequence))

        candidates = [o for o in self.resting if o.side == BUY]
        if not candidates:
            return None
        return min(candidates, key=lambda o: (-o.price, o.sequence))

    @staticmethod
    def _willing_to_trade(
        side: str, order_type: str, price: Optional[float], resting_price: float
    ) -> bool:
        if order_type == MARKET:
            return True
        if side == BUY:
            return price >= resting_price
        return price <= resting_price

    # ------------------------------------------------------------------
    # Views (rebuilt from scratch every time, on purpose)
    # ------------------------------------------------------------------

    def _sorted_side(self, side: str) -> List[RestingOrder]:
        orders = [o for o in self.resting if o.side == side]
        if side == BUY:
            return sorted(orders, key=lambda o: (-o.price, o.sequence))
        return sorted(orders, key=lambda o: (o.price, o.sequence))

    def best_bid(self) -> Optional[float]:
        bids = [o.price for o in self.resting if o.side == BUY]
        return max(bids) if bids else None

    def best_ask(self) -> Optional[float]:
        asks = [o.price for o in self.resting if o.side == SELL]
        return min(asks) if asks else None

    def depth(self, side: str) -> int:
        return sum(o.quantity for o in self.resting if o.side == side)

    def l2(self, side: str) -> List[Tuple[float, int, int]]:
        """``(price, total_quantity, order_count)`` per level, best-first."""
        levels: List[Tuple[float, int, int]] = []
        for order in self._sorted_side(side):
            if levels and levels[-1][0] == order.price:
                price, quantity, count = levels[-1]
                levels[-1] = (price, quantity + order.quantity, count + 1)
            else:
                levels.append((order.price, order.quantity, 1))
        return levels

    def l3(self, side: str) -> List[Tuple[int, float, int]]:
        """``(order_id, price, quantity)`` in strict price-time priority."""
        return [(o.order_id, o.price, o.quantity) for o in self._sorted_side(side)]

    def contains(self, order_id: int) -> bool:
        return any(o.order_id == order_id for o in self.resting)

    def __len__(self) -> int:
        return len(self.resting)
