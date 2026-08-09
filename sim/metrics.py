"""Session metrics for the simulation layer.

Driven by explicit calls from :class:`sim.exchange.Exchange` rather than by
dispatching on an event log — the counters are cheap and the call sites are
obvious.
"""

from __future__ import annotations

from typing import Optional


class SimulationMetrics:
    def __init__(self):
        self.orders_submitted = 0
        self.total_trades = 0
        self.total_volume = 0
        self.cancellations = 0
        self.filled_order_ids = set()
        self._spread_sum = 0.0
        self._spread_samples = 0

    # --- Recording ---

    def on_order_submitted(self) -> None:
        self.orders_submitted += 1

    def on_trade(self, quantity: int) -> None:
        self.total_trades += 1
        self.total_volume += quantity

    def on_order_filled(self, order_id: int) -> None:
        self.filled_order_ids.add(order_id)

    def on_cancel(self) -> None:
        self.cancellations += 1

    def on_book_change(
        self, best_bid: Optional[float], best_ask: Optional[float]
    ) -> None:
        if best_bid is not None and best_ask is not None:
            self._spread_sum += best_ask - best_bid
            self._spread_samples += 1

    # --- Derived ---

    @property
    def average_spread(self) -> Optional[float]:
        if self._spread_samples == 0:
            return None
        return self._spread_sum / self._spread_samples

    @property
    def orders_fully_filled(self) -> int:
        return len(self.filled_order_ids)

    @property
    def fill_rate(self) -> float:
        if self.orders_submitted == 0:
            return 0.0
        return len(self.filled_order_ids) / self.orders_submitted

    def show(self) -> None:
        spread = self.average_spread
        print("\nSESSION METRICS")
        print(f"  Orders submitted:    {self.orders_submitted:,}")
        print(f"  Trades executed:     {self.total_trades:,}")
        print(f"  Volume traded:       {self.total_volume:,}")
        print(f"  Cancellations:       {self.cancellations:,}")
        print(f"  Orders fully filled: {self.orders_fully_filled:,}")
        print(f"  Fill rate:           {self.fill_rate * 100:.2f}%")
        print(
            f"  Average spread:      "
            f"{'n/a' if spread is None else round(spread, 4)}"
        )
