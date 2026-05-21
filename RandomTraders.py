"""
Simple autonomous participant: random limit orders submitted into Exchange.
Run this file directly for a tiny multi-step simulation loop.
"""

from __future__ import annotations

import random
from typing import Optional

from exchange import Exchange
from timing import timed

MM_EVERY_N_ORDERS = 5  # refresh quotes every N trader steps (matches benchmark default)


class MarketMaker:
    """Keeps symmetric limit quotes at midpoint ± 0.5; refreshes by cancelling prior ids."""

    def __init__(self, exchange: Exchange, quote_size: int = 10):
        self.exchange = exchange
        self.quote_size = quote_size
        self._buy_quote_id = None
        self._sell_quote_id = None

    def _is_resting(self, order_id, side):
        book = (
            self.exchange.order_book.getBuyOrders()
            if side == "buy"
            else self.exchange.order_book.getSellOrders()
        )
        return any(o.order_id == order_id for o in book)

    def _quote_prices(self, buys, sells):
        """Non-crossing buy/sell targets for current book state."""
        if not buys and not sells:
            return 99.0, 101.0
        if not buys:
            best_ask = sells[0].price
            buy_price = round(min(99.0, best_ask - 0.5), 2)
            sell_price = round(best_ask + 0.5, 2)
            if buy_price >= best_ask:
                buy_price = round(best_ask - 0.01, 2)
            return buy_price, sell_price
        if not sells:
            best_bid = buys[0].price
            buy_price = round(best_bid - 0.5, 2)
            sell_price = round(max(101.0, best_bid + 0.5), 2)
            if sell_price <= best_bid:
                sell_price = round(best_bid + 0.01, 2)
            return buy_price, sell_price

        best_bid = buys[0].price
        best_ask = sells[0].price
        midpoint = (best_bid + best_ask) / 2
        buy_price = round(midpoint - 0.5, 2)
        sell_price = round(midpoint + 0.5, 2)
        # Stay passive: buy below ask, sell above bid
        if buy_price >= best_ask:
            buy_price = round(best_ask - 0.01, 2)
        if sell_price <= best_bid:
            sell_price = round(best_bid + 0.01, 2)
        if buy_price >= sell_price:
            buy_price = round(best_bid + 0.01, 2)
            sell_price = round(best_ask - 0.01, 2)
        return buy_price, sell_price

    def _submit_quote(self, side, price):
        order = self.exchange.manual_order(side, self.quote_size, price, "limit")
        self.exchange.submit_order(order)
        if self._is_resting(order.order_id, side):
            return price, order.order_id
        return price, None

    def refresh_quotes(self):
        with timed("mm_refresh"):
            if self._buy_quote_id is not None:
                self.exchange.cancel_order(self._buy_quote_id)
            if self._sell_quote_id is not None:
                self.exchange.cancel_order(self._sell_quote_id)
            self._buy_quote_id = None
            self._sell_quote_id = None

            buys = self.exchange.order_book.getBuyOrders()
            sells = self.exchange.order_book.getSellOrders()
            buy_price, sell_price = self._quote_prices(buys, sells)

            _, buy_id = self._submit_quote("buy", buy_price)
            _, sell_id = self._submit_quote("sell", sell_price)
            self._buy_quote_id = buy_id
            self._sell_quote_id = sell_id

            return (
                buy_price if buy_id is not None else None,
                sell_price if sell_id is not None else None,
            )


MIN_REQUOTE_TICKS = 3
MIN_REQUOTE_SIZE_DELTA = 3
MAX_QUOTE_AGE_REFRESHES = 50
INVENTORY_LIMIT_BUFFER = 0.8
TICK_SIZE = 0.01


