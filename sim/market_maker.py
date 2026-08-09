"""Market makers — strategy, not engine.

Deliberately outside ``orderbook/``: these read top-of-book, decide where to
quote, and carry inventory and PnL. The engine has no opinion about any of it.

Both makers now read the book through O(1) top-of-book accessors rather than
flattening it, and inventory is reconciled from the trade tape by index rather
than by rescanning an event log.
"""

from __future__ import annotations

from typing import Optional, Set, Tuple

from orderbook import Side
from sim.exchange import Exchange

TICK_SIZE = 0.01
MIN_REQUOTE_TICKS = 3
MIN_REQUOTE_SIZE_DELTA = 3
MAX_QUOTE_AGE_REFRESHES = 50
INVENTORY_LIMIT_BUFFER = 0.8

DEFAULT_FAIR_VALUE = 100.0


class MarketMaker:
    """Symmetric quotes at mid +/- half a spread; cancels and reposts each refresh."""

    def __init__(self, exchange: Exchange, quote_size: int = 10):
        self.exchange = exchange
        self.quote_size = quote_size
        self._bid_id: Optional[int] = None
        self._ask_id: Optional[int] = None

    def _quote_prices(
        self, best_bid: Optional[float], best_ask: Optional[float]
    ) -> Tuple[float, float]:
        """Non-crossing bid/ask targets for the current top of book."""
        if best_bid is None and best_ask is None:
            return 99.0, 101.0

        if best_bid is None:
            bid = round(min(99.0, best_ask - 0.5), 2)
            if bid >= best_ask:
                bid = round(best_ask - TICK_SIZE, 2)
            return bid, round(best_ask + 0.5, 2)

        if best_ask is None:
            ask = round(max(101.0, best_bid + 0.5), 2)
            if ask <= best_bid:
                ask = round(best_bid + TICK_SIZE, 2)
            return round(best_bid - 0.5, 2), ask

        mid = (best_bid + best_ask) / 2
        bid = round(mid - 0.5, 2)
        ask = round(mid + 0.5, 2)
        if bid >= best_ask:
            bid = round(best_ask - TICK_SIZE, 2)
        if ask <= best_bid:
            ask = round(best_bid + TICK_SIZE, 2)
        if bid >= ask:
            bid = round(best_bid + TICK_SIZE, 2)
            ask = round(best_ask - TICK_SIZE, 2)
        return bid, ask

    def _post(self, side: Side, price: float) -> Optional[int]:
        result = self.exchange.submit_new(side, self.quote_size, price)
        return result.order.order_id if result.resting else None

    def refresh_quotes(self) -> Tuple[Optional[float], Optional[float]]:
        for order_id in (self._bid_id, self._ask_id):
            if order_id is not None:
                self.exchange.cancel(order_id)
        self._bid_id = self._ask_id = None

        book = self.exchange.book
        bid_price, ask_price = self._quote_prices(book.best_bid(), book.best_ask())

        self._bid_id = self._post(Side.BUY, bid_price)
        self._ask_id = self._post(Side.SELL, ask_price)

        return (
            bid_price if self._bid_id is not None else None,
            ask_price if self._ask_id is not None else None,
        )


