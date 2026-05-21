from collections import deque

from order_book import OrderBook, MarketHealthError
from matching_engine import MatchingEngine
from order import Order
from trades import Trade
from datetime import datetime
from event import Event
from metrics import SimulationMetrics
import sim_config
from timing import timed

class Exchange:
    def __init__(self):
        self.order_book = OrderBook()
        self.engine = MatchingEngine()
        self._next_order_id = self.order_book._next_order_id
        self.trade_id = 1
        self.event_id = 1
        self.event_count = 0
        self.trade_history = []
        if sim_config.STORE_EVENT_HISTORY:
            self.event_history = deque(maxlen=sim_config.MAX_EVENT_HISTORY)
        else:
            self.event_history = deque(maxlen=0)
        self.metrics = SimulationMetrics()

    def manual_order(self, side, quantity, price=None, order_type="limit"):
        """Build an order with the next exchange id, emit NEW_ORDER, return Order."""
        order_id = self._next_order_id
        order = Order(order_id, side, price, quantity, order_type)
        fields = {
            "id": order_id,
            "side": order.side,
            "price": order.price,
            "quantity": order.quantity,
            "time": datetime.now(),
        }
        self._next_order_id += 1
        self._emit("NEW_ORDER", fields)
        return order

    def create_order(self):
        choice = input(
            "Do you want market buy, market sell, limit buy, or limit sell? "
        ).strip().lower()

        if choice not in {"market buy", "market sell", "limit buy", "limit sell"}:
            raise ValueError("Invalid choice. Use market buy/sell or limit buy/sell.")

        side = "buy" if "buy" in choice else "sell"
        order_type = "market" if "market" in choice else "limit"

        action_word = "buy" if side == "buy" else "sell"
        quantity = int(input(f"What quantity do you want to {action_word}? "))
        price = None
        if order_type == "limit":
            price = float(input(f"What price do you want to {action_word} at? "))
        return self.manual_order(side, quantity, price, order_type)
    
    def _emit(self, event_type, data):
        with timed("event_emit"):
            payload = dict(data)
            event_id = self.event_id
            self.event_id += 1
            self.event_count += 1
            if sim_config.STORE_EVENT_HISTORY:
                self.event_history.append(Event(event_id, event_type, payload))
            self.metrics.on_event(event_type, payload)

    def _emit_order_added(self, order):
        self._emit(
            "ORDER_ADDED",
            {
                "order_id": order.order_id,
                "side": order.side,
                "price": order.price,
                "quantity": order.quantity,
            },
        )
    def _emit_order_cancelled(self,order):
        self._emit(
            "ORDER_CANCELLED",
            {
                "order_id": order.order_id,
                "side": order.side,
                "price": order.price,
                "quantity": order.quantity,
            },
        )
    def _emit_book_updated(self):
        self.metrics.book_update_count += 1
        if not sim_config.EMIT_BOOK_UPDATED_EVENTS:
            self.metrics.book_update_events_skipped += 1
            best_bid = self.order_book.best_bid_price()
            best_ask = self.order_book.best_ask_price()
            if best_bid is not None and best_ask is not None:
                self.metrics.record_spread_sample(best_bid, best_ask)
            return

        self.metrics.book_event_payload_builds += 1
        best_bid = self.order_book.best_bid_price()
        best_ask = self.order_book.best_ask_price()
        self.metrics.book_update_events_emitted += 1
        self._emit("BOOK_UPDATED", {"best_bid": best_bid, "best_ask": best_ask})

    def _is_order_on_book(self, order):
        return self.order_book.is_on_book(order.order_id)

    def _assert_trade_remainders(self, incoming_remaining, resting_remaining):
        if incoming_remaining < 0 or resting_remaining < 0:
            raise MarketHealthError(
                f"Negative trade remainder: incoming={incoming_remaining} "
                f"resting={resting_remaining}"
            )

    def _assert_remaining_nonnegative(self, order):
        if order.quantity < 0:
            raise MarketHealthError(
                f"Negative remaining quantity on order_id={order.order_id} "
                f"quantity={order.quantity}"
            )

    def submit_order(self, order):
        with timed("submit_order"):
            return self._submit_order_impl(order)

    def _trade_executed_payload(self, order, resting_oid, trade_data):
        if order.side == "buy":
            return {
                "trade_id": self.trade_id,
                "price": trade_data[2],
                "quantity": trade_data[3],
                "aggressor_side": order.side,
                "buy_order_id": order.order_id,
                "sell_order_id": resting_oid,
            }
        return {
            "trade_id": self.trade_id,
            "price": trade_data[2],
            "quantity": trade_data[3],
            "aggressor_side": order.side,
            "buy_order_id": resting_oid,
            "sell_order_id": order.order_id,
        }

    def _apply_resting_fill(self, resting_order_id, resting_remaining):
        self.order_book.apply_resting_fill(resting_order_id, resting_remaining)

    def _submit_order_impl(self, order):
        if order.order_type == "limit":
            with timed("submit_validation"):
                last_price = None
                had_trade = False
                done_msg = "Bought @" if order.side == "buy" else "Sold @"
            while order.quantity > 0:
                with timed("submit_matching_loop"):
                    trade_data = self.engine.process_order(order, self.order_book)
                    if not trade_data[0]:
                        break
                had_trade = True
                executed_quantity = trade_data[3]
                incoming_remaining = trade_data[4]
                resting_remaining = trade_data[5]
                self._assert_trade_remainders(incoming_remaining, resting_remaining)
                last_price = trade_data[2]
                resting_oid = trade_data[1]
                with timed("submit_fill_processing"):
                    self._emit(
                        "TRADE_EXECUTED",
                        self._trade_executed_payload(order, resting_oid, trade_data),
                    )
                    self._apply_resting_fill(resting_oid, resting_remaining)
                with timed("submit_book_events"):
                    if incoming_remaining <= 0:
                        self._emit(
                            "ORDER_FILLED",
                            {
                                "order_id": order.order_id,
                                "side": order.side,
                                "executed_quantity": executed_quantity,
                                "remaining_quantity": 0,
                            },
                        )
                        self._emit_book_updated()
                    else:
                        self._emit(
                            "ORDER_PARTIALLY_FILLED",
                            {
                                "order_id": order.order_id,
                                "side": order.side,
                                "executed_quantity": executed_quantity,
                                "remaining_quantity": incoming_remaining,
                            },
                        )
                        self._emit_book_updated()
                with timed("submit_fill_processing"):
                    timestamp = datetime.now()
                    trade = Trade(
                        self.trade_id,
                        trade_data[2],
                        executed_quantity,
                        timestamp,
                        order.side,
                    )
                    self.trade_history.append(trade)
                    self.trade_id += 1
                    order.quantity = incoming_remaining
                    self._assert_remaining_nonnegative(order)
                with timed("submit_cleanup"):
                    if resting_remaining <= 0:
                        self.order_book.assert_not_on_book(
                            resting_oid, "resting fully filled"
                        )
                    if incoming_remaining <= 0:
                        self.order_book.assert_not_on_book(
                            order.order_id, "incoming fully filled"
                        )
                        return done_msg, last_price
            if not had_trade:
                with timed("submit_book_insert"):
                    self.order_book.add_order(order)
                    self._emit_order_added(order)
                    self._emit_book_updated()
                return "Order added to book"
            if order.quantity > 0 and not self._is_order_on_book(order):
                with timed("submit_book_insert"):
                    self.order_book.add_order(order)
                    self._emit_order_added(order)
                    self._emit_book_updated()
            return done_msg, last_price

        if order.order_type == "market":
            with timed("submit_validation"):
                is_buy = order.side == "buy"
                if is_buy:
                    empty_msg = "No sell orders available for market buy"
                    partial_msg = (
                        "Market buy partially filled; insufficient liquidity"
                    )
                    done_msg = "Bought @"
                    no_liquidity = self.order_book.best_ask_price() is None
                else:
                    empty_msg = "No buy orders available for market sell"
                    partial_msg = (
                        "Market sell partially filled; insufficient liquidity"
                    )
                    done_msg = "Sold @"
                    no_liquidity = self.order_book.best_bid_price() is None
                if no_liquidity:
                    return empty_msg
                last_price = None
            while order.quantity > 0:
                with timed("submit_matching_loop"):
                    trade_data = self.engine.process_order(order, self.order_book)
                    if not trade_data[0]:
                        break
                executed_quantity = trade_data[3]
                incoming_remaining = trade_data[4]
                resting_remaining = trade_data[5]
                self._assert_trade_remainders(incoming_remaining, resting_remaining)
                last_price = trade_data[2]
                resting_oid = trade_data[1]
                with timed("submit_fill_processing"):
                    self._emit(
                        "TRADE_EXECUTED",
                        self._trade_executed_payload(order, resting_oid, trade_data),
                    )
                    timestamp = datetime.now()
                    trade = Trade(
                        self.trade_id,
                        trade_data[2],
                        executed_quantity,
                        timestamp,
                        order.side,
                    )
                    self.trade_history.append(trade)
                    self.trade_id += 1
                    order.quantity = incoming_remaining
                    self._assert_remaining_nonnegative(order)
                    self._apply_resting_fill(resting_oid, resting_remaining)
                with timed("submit_book_events"):
                    if incoming_remaining <= 0:
                        self._emit(
                            "ORDER_FILLED",
                            {
                                "order_id": order.order_id,
                                "side": order.side,
                                "executed_quantity": executed_quantity,
                                "remaining_quantity": 0,
                            },
                        )
                        self._emit_book_updated()
                    else:
                        self._emit(
                            "ORDER_PARTIALLY_FILLED",
                            {
                                "order_id": order.order_id,
                                "side": order.side,
                                "executed_quantity": executed_quantity,
                                "remaining_quantity": incoming_remaining,
                            },
                        )
                        self._emit_book_updated()
                with timed("submit_cleanup"):
                    if resting_remaining <= 0:
                        self.order_book.assert_not_on_book(
                            resting_oid, "resting fully filled"
                        )
                    if incoming_remaining <= 0:
                        self.order_book.assert_not_on_book(
                            order.order_id, "incoming fully filled"
                        )
                        return done_msg, last_price
            if order.quantity > 0:
                return partial_msg
            with timed("submit_cleanup"):
                self.order_book.assert_not_on_book(
                    order.order_id, "incoming fully filled"
                )
            return done_msg, last_price

        with timed("submit_validation"):
            return "Unsupported order type"
    def cancel_order(self, order_id):
        with timed("cancel_order"):
            order = self.order_book.cancel_order(order_id)
            if order is None:
                return "order not found"
            self._emit_order_cancelled(order)
            self.order_book.assert_not_on_book(order_id, "cancelled")
            self._emit_book_updated()
            return "removed", order
            
            
             
            
            
            
                
    def getBook(self):
        return self.order_book.show_book()
    def show_trade_history(self):
        print("\nTRADE HISTORY")

        if not self.trade_history:
            print("No trades executed yet.")
            return

        for trade in self.trade_history:
            trade.show_trade()
        return self.order_book.show_book()
    def show_events_history(self):
        print("\nEVENTS HISTORY")

        if not self.event_history:
            print("No events recorded yet.")
            return

        for ev in self.event_history:
            print(f"  [{ev.event_id}] {ev.type} {ev.data}")

    def show_recent_trades(self, n=5):
        print(f"\nRECENT TRADES (last {n})")
        if not self.trade_history:
            print("  No trades executed yet.")
            return
        for trade in self.trade_history[-n:]:
            print(
                f"  #{trade.trade_id} {trade.side.upper()} "
                f"{trade.quantity} @ {trade.price}"
            )

    def get_recent_events(self, n=10):
        """Return the latest stored events (bounded deque tail)."""
        if n <= 0:
            return []
        return list(self.event_history)[-n:]

    def show_recent_events(self, n=10):
        print(f"\nRECENT EVENTS (last {n})")
        recent = self.get_recent_events(n)
        if not recent:
            print("  No events recorded yet.")
            return
        for ev in recent:
            print(f"  [{ev.event_id}] {ev.type} {ev.data}")

    def show_market_summary(self):
        buys = self.order_book.getBuyOrders()
        sells = self.order_book.getSellOrders()
        best_bid = buys[0].price if buys else None
        best_ask = sells[0].price if sells else None
        spread = (
            round(best_ask - best_bid, 2)
            if best_bid is not None and best_ask is not None
            else None
        )
        bid_depth = sum(o.quantity for o in buys)
        ask_depth = sum(o.quantity for o in sells)
        total_trades = len(self.trade_history)
        total_volume = sum(t.quantity for t in self.trade_history)

        print("\nMARKET SUMMARY")
        print(f"  Total trades:  {total_trades}")
        print(f"  Total volume:  {total_volume}")
        print(f"  Best bid:      {best_bid}")
        print(f"  Best ask:      {best_ask}")
        print(f"  Spread:        {spread}")
        print(f"  Bid depth:     {bid_depth}")
        print(f"  Ask depth:     {ask_depth}")

    def show_metrics(self):
        self.metrics.show_metrics()

if __name__ == "__main__":
    exchange = Exchange()
    print("Exchange simulator started.")

    while True:
        print("\n--- Main menu ---")
        print("1. Add order")
        print("2. Cancel order")
        print("3. Exit")
        choice = input("Choose an option (1/2/3): ").strip()

        if choice == "3":
            print("Exiting exchange simulator.")
            break

        if choice == "1":
            try:
                order = exchange.create_order()
                result = exchange.submit_order(order)
                print(result)
                print("\nUpdated order book:")
                print(exchange.getBook())
                exchange.show_trade_history()
                exchange.show_events_history()
            except ValueError as exc:
                print(f"Input error: {exc}")

        elif choice == "2":
            try:
                order_id = int(input("Order ID to cancel: ").strip())
                result = exchange.cancel_order(order_id)
                print(result)
                print("\nUpdated order book:")
                print(exchange.getBook())
                exchange.show_trade_history()
                exchange.show_events_history()
            except ValueError as exc:
                print(f"Input error: {exc}")

        else:
            print("Invalid choice. Enter 1, 2, or 3.")