class InventoryAwareMarketMaker:
    """
    Inventory-skewed market maker: shifts reservation price and quote sizes from
    inventory, and only cancels/reposts when quotes are stale, far from target, or risky.
    """

    def __init__(
        self,
        exchange: Exchange,
        base_quote_size: int = 10,
        base_spread: float = 1.0,
        inventory_risk_factor: float = 0.05,
        max_inventory: int = 100,
    ):
        self.exchange = exchange
        self.base_quote_size = base_quote_size
        self.base_spread = base_spread
        self.inventory_risk_factor = inventory_risk_factor
        self.max_inventory = max_inventory
        self.min_quote_size = 1
        self.max_quote_size = base_quote_size * 2

        self.inventory = 0
        self.cash = 0.0
        self.max_abs_inventory = 0

        self.quote_refreshes = 0
        self.quote_replacements = 0
        self.quote_skips = 0
        self.quote_kept_due_to_threshold = 0
        self.quote_replaced_due_to_price = 0
        self.quote_replaced_due_to_size = 0
        self.quote_replaced_due_to_age = 0
        self.quote_replaced_due_to_risk = 0

        self._buy_quote_id = None
        self._sell_quote_id = None
        self.active_bid_age_refreshes = 0
        self.active_ask_age_refreshes = 0
        self.current_bid_quote = None  # (price, size) or None
        self.current_ask_quote = None
        self._last_processed_event_id = 0
        self.last_refresh_mm_side_replaced = "none"
        self.last_refresh_replacement_reason = "none"

    @property
    def total_pnl(self) -> float:
        fair = self._fair_value(
            self.exchange.order_book.getBuyOrders(),
            self.exchange.order_book.getSellOrders(),
        )
        return self.cash + self.inventory * fair

    def _fair_value(self, buys, sells) -> float:
        if buys and sells:
            return (buys[0].price + sells[0].price) / 2
        if buys:
            return buys[0].price + self.base_spread / 2
        if sells:
            return sells[0].price - self.base_spread / 2
        return 100.0

    def _clamp_passive_prices(self, buy_price: float, sell_price: float, buys, sells):
        if sells:
            best_ask = sells[0].price
            if buy_price >= best_ask:
                buy_price = round(best_ask - 0.01, 2)
        if buys:
            best_bid = buys[0].price
            if sell_price <= best_bid:
                sell_price = round(best_bid + 0.01, 2)
        if buy_price >= sell_price:
            if buys and sells:
                buy_price = round(buys[0].price + 0.01, 2)
                sell_price = round(sells[0].price - 0.01, 2)
        return buy_price, sell_price

    def _desired_quotes(self, buys, sells):
        fair_value = self._fair_value(buys, sells)
        inventory_skew = self.inventory * self.inventory_risk_factor
        reservation = fair_value - inventory_skew
        bid_price = round(reservation - self.base_spread / 2, 2)
        ask_price = round(reservation + self.base_spread / 2, 2)
        bid_price, ask_price = self._clamp_passive_prices(
            bid_price, ask_price, buys, sells
        )

        ratio = self.inventory / self.max_inventory
        bid_size = self._clamp_size(
            int(round(self.base_quote_size * (1 - ratio)))
        )
        ask_size = self._clamp_size(
            int(round(self.base_quote_size * (1 + ratio)))
        )
        return bid_price, bid_size, ask_price, ask_size, fair_value

    def _clamp_size(self, size: int) -> int:
        return max(self.min_quote_size, min(self.max_quote_size, size))

    def _resting_quote(self, order_id):
        if order_id is None:
            return None
        node = self.exchange.order_book.orders_by_id.get(order_id)
        if node is None:
            return None
        o = node.order
        return (o.price, o.quantity)

    def _process_fills(self):
        for ev in self.exchange.event_history:
            if ev.event_id <= self._last_processed_event_id:
                continue
            self._last_processed_event_id = ev.event_id
            if ev.type != "TRADE_EXECUTED":
                continue
            data = ev.data
            price = data["price"]
            qty = data["quantity"]
            buy_id = data.get("buy_order_id")
            sell_id = data.get("sell_order_id")
            if buy_id == self._buy_quote_id:
                self.inventory += qty
                self.cash -= price * qty
            if sell_id == self._sell_quote_id:
                self.inventory -= qty
                self.cash += price * qty
        self.max_abs_inventory = max(
            self.max_abs_inventory, abs(self.inventory)
        )

    def _cancel_quote(self, order_id):
        if order_id is not None:
            self.exchange.cancel_order(order_id)

    def _submit_quote(self, side: str, price: float, size: int):
        order = self.exchange.manual_order(side, size, price, "limit")
        self.exchange.submit_order(order)
        if order.order_id in self.exchange.order_book.orders_by_id:
            return price, size, order.order_id
        return price, size, None

    def _side_risky(self, side: str) -> bool:
        limit = self.max_inventory * INVENTORY_LIMIT_BUFFER
        if side == "buy":
            return self.inventory >= limit
        return self.inventory <= -limit

    def _requote_reason(
        self,
        resting: tuple[float, int],
        desired_price: float,
        desired_size: int,
        quote_age: int,
    ) -> Optional[str]:
        price_delta_ticks = abs(resting[0] - desired_price) / TICK_SIZE
        if price_delta_ticks >= MIN_REQUOTE_TICKS:
            return "price"
        if abs(resting[1] - desired_size) >= MIN_REQUOTE_SIZE_DELTA:
            return "size"
        if quote_age >= MAX_QUOTE_AGE_REFRESHES:
            return "age"
        return None

    def _record_replacement(self, reason: str) -> None:
        self.quote_replacements += 1
        if reason == "price":
            self.quote_replaced_due_to_price += 1
        elif reason == "size":
            self.quote_replaced_due_to_size += 1
        elif reason == "age":
            self.quote_replaced_due_to_age += 1
        elif reason == "risk":
            self.quote_replaced_due_to_risk += 1

    def _clear_side_state(self, side: str) -> None:
        if side == "buy":
            self.current_bid_quote = None
        else:
            self.current_ask_quote = None

    def _set_side_quote(self, side: str, quote: Optional[tuple]) -> None:
        if side == "buy":
            self.current_bid_quote = quote
        else:
            self.current_ask_quote = quote

    def _update_side(
        self,
        side: str,
        desired_price: float,
        desired_size: int,
        order_id,
        quote_age: int,
        place_allowed: bool,
    ) -> tuple[Optional[int], int, bool, str]:
        resting = self._resting_quote(order_id)
        risky = self._side_risky(side)

        if not place_allowed:
            if order_id is not None and resting is not None:
                self._cancel_quote(order_id)
                self._record_replacement("risk")
            self._clear_side_state(side)
            return None, 0, order_id is not None, "risk"

        if risky:
            if order_id is not None and resting is not None:
                self._cancel_quote(order_id)
                self._record_replacement("risk")
            self._clear_side_state(side)
            return None, 0, order_id is not None, "risk"

        if resting is None:
            reason = "filled_cancelled" if order_id is not None else "missing"
            price, size, new_id = self._submit_quote(side, desired_price, desired_size)
            self.quote_replacements += 1
            quote = (price, size) if new_id is not None else None
            self._set_side_quote(side, quote)
            return new_id, 0, True, reason

        reason = self._requote_reason(resting, desired_price, desired_size, quote_age)
        if reason is None:
            self.quote_skips += 1
            self.quote_kept_due_to_threshold += 1
            self._set_side_quote(side, resting)
            return order_id, quote_age + 1, False, "none"

        if order_id is not None:
            self._cancel_quote(order_id)
        price, size, new_id = self._submit_quote(side, desired_price, desired_size)
        self._record_replacement(reason)
        quote = (price, size) if new_id is not None else None
        self._set_side_quote(side, quote)
        return new_id, 0, True, reason

    def refresh_quotes(self):
        with timed("mm_refresh"):
            self.quote_refreshes += 1
            self._process_fills()

            buys = self.exchange.order_book.getBuyOrders()
            sells = self.exchange.order_book.getSellOrders()
            bid_price, bid_size, ask_price, ask_size, _ = self._desired_quotes(
                buys, sells
            )

            place_bid = self.inventory < self.max_inventory
            place_ask = self.inventory > -self.max_inventory

            (
                self._buy_quote_id,
                self.active_bid_age_refreshes,
                bid_replaced,
                bid_reason,
            ) = self._update_side(
                "buy",
                bid_price,
                bid_size,
                self._buy_quote_id,
                self.active_bid_age_refreshes,
                place_bid,
            )
            (
                self._sell_quote_id,
                self.active_ask_age_refreshes,
                ask_replaced,
                ask_reason,
            ) = self._update_side(
                "sell",
                ask_price,
                ask_size,
                self._sell_quote_id,
                self.active_ask_age_refreshes,
                place_ask,
            )

            if bid_replaced and ask_replaced:
                self.last_refresh_mm_side_replaced = "both"
            elif bid_replaced:
                self.last_refresh_mm_side_replaced = "bid"
            elif ask_replaced:
                self.last_refresh_mm_side_replaced = "ask"
            else:
                self.last_refresh_mm_side_replaced = "none"

            if bid_replaced and bid_reason != "none":
                self.last_refresh_replacement_reason = bid_reason
            elif ask_replaced and ask_reason != "none":
                self.last_refresh_replacement_reason = ask_reason
            else:
                self.last_refresh_replacement_reason = "none"

            return (
                self.current_bid_quote[0] if self.current_bid_quote else None,
                self.current_ask_quote[0] if self.current_ask_quote else None,
            )


