"""In-memory simulation metrics collected from exchange lifecycle events."""


class SimulationMetrics:
    def __init__(self):
        self.total_trades = 0
        self.total_volume = 0
        self.cancellation_count = 0
        self.orders_submitted = 0
        self.filled_order_ids = set()
        self._spread_sum = 0.0
        self._spread_samples = 0
        self.book_update_count = 0
        self.book_update_events_emitted = 0
        self.book_update_events_skipped = 0
        self.book_event_payload_builds = 0

    def record_spread_sample(self, best_bid: float, best_ask: float) -> None:
        self._spread_sum += best_ask - best_bid
        self._spread_samples += 1

    def on_event(self, event_type, data):
        if event_type == "TRADE_EXECUTED":
            self.total_trades += 1
            self.total_volume += data.get("quantity", 0)
        elif event_type == "ORDER_CANCELLED":
            self.cancellation_count += 1
        elif event_type == "NEW_ORDER":
            self.orders_submitted += 1
        elif event_type == "ORDER_FILLED":
            order_id = data.get("order_id")
            if order_id is not None:
                self.filled_order_ids.add(order_id)
        elif event_type == "BOOK_UPDATED":
            best_bid = data.get("best_bid")
            best_ask = data.get("best_ask")
            if best_bid is not None and best_ask is not None:
                self.record_spread_sample(best_bid, best_ask)

    @property
    def average_spread(self):
        if self._spread_samples == 0:
            return None
        return self._spread_sum / self._spread_samples

    @property
    def fill_rate(self):
        if self.orders_submitted == 0:
            return 0.0
        return len(self.filled_order_ids) / self.orders_submitted

    def show_metrics(self):
        avg_spread = self.average_spread
        spread_display = (
            round(avg_spread, 4) if avg_spread is not None else "n/a"
        )
        fill_pct = round(self.fill_rate * 100, 2)

        print("\nSIMULATION METRICS")
        print(f"  Total trades executed:  {self.total_trades}")
        print(f"  Total traded volume:    {self.total_volume}")
        print(f"  Cancellations:          {self.cancellation_count}")
        print(f"  Orders submitted:       {self.orders_submitted}")
        print(f"  Orders fully filled:    {len(self.filled_order_ids)}")
        print(f"  Fill rate:              {fill_pct}%")
        print(f"  Average spread:         {spread_display}")
