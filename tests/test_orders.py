"""Order validation, resting behaviour, and the L2 book view."""

from __future__ import annotations

import pytest

from orderbook import (
    BookIntegrityError,
    Order,
    OrderStatus,
    OrderType,
    Side,
)


# ----------------------------------------------------------------------
# Order construction
# ----------------------------------------------------------------------


def test_limit_order_defaults():
    order = Order(1, Side.BUY, OrderType.LIMIT, 10, 100.0)
    assert order.remaining == 10
    assert order.filled == 0
    assert order.status is OrderStatus.NEW
    assert order.sequence is None
    assert order.is_limit


def test_side_and_type_accept_plain_strings():
    order = Order(1, "buy", "limit", 5, 100.0)
    assert order.side is Side.BUY
    assert order.order_type is OrderType.LIMIT
    assert order.side == "buy"  # str enum stays JSON-friendly


def test_side_opposite():
    assert Side.BUY.opposite is Side.SELL
    assert Side.SELL.opposite is Side.BUY


def test_market_order_never_carries_a_price():
    order = Order(1, Side.BUY, OrderType.MARKET, 10, price=123.0)
    assert order.price is None


@pytest.mark.parametrize("quantity", [0, -5])
def test_non_positive_quantity_rejected(quantity):
    with pytest.raises(ValueError, match="positive"):
        Order(1, Side.BUY, OrderType.LIMIT, quantity, 100.0)


@pytest.mark.parametrize("quantity", [1.5, "10", True])
def test_non_integer_quantity_rejected(quantity):
    with pytest.raises(ValueError):
        Order(1, Side.BUY, OrderType.LIMIT, quantity, 100.0)


def test_limit_order_requires_a_price():
    with pytest.raises(ValueError, match="require a price"):
        Order(1, Side.BUY, OrderType.LIMIT, 10, None)


def test_non_positive_price_rejected():
    with pytest.raises(ValueError, match="price must be positive"):
        Order(1, Side.SELL, OrderType.LIMIT, 10, 0.0)


def test_unknown_side_rejected():
    with pytest.raises(ValueError):
        Order(1, "sideways", OrderType.LIMIT, 10, 100.0)


def test_crosses():
    buy = Order(1, Side.BUY, OrderType.LIMIT, 10, 100.0)
    assert buy.crosses(99.0)
    assert buy.crosses(100.0)
    assert not buy.crosses(100.01)

    sell = Order(2, Side.SELL, OrderType.LIMIT, 10, 100.0)
    assert sell.crosses(101.0)
    assert sell.crosses(100.0)
    assert not sell.crosses(99.99)

    mkt = Order(3, Side.BUY, OrderType.MARKET, 10)
    assert mkt.crosses(1e9)


# ----------------------------------------------------------------------
# Resting
# ----------------------------------------------------------------------


def test_resting_order_gets_sequence_and_status(market):
    result = market.buy(10, 99.0)
    order = result.order
    assert result.resting
    assert order.status is OrderStatus.RESTING
    assert order.sequence == 1
    assert market.book.contains(order.order_id)


def test_sequence_increases_with_arrival(market):
    first = market.buy(1, 99.0).order
    second = market.buy(1, 98.0).order
    assert second.sequence > first.sequence


def test_book_rejects_duplicate_order_id(book):
    order = Order(7, Side.BUY, OrderType.LIMIT, 5, 100.0)
    book.add(order)
    with pytest.raises(BookIntegrityError, match="duplicate order id"):
        book.add(Order(7, Side.BUY, OrderType.LIMIT, 5, 100.0))


def test_book_refuses_to_rest_a_priceless_order(book):
    order = Order(1, Side.BUY, OrderType.MARKET, 5)
    with pytest.raises(BookIntegrityError, match="without a price"):
        book.add(order)


# ----------------------------------------------------------------------
# Top of book
# ----------------------------------------------------------------------


def test_empty_book_has_no_top(book):
    assert book.best_bid() is None
    assert book.best_ask() is None
    assert book.spread() is None
    assert book.mid_price() is None
    assert len(book) == 0


def test_best_prices_track_insertion(market):
    market.buy(5, 99.0)
    market.buy(5, 99.5)
    market.sell(5, 101.0)
    market.sell(5, 100.5)

    assert market.book.best_bid() == 99.5
    assert market.book.best_ask() == 100.5
    assert market.book.spread() == pytest.approx(1.0)
    assert market.book.mid_price() == pytest.approx(100.0)
    market.validate()


def test_depth_totals_are_incremental(market):
    market.buy(10, 99.0)
    market.buy(20, 98.0)
    market.sell(7, 101.0)

    assert market.book.bid_depth == 30
    assert market.book.ask_depth == 7
    assert market.book.depth_at(Side.BUY, 99.0) == 10
    assert market.book.depth_at(Side.BUY, 97.0) == 0
    market.validate()


# ----------------------------------------------------------------------
# L2 aggregation
# ----------------------------------------------------------------------


def test_l2_aggregates_orders_at_a_price(market):
    market.buy(5, 99.0)
    market.buy(7, 99.0)
    market.buy(3, 98.0)
    market.sell(4, 101.0)

    snapshot = market.book.l2()
    assert snapshot.bids == [(99.0, 12, 2), (98.0, 3, 1)]
    assert snapshot.asks == [(101.0, 4, 1)]
    assert snapshot.best_bid == 99.0
    assert snapshot.best_ask == 101.0


def test_l2_is_best_first_on_both_sides(market):
    for price in (97.0, 99.0, 98.0):
        market.buy(1, price)
    for price in (103.0, 101.0, 102.0):
        market.sell(1, price)

    snapshot = market.book.l2()
    assert [lv.price for lv in snapshot.bids] == [99.0, 98.0, 97.0]
    assert [lv.price for lv in snapshot.asks] == [101.0, 102.0, 103.0]


def test_l2_respects_depth_limit(market):
    for i in range(10):
        market.buy(1, 99.0 - i)
        market.sell(1, 101.0 + i)

    snapshot = market.book.l2(depth=3)
    assert len(snapshot.bids) == 3
    assert len(snapshot.asks) == 3
    assert [lv.price for lv in snapshot.bids] == [99.0, 98.0, 97.0]


def test_l2_on_empty_book(book):
    snapshot = book.l2()
    assert snapshot.bids == []
    assert snapshot.asks == []
    assert snapshot.best_bid is None


def test_level_count_tracks_garbage_collection(market):
    market.buy(1, 99.0)
    market.buy(1, 98.0)
    assert market.book.level_count(Side.BUY) == 2

    market.cancel(1)
    assert market.book.level_count(Side.BUY) == 1
    market.validate()


# ----------------------------------------------------------------------
# Ordering within a level
# ----------------------------------------------------------------------


def test_orders_iterate_in_price_time_priority(market):
    a = market.buy(1, 99.0).order
    b = market.buy(1, 100.0).order
    c = market.buy(1, 99.0).order

    ids = [o.order_id for o in market.book.orders(Side.BUY)]
    assert ids == [b.order_id, a.order_id, c.order_id]


def test_ask_side_iterates_lowest_first(market):
    high = market.sell(1, 102.0).order
    low = market.sell(1, 101.0).order

    ids = [o.order_id for o in market.book.orders(Side.SELL)]
    assert ids == [low.order_id, high.order_id]


def test_validate_catches_a_corrupted_depth_cache(market):
    market.buy(10, 99.0)
    market.book.bid_depth += 1
    with pytest.raises(BookIntegrityError, match="depth cache mismatch"):
        market.validate()