def create_market_maker(
    exchange: Exchange,
    mm_type: str = "simple",
    quote_size: int = 10,
):
    """Factory: ``simple`` (default) or ``inventory`` market maker."""
    if mm_type == "inventory":
        return InventoryAwareMarketMaker(
            exchange,
            base_quote_size=quote_size,
            max_inventory=max(quote_size * 10, 50),
        )
    if mm_type == "simple":
        return MarketMaker(exchange, quote_size=quote_size)
    raise ValueError(f"Unknown mm_type: {mm_type!r} (use 'simple' or 'inventory')")


class RandomTrader:
    """Random limit orders near a base price; submits via Exchange.submit_order."""

    def __init__(
        self,
        exchange: Exchange,
        base_price: float = 100.0,
        jitter: float = 1.0,
    ):
        self.exchange = exchange
        self.base_price = base_price
        self.jitter = jitter

    def submit_random_limit(self):
        side = random.choice(["buy", "sell"])
        quantity = random.randint(1, 40)
        delta = random.uniform(-self.jitter, self.jitter)
        price = round(self.base_price + delta, 2)
        order = self.exchange.manual_order(side, quantity, price, "limit")
        return self.exchange.submit_order(order)


def _new_simulation():
    exchange = Exchange()
    mm = MarketMaker(exchange, quote_size=10)
    traders = [
        RandomTrader(exchange, base_price=99.0, jitter=1.0),
        RandomTrader(exchange, base_price=100.0, jitter=1.0),
        RandomTrader(exchange, base_price=101.0, jitter=1.0),
    ]
    return exchange, mm, traders


