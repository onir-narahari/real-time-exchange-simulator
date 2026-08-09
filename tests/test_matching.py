"""Matching semantics: price-time priority, sweeps, partial fills, market orders."""

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
# No match
# ----------------------------------------------------------------------


def test_non_crossing_limit_rests(market):
    market.sell(10, 101.0)
    result = market.buy(10, 100.0)

    assert result.trades == []
    assert result.resting
    assert result.status is OrderStatus.RESTING
    assert market.book.best_bid() == 100.0
    market.validate()


def test_limit_one_tick_away_does_not_trade(market):
    market.sell(10, 100.01)
    result = market.buy(10, 100.0)
    assert result.trades == []
    market.validate()


def test_first_order_into_an_empty_book_rests(market):
    result = market.buy(10, 100.0)
    assert result.trades == []
    assert result.resting


# ----------------------------------------------------------------------
# Basic execution
# ----------------------------------------------------------------------


def test_exact_match_clears_both_orders(market):
    resting = market.sell(10, 100.0).order
    result = market.buy(10, 100.0)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.price == 100.0
    assert trade.quantity == 10
    assert trade.aggressor_side is Side.BUY
    assert trade.resting_order_id == resting.order_id

    assert result.status is OrderStatus.FILLED
    assert not result.resting
    assert resting.status is OrderStatus.FILLED
    assert len(market.book) == 0
    assert market.book.best_ask() is None
    market.validate()


def test_trade_prints_at_the_resting_price(market):
    """An aggressive buy gets price improvement, not its own limit."""
    market.sell(10, 100.0)
    result = market.buy(10, 105.0)

    assert result.trades[0].price == 100.0
    assert result.average_price == pytest.approx(100.0)


def test_aggressor_side_is_recorded(market):
    market.buy(10, 100.0)
    result = market.sell(10, 100.0)
    assert result.trades[0].aggressor_side is Side.SELL


def test_trade_buy_sell_ids_follow_the_aggressor(market):
    resting_ask = market.sell(5, 100.0).order
    aggressive_bid = market.buy(5, 100.0)
    trade = aggressive_bid.trades[0]
    assert trade.buy_order_id == aggressive_bid.order.order_id
    assert trade.sell_order_id == resting_ask.order_id


def test_trade_ids_are_sequential(market):
    market.sell(1, 100.0)
    market.sell(1, 101.0)
    result = market.buy(2, 101.0)
    assert [t.trade_id for t in result.trades] == [1, 2]


# ----------------------------------------------------------------------
# Partial fills
# ----------------------------------------------------------------------


def test_incoming_larger_than_resting_rests_the_remainder(market):
    market.sell(4, 100.0)
    result = market.buy(10, 100.0)

    assert result.filled_quantity == 4
    assert result.order.remaining == 6
    assert result.resting
    assert result.status is OrderStatus.PARTIALLY_FILLED
    assert market.book.best_bid() == 100.0
    assert market.book.bid_depth == 6
    market.validate()


def test_resting_larger_than_incoming_keeps_its_place(market):
    resting = market.sell(10, 100.0).order
    market.buy(4, 100.0)

    assert resting.remaining == 6
    assert resting.status is OrderStatus.PARTIALLY_FILLED
    assert market.book.best_ask() == 100.0
    assert market.book.ask_depth == 6
    assert market.book.l2().asks == [(100.0, 6, 1)]
    market.validate()


def test_partial_fill_does_not_lose_time_priority(market):
    first = market.sell(10, 100.0).order
    second = market.sell(10, 100.0).order

    market.buy(4, 100.0)  # eats into `first` only

    assert [o.order_id for o in market.book.orders(Side.SELL)] == [
        first.order_id,
        second.order_id,
    ]


# ----------------------------------------------------------------------
# Price-time priority
# ----------------------------------------------------------------------


def test_best_price_fills_before_worse_price(market):
    worse = market.sell(5, 101.0).order
    better = market.sell(5, 100.0).order

    result = market.buy(5, 101.0)

    assert result.trades[0].resting_order_id == better.order_id
    assert market.book.contains(worse.order_id)
    market.validate()


def test_fifo_within_a_price_level(market):
    first = market.sell(5, 100.0).order
    second = market.sell(5, 100.0).order
    third = market.sell(5, 100.0).order

    result = market.buy(15, 100.0)
    hit = [t.resting_order_id for t in result.trades]
    assert hit == [first.order_id, second.order_id, third.order_id]
    market.validate()


