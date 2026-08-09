"""A thin session layer over the core engine.

The core knows nothing about who is trading or how the session is going. This
wrapper adds what a *simulation* needs and nothing the engine needs:

- order-id allocation
- a trade tape with timestamps and both sides' order ids
- session metrics

It is deliberately small. If something here starts looking like matching
logic, it belongs in ``orderbook/`` instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, NamedTuple, Optional

from orderbook import MatchingEngine, MatchResult, Order, OrderBook, OrderType, Side
from sim.metrics import SimulationMetrics


class TapeEntry(NamedTuple):
    """A printed trade, as a downstream consumer sees it."""

    trade_id: int
    price: float
    quantity: int
    side: str  # aggressor side
    buy_order_id: int
    sell_order_id: int
    timestamp: datetime


class Exchange:
    def __init__(self):
        self.book = OrderBook()
        self.engine = MatchingEngine()
        self.metrics = SimulationMetrics()
        self.trade_history: List[TapeEntry] = []
        self._next_order_id = 1

    # --- Order entry ---

    def new_order(
        self,
        side: Side,
        quantity: int,
        price: Optional[float] = None,
        order_type: OrderType = OrderType.LIMIT,
    ) -> Order:
        """Allocate an id and build an order; does not submit it."""
        order = Order(self._next_order_id, side, order_type, quantity, price)
        self._next_order_id += 1
        return order

    def submit(self, order: Order) -> MatchResult:
        best_bid_before = self.book.best_bid()
        best_ask_before = self.book.best_ask()

        self.metrics.on_order_submitted()
        result = self.engine.submit(order, self.book)

        now = datetime.now()
        for trade in result.trades:
            self.trade_history.append(
                TapeEntry(
                    trade_id=trade.trade_id,
                    price=trade.price,
                    quantity=trade.quantity,
                    side=trade.aggressor_side.value,
                    buy_order_id=trade.buy_order_id,
                    sell_order_id=trade.sell_order_id,
                    timestamp=now,
                )
            )
            self.metrics.on_trade(trade.quantity)
            if not self.book.contains(trade.resting_order_id):
                self.metrics.on_order_filled(trade.resting_order_id)

        if order.is_filled:
            self.metrics.on_order_filled(order.order_id)

        self._note_book_change(best_bid_before, best_ask_before)
        return result

    def submit_new(
        self,
        side: Side,
        quantity: int,
        price: Optional[float] = None,
        order_type: OrderType = OrderType.LIMIT,
    ) -> MatchResult:
        return self.submit(self.new_order(side, quantity, price, order_type))

    def cancel(self, order_id: int) -> Optional[Order]:
        best_bid_before = self.book.best_bid()
        best_ask_before = self.book.best_ask()

        order = self.engine.cancel(order_id, self.book)
        if order is not None:
            self.metrics.on_cancel()
            self._note_book_change(best_bid_before, best_ask_before)
        return order

    # --- Views ---

    def is_resting(self, order_id: int) -> bool:
        return self.book.contains(order_id)

    def trades_since(self, index: int) -> List[TapeEntry]:
        """Tape entries recorded after ``index``. O(new), never O(history)."""
        return self.trade_history[index:]

    def show_market_summary(self) -> None:
        book = self.book
        snapshot = book.l2(depth=5)
        spread = book.spread()
        print("\nMARKET SUMMARY")
        print(f"  Best bid / ask: {book.best_bid()} / {book.best_ask()}")
        print(f"  Spread:         {'n/a' if spread is None else round(spread, 4)}")
        print(f"  Depth:          {book.bid_depth:,} bid / {book.ask_depth:,} ask")
        print(f"  Resting orders: {len(book):,}")
        print(f"  Trades:         {len(self.trade_history):,}")
        for level in reversed(snapshot.asks):
            print(f"    ask {level.price:>8}  {level.quantity:>7,} ({level.order_count})")
        for level in snapshot.bids:
            print(f"    bid {level.price:>8}  {level.quantity:>7,} ({level.order_count})")

    def show_recent_trades(self, count: int = 5) -> None:
        print(f"\nRECENT TRADES (last {count})")
        if not self.trade_history:
            print("  none")
            return
        for entry in self.trade_history[-count:]:
            print(
                f"  #{entry.trade_id} {entry.side.upper():<4} "
                f"{entry.quantity} @ {entry.price}"
            )

    def show_metrics(self) -> None:
        self.metrics.show()

    # --- Internals ---

    def _note_book_change(
        self, best_bid_before: Optional[float], best_ask_before: Optional[float]
    ) -> None:
        best_bid = self.book.best_bid()
        best_ask = self.book.best_ask()
        if best_bid != best_bid_before or best_ask != best_ask_before:
            self.metrics.on_book_change(best_bid, best_ask)