def _print_step_summary(step, trader_result, mm_result, exchange, trader_base=None):
    base = f" (trader base {trader_base})" if trader_base is not None else ""
    print(f"\n--- Step {step}{base} ---")
    print("submit_order:", trader_result)
    print("market_maker:", mm_result)
    exchange.show_market_summary()
    exchange.show_recent_trades(5)
    exchange.show_recent_events(10)


def run_random_sim(num_orders: int = 15, print_every: int = 0):
    """Short demo run. print_every=0 prints only final metrics."""
    exchange, mm, traders = _new_simulation()
    if print_every <= 0:
        print_every = num_orders

    print("RandomTrader simulation started (3 traders @ base 99 / 100 / 101, jitter ±1).")
    for i in range(1, num_orders + 1):
        trader = traders[(i - 1) % len(traders)]
        result = trader.submit_random_limit()
        mm_result = (
            mm.refresh_quotes()
            if i % MM_EVERY_N_ORDERS == 0
            else None
        )
        if i % print_every == 0 or i == num_orders:
            _print_step_summary(i, result, mm_result, exchange, trader.base_price)

    exchange.show_metrics()


def run_continuous_sim(steps: int = 1000, print_every: int = 25):
    """Long synchronous run with periodic summaries."""
    exchange, mm, traders = _new_simulation()
    print(
        f"Continuous simulation started ({steps} steps, summary every {print_every})."
    )

    for i in range(1, steps + 1):
        trader = traders[(i - 1) % len(traders)]
        result = trader.submit_random_limit()
        mm_result = (
            mm.refresh_quotes()
            if i % MM_EVERY_N_ORDERS == 0
            else None
        )
        if print_every > 0 and (i % print_every == 0 or i == steps):
            _print_step_summary(i, result, mm_result, exchange, trader.base_price)

    print("\n=== Final simulation report ===")
    exchange.show_market_summary()
    exchange.show_metrics()


if __name__ == "__main__":
    random.seed()
    run_continuous_sim(steps=200, print_every=25)
