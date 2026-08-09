"""Cancellation: O(1) unlink, level collection, and the state it must leave behind."""

from __future__ import annotations

import random

import pytest

from orderbook import (
    BookIntegrityError,
    MatchingEngine,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Side,
)


# ----------------------------------------------------------------------
# Position within the queue
# ----------------------------------------------------------------------


def test_cancel_head_of_level(market):
    first = market.sell(1, 100.0).order
    second = market.sell(2, 100.0).order
    third = market.sell(3, 100.0).order

    cancelled = market.cancel(first.order_id)

    assert cancelled is first
    assert cancelled.status is OrderStatus.CANCELLED
    assert [o.order_id for o in market.book.orders(Side.SELL)] == [
        second.order_id,
        third.order_id,
    ]
    market.validate()


def test_cancel_middle_of_level(market):
    first = market.buy(1, 99.0).order
    middle = market.buy(2, 99.0).order
    last = market.buy(3, 99.0).order

    market.cancel(middle.order_id)

    assert [o.order_id for o in market.book.orders(Side.BUY)] == [
        first.order_id,
        last.order_id,
    ]
    market.validate()


def test_cancel_tail_of_level(market):
    first = market.buy(1, 99.0).order
    last = market.buy(3, 99.0).order

    market.cancel(last.order_id)

    assert [o.order_id for o in market.book.orders(Side.BUY)] == [first.order_id]
    market.validate()


def test_cancel_only_order_at_a_level(market):
    order = market.buy(5, 99.0).order
    market.cancel(order.order_id)

    assert len(market.book) == 0
    assert market.book.best_bid() is None
    assert market.book.level_count(Side.BUY) == 0
    market.validate()


def test_cancelling_does_not_disturb_neighbours(market):
    market.buy(5, 99.0)
    target = market.buy(5, 98.0).order
    market.buy(5, 97.0)

    market.cancel(target.order_id)

    assert [lv.price for lv in market.book.l2().bids] == [99.0, 97.0]
    market.validate()


# ----------------------------------------------------------------------
# Effect on derived state
# ----------------------------------------------------------------------


def test_cancel_updates_depth_and_level_aggregates(market):
    market.buy(10, 99.0)
    target = market.buy(15, 99.0).order

    market.cancel(target.order_id)

    assert market.book.bid_depth == 10
    assert market.book.l2().bids == [(99.0, 10, 1)]
    market.validate()


def test_cancel_reveals_the_next_best_price(market):
    top = market.buy(5, 100.0).order
    market.buy(5, 99.0)

    market.cancel(top.order_id)

    assert market.book.best_bid() == 99.0
    market.validate()


def test_cancel_empties_a_level_and_collects_it(market):
    order = market.sell(5, 101.0).order
    assert market.book.level_count(Side.SELL) == 1

    market.cancel(order.order_id)

    assert market.book.level_count(Side.SELL) == 0
    assert market.book.level_removes == 1
    market.validate()


def test_price_can_be_reused_after_its_level_is_collected(market):
    first = market.sell(5, 101.0).order
    market.cancel(first.order_id)
    market.sell(3, 101.0)

    assert market.book.best_ask() == 101.0
    assert market.book.ask_depth == 3
    market.validate()


# ----------------------------------------------------------------------
# Cancels that should not happen
# ----------------------------------------------------------------------


def test_cancelling_an_unknown_id_returns_none(market):
    assert market.cancel(999) is None


def test_cancel_is_not_idempotent_but_is_safe(market):
    order = market.buy(5, 99.0).order
    assert market.cancel(order.order_id) is order
    assert market.cancel(order.order_id) is None
    market.validate()


def test_cannot_cancel_a_fully_filled_order(market):
    resting = market.sell(5, 100.0).order
    market.buy(5, 100.0)

    assert resting.status is OrderStatus.FILLED
    assert market.cancel(resting.order_id) is None
    market.validate()


def test_cancel_after_partial_fill_removes_only_the_remainder(market):
    resting = market.sell(10, 100.0).order
    market.buy(4, 100.0)

    cancelled = market.cancel(resting.order_id)

    assert cancelled is resting
    assert cancelled.remaining == 6
    assert cancelled.filled == 4
    assert market.book.ask_depth == 0
    market.validate()


def test_filling_an_absent_order_raises(book):
    with pytest.raises(BookIntegrityError, match="not on the book"):
        book.fill(1, 5)


def test_overfilling_a_resting_order_raises(book):
    book.add(Order(1, Side.SELL, OrderType.LIMIT, 5, 100.0))
    with pytest.raises(BookIntegrityError, match="invalid fill"):
        book.fill(1, 6)


# ----------------------------------------------------------------------
# Under load
# ----------------------------------------------------------------------


def test_cancel_never_touches_unrelated_levels(market):
    """A cancel at one price must leave every other level byte-identical."""
    for price in (97.0, 98.0, 99.0):
        market.buy(5, price)
        market.buy(7, price)

    before = {lv.price: lv for lv in market.book.l2().bids}
    victim = next(o for o in market.book.orders(Side.BUY) if o.price == 98.0)
    market.cancel(victim.order_id)
    after = {lv.price: lv for lv in market.book.l2().bids}

    assert after[97.0] == before[97.0]
    assert after[99.0] == before[99.0]
    assert after[98.0] == (98.0, 7, 1)
    market.validate()


def test_book_survives_heavy_submit_cancel_churn():
    rng = random.Random(1234)
    book = OrderBook()
    engine = MatchingEngine()
    live = []
    next_id = 1

    for _ in range(4000):
        if live and rng.random() < 0.45:
            order_id = live.pop(rng.randrange(len(live)))
            engine.cancel(order_id, book)
            continue

        side = rng.choice([Side.BUY, Side.SELL])
        price = round(rng.uniform(98.0, 102.0), 2)
        order = Order(next_id, side, OrderType.LIMIT, rng.randint(1, 20), price)
        next_id += 1
        result = engine.submit(order, book)
        if result.resting:
            live.append(order.order_id)

    book.validate()

    # Cancel everything that is still resting; the book must end truly empty.
    for order_id in list(book.orders_by_id):
        engine.cancel(order_id, book)

    assert len(book) == 0
    assert book.bid_depth == 0
    assert book.ask_depth == 0
    assert book.level_count(Side.BUY) == 0
    assert book.level_count(Side.SELL) == 0
    assert book.level_creates == book.level_removes
    book.validate()