def test_sweep_walks_levels_in_price_order(market):
    market.sell(5, 100.0)
    market.sell(5, 101.0)
    market.sell(5, 102.0)

    result = market.buy(15, 102.0)

    assert [t.price for t in result.trades] == [100.0, 101.0, 102.0]
    assert result.filled_quantity == 15
    assert result.traded_notional == pytest.approx(5 * (100.0 + 101.0 + 102.0))
    assert result.average_price == pytest.approx(101.0)
    assert market.book.best_ask() is None
    market.validate()


def test_sweep_stops_at_the_limit_price(market):
    market.sell(5, 100.0)
    market.sell(5, 101.0)
    market.sell(5, 102.0)

    result = market.buy(15, 101.0)

    assert result.filled_quantity == 10
    assert result.order.remaining == 5
    assert result.resting
    assert market.book.best_ask() == 102.0
    assert market.book.best_bid() == 101.0
    market.validate()


def test_sell_sweep_walks_bids_high_to_low(market):
    market.buy(5, 100.0)
    market.buy(5, 99.0)
    market.buy(5, 98.0)

    result = market.sell(15, 98.0)
    assert [t.price for t in result.trades] == [100.0, 99.0, 98.0]
    market.validate()


# ----------------------------------------------------------------------
# Market orders
# ----------------------------------------------------------------------


def test_market_buy_sweeps_regardless_of_price(market):
    market.sell(5, 100.0)
    market.sell(5, 500.0)

    result = market.market(Side.BUY, 10)

    assert [t.price for t in result.trades] == [100.0, 500.0]
    assert result.status is OrderStatus.FILLED
    market.validate()


def test_market_order_never_rests(market):
    market.sell(4, 100.0)
    result = market.market(Side.BUY, 10)

    assert result.filled_quantity == 4
    assert result.order.remaining == 6
    assert not result.resting
    assert result.status is OrderStatus.EXPIRED
    assert market.book.best_bid() is None
    assert len(market.book) == 0
    market.validate()


def test_market_order_against_empty_book_expires(market):
    result = market.market(Side.BUY, 10)
    assert result.trades == []
    assert result.status is OrderStatus.EXPIRED
    assert len(market.book) == 0


def test_market_sell_hits_the_bid(market):
    market.buy(10, 99.0)
    result = market.market(Side.SELL, 10)
    assert result.trades[0].price == 99.0
    assert result.status is OrderStatus.FILLED
    market.validate()


# ----------------------------------------------------------------------
# Empty price levels
# ----------------------------------------------------------------------


def test_level_is_collected_when_fully_traded(market):
    market.sell(5, 100.0)
    assert market.book.level_count(Side.SELL) == 1

    market.buy(5, 100.0)

    assert market.book.level_count(Side.SELL) == 0
    assert market.book.level_removes == 1
    assert market.book.l2().asks == []
    market.validate()


def test_partially_traded_level_is_kept(market):
    market.sell(10, 100.0)
    market.buy(4, 100.0)

    assert market.book.level_count(Side.SELL) == 1
    assert market.book.level_removes == 0
    assert market.book.l2().asks == [(100.0, 6, 1)]
    market.validate()


def test_level_with_a_second_order_survives_a_full_fill(market):
    first = market.sell(5, 100.0).order
    second = market.sell(5, 100.0).order

    market.buy(5, 100.0)  # consumes `first` entirely

    assert not market.book.contains(first.order_id)
    assert market.book.contains(second.order_id)
    assert market.book.level_count(Side.SELL) == 1
    assert market.book.level_removes == 0
    market.validate()


def test_sweep_collects_every_level_it_empties(market):
    for price in (100.0, 101.0, 102.0):
        market.sell(5, price)
    assert market.book.level_count(Side.SELL) == 3

    market.buy(15, 102.0)

    assert market.book.level_count(Side.SELL) == 0
    assert market.book.level_removes == 3
    assert market.book.ask_depth == 0
    market.validate()


def test_price_is_reusable_after_being_traded_out(market):
    market.sell(5, 100.0)
    market.buy(5, 100.0)
    assert market.book.best_ask() is None

    market.sell(3, 100.0)

    assert market.book.best_ask() == 100.0
    assert market.book.l2().asks == [(100.0, 3, 1)]
    market.validate()


def test_no_empty_level_is_ever_left_behind(market):
    """Sweep the whole book away and assert nothing phantom remains."""
    for price in (99.0, 99.5, 100.0):
        market.buy(4, price)
    for price in (101.0, 101.5, 102.0):
        market.sell(4, price)

    market.market(Side.BUY, 12)
    market.market(Side.SELL, 12)

    assert len(market.book) == 0
    assert market.book.level_count(Side.BUY) == 0
    assert market.book.level_count(Side.SELL) == 0
    assert market.book.l2().bids == []
    assert market.book.l2().asks == []
    assert market.book.level_creates == market.book.level_removes
    market.validate()