class InventoryAwareMarketMaker:
    """Inventory-skewed maker.

    Shifts its reservation price against its position, sizes each side by the
    inventory ratio, pulls a side entirely near the position limit, and only
    reposts when a quote is far from target, badly sized, or stale.
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
        self.replaced_due_to_price = 0
        self.replaced_due_to_size = 0
        self.replaced_due_to_age = 0
        self.replaced_due_to_risk = 0

        self._bid_id: Optional[int] = None
        self._ask_id: Optional[int] = None
        self._bid_age = 0
        self._ask_age = 0
        self.current_bid: Optional[Tuple[float, int]] = None
        self.current_ask: Optional[Tuple[float, int]] = None

        self._my_order_ids: Set[int] = set()
        self._tape_index = 0

    # --- Valuation ---

    def fair_value(self) -> float:
        book = self.exchange.book
        best_bid, best_ask = book.best_bid(), book.best_ask()
        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2
        if best_bid is not None:
            return best_bid + self.base_spread / 2
        if best_ask is not None:
            return best_ask - self.base_spread / 2
        return DEFAULT_FAIR_VALUE

    @property
    def total_pnl(self) -> float:
        """Cash plus inventory marked at fair value."""
        return self.cash + self.inventory * self.fair_value()

    # --- Fill reconciliation ---

    def _process_fills(self) -> None:
        """Credit fills on our own orders from the tape, reading only new entries."""
        new_entries = self.exchange.trades_since(self._tape_index)
        self._tape_index += len(new_entries)

        for entry in new_entries:
            if entry.buy_order_id in self._my_order_ids:
                self.inventory += entry.quantity
                self.cash -= entry.price * entry.quantity
            if entry.sell_order_id in self._my_order_ids:
                self.inventory -= entry.quantity
                self.cash += entry.price * entry.quantity

        self.max_abs_inventory = max(self.max_abs_inventory, abs(self.inventory))

    def _prune_order_ids(self) -> None:
        """Forget ids that are neither resting nor a live quote.

        Safe only *after* :meth:`_process_fills` has drained the tape.
        """
        live = {self._bid_id, self._ask_id}
        self._my_order_ids = {
            order_id
            for order_id in self._my_order_ids
            if order_id in live or self.exchange.is_resting(order_id)
        }

    # --- Quoting ---

    def _clamp_size(self, size: int) -> int:
        return max(self.min_quote_size, min(self.max_quote_size, size))

    def _desired_quotes(self) -> Tuple[float, int, float, int]:
        book = self.exchange.book
        best_bid, best_ask = book.best_bid(), book.best_ask()

        reservation = self.fair_value() - self.inventory * self.inventory_risk_factor
        bid_price = round(reservation - self.base_spread / 2, 2)
        ask_price = round(reservation + self.base_spread / 2, 2)

        # Stay passive: never quote through the touch.
        if best_ask is not None and bid_price >= best_ask:
            bid_price = round(best_ask - TICK_SIZE, 2)
        if best_bid is not None and ask_price <= best_bid:
            ask_price = round(best_bid + TICK_SIZE, 2)
        if bid_price >= ask_price and best_bid is not None and best_ask is not None:
            bid_price = round(best_bid + TICK_SIZE, 2)
            ask_price = round(best_ask - TICK_SIZE, 2)

        ratio = self.inventory / self.max_inventory
        bid_size = self._clamp_size(int(round(self.base_quote_size * (1 - ratio))))
        ask_size = self._clamp_size(int(round(self.base_quote_size * (1 + ratio))))
        return bid_price, bid_size, ask_price, ask_size

    def _resting_quote(self, order_id: Optional[int]) -> Optional[Tuple[float, int]]:
        if order_id is None:
            return None
        order = self.exchange.book.get(order_id)
        if order is None:
            return None
        return order.price, order.remaining

    def _requote_reason(
        self,
        resting: Tuple[float, int],
        desired_price: float,
        desired_size: int,
        age: int,
    ) -> Optional[str]:
        if abs(resting[0] - desired_price) / TICK_SIZE >= MIN_REQUOTE_TICKS:
            return "price"
        if abs(resting[1] - desired_size) >= MIN_REQUOTE_SIZE_DELTA:
            return "size"
        if age >= MAX_QUOTE_AGE_REFRESHES:
            return "age"
        return None

    def _record_replacement(self, reason: str) -> None:
        self.quote_replacements += 1
        attr = f"replaced_due_to_{reason}"
        if hasattr(self, attr):
            setattr(self, attr, getattr(self, attr) + 1)

    def _side_is_risky(self, side: Side) -> bool:
        limit = self.max_inventory * INVENTORY_LIMIT_BUFFER
        if side is Side.BUY:
            return self.inventory >= limit
        return self.inventory <= -limit

    def _post(self, side: Side, price: float, size: int) -> Optional[int]:
        result = self.exchange.submit_new(side, size, price)
        self._my_order_ids.add(result.order.order_id)
        return result.order.order_id if result.resting else None

    def _update_side(
        self,
        side: Side,
        desired_price: float,
        desired_size: int,
        order_id: Optional[int],
        age: int,
        allowed: bool,
    ) -> Tuple[Optional[int], int, Optional[Tuple[float, int]]]:
        resting = self._resting_quote(order_id)

        if not allowed or self._side_is_risky(side):
            if resting is not None:
                self.exchange.cancel(order_id)
                self._record_replacement("risk")
            return None, 0, None

        if resting is None:
            new_id = self._post(side, desired_price, desired_size)
            self.quote_replacements += 1
            quote = (desired_price, desired_size) if new_id is not None else None
            return new_id, 0, quote

        reason = self._requote_reason(resting, desired_price, desired_size, age)
        if reason is None:
            self.quote_skips += 1
            return order_id, age + 1, resting

        self.exchange.cancel(order_id)
        new_id = self._post(side, desired_price, desired_size)
        self._record_replacement(reason)
        quote = (desired_price, desired_size) if new_id is not None else None
        return new_id, 0, quote

    def refresh_quotes(self) -> Tuple[Optional[float], Optional[float]]:
        self.quote_refreshes += 1
        self._process_fills()
        self._prune_order_ids()

        bid_price, bid_size, ask_price, ask_size = self._desired_quotes()

        self._bid_id, self._bid_age, self.current_bid = self._update_side(
            Side.BUY,
            bid_price,
            bid_size,
            self._bid_id,
            self._bid_age,
            allowed=self.inventory < self.max_inventory,
        )
        self._ask_id, self._ask_age, self.current_ask = self._update_side(
            Side.SELL,
            ask_price,
            ask_size,
            self._ask_id,
            self._ask_age,
            allowed=self.inventory > -self.max_inventory,
        )

        return (
            self.current_bid[0] if self.current_bid else None,
            self.current_ask[0] if self.current_ask else None,
        )

    def report(self) -> dict:
        return {
            "inventory": self.inventory,
            "max_abs_inventory": self.max_abs_inventory,
            "cash": round(self.cash, 2),
            "total_pnl": round(self.total_pnl, 2),
            "quote_refreshes": self.quote_refreshes,
            "quote_replacements": self.quote_replacements,
            "quote_skips": self.quote_skips,
            "replaced_price": self.replaced_due_to_price,
            "replaced_size": self.replaced_due_to_size,
            "replaced_age": self.replaced_due_to_age,
            "replaced_risk": self.replaced_due_to_risk,
        }


def create_market_maker(
    exchange: Exchange, mm_type: str = "simple", quote_size: int = 10
):
    if mm_type == "simple":
        return MarketMaker(exchange, quote_size=quote_size)
    if mm_type == "inventory":
        return InventoryAwareMarketMaker(
            exchange,
            base_quote_size=quote_size,
            max_inventory=max(quote_size * 10, 50),
        )
    raise ValueError(f"unknown mm_type {mm_type!r} (use 'simple' or 'inventory')")