# ----------------------------------------------------------------------
# Crossed and locked books
# ----------------------------------------------------------------------


def test_a_crossing_limit_trades_instead_of_resting(market):
    market.sell(5, 100.0)
    result = market.buy(5, 101.0)

    assert result.trades
    assert not result.resting
    assert market.book.best_bid() is None
    market.validate()


def test_a_locked_price_trades_rather_than_locking(market):
    """A bid equal to the best ask must trade — the book may not sit locked."""
    market.sell(5, 100.0)
    result = market.buy(5, 100.0)

    assert len(result.trades) == 1
    assert market.book.best_bid() is None
    assert market.book.best_ask() is None
    market.validate()


def test_book_is_uncrossed_after_a_deep_sweep(market):
    for price in (100.0, 100.5, 101.0, 101.5):
        market.sell(5, price)
    for price in (99.5, 99.0):
        market.buy(5, price)

    market.buy(14, 101.5)

    best_bid, best_ask = market.book.best_bid(), market.book.best_ask()
    assert best_bid is not None and best_ask is not None
    assert best_bid < best_ask
    market.validate()


def test_remainder_rests_without_crossing_the_far_side(market):
    """A partially filled aggressor rests at its own price, never through the book."""
    market.sell(5, 100.0)
    market.sell(5, 103.0)

    result = market.buy(10, 101.0)

    assert result.filled_quantity == 5
    assert market.book.best_bid() == 101.0
    assert market.book.best_ask() == 103.0
    assert market.book.best_bid() < market.book.best_ask()
    market.validate()


def test_repeated_crossing_flow_never_leaves_a_crossed_book(market):
    """Alternate aggressive both ways; the book must be uncrossed at every step."""
    for i in range(60):
        if i % 2:
            market.buy(7, 100.0 + (i % 5) * 0.01)
        else:
            market.sell(7, 100.0 - (i % 5) * 0.01)

        best_bid, best_ask = market.book.best_bid(), market.book.best_ask()
        if best_bid is not None and best_ask is not None:
            assert best_bid < best_ask, f"crossed at step {i}"
        market.validate()


def test_validate_detects_a_forced_crossed_book(book):
    """Bypass matching to plant a crossed state; validate must reject it."""
    book.add(Order(1, Side.BUY, OrderType.LIMIT, 5, 101.0))
    book.add(Order(2, Side.SELL, OrderType.LIMIT, 5, 100.0))

    with pytest.raises(BookIntegrityError, match="crossed book"):
        book.validate()


# ----------------------------------------------------------------------
# Conservation and invariants
# ----------------------------------------------------------------------


def test_book_never_stays_crossed(market):
    market.sell(10, 100.0)
    market.buy(10, 105.0)
    market.validate()  # raises if best bid >= best ask


def test_engine_trade_counter_matches_trades_emitted(market):
    market.sell(5, 100.0)
    market.sell(5, 101.0)
    market.buy(10, 101.0)
    assert market.engine.trades_generated == 2


def test_quantity_is_conserved_under_random_flow():
    """Traded volume + resting depth must equal everything ever submitted."""
    rng = random.Random(42)
    book = OrderBook()
    engine = MatchingEngine()

    submitted_buy = submitted_sell = 0
    bought = sold = 0

    for order_id in range(1, 2001):
        side = rng.choice([Side.BUY, Side.SELL])
        quantity = rng.randint(1, 25)
        price = round(rng.uniform(98.0, 102.0), 2)
        result = engine.submit(
            Order(order_id, side, OrderType.LIMIT, quantity, price), book
        )

        if side is Side.BUY:
            submitted_buy += quantity
        else:
            submitted_sell += quantity

        traded = sum(t.quantity for t in result.trades)
        bought += traded if side is Side.BUY else 0
        sold += traded if side is Side.SELL else 0
        # Each trade also fills the opposing resting order.
        if side is Side.BUY:
            sold += traded
        else:
            bought += traded

    book.validate()
    assert bought == sold  # every share bought was sold
    assert submitted_buy - bought == book.bid_depth
    assert submitted_sell - sold == book.ask_depth


def test_matching_is_deterministic_under_a_fixed_seed():
    def run():
        rng = random.Random(7)
        book = OrderBook()
        engine = MatchingEngine()
        prints = []
        for order_id in range(1, 501):
            side = rng.choice([Side.BUY, Side.SELL])
            order = Order(
                order_id,
                side,
                OrderType.LIMIT,
                rng.randint(1, 15),
                round(rng.uniform(99.0, 101.0), 2),
            )
            for trade in engine.submit(order, book).trades:
                prints.append((trade.price, trade.quantity, trade.resting_order_id))
        return prints, book.best_bid(), book.best_ask()

    assert run() == run()
