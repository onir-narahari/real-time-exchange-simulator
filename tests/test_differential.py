"""The optimized engine must agree with the naive reference, order for order.

Every case below drives the same randomized flow through both engines and
compares trades, order outcomes, and the full book after *each* operation. See
``reference/differential.py`` for the comparison itself and
``reference/naive_engine.py`` for the answer key.
"""

from __future__ import annotations

import pytest

from reference.differential import PROFILES, DifferentialMismatch, run_case
from reference.naive_engine import BUY, LIMIT, MARKET, SELL, NaiveEngine


# ----------------------------------------------------------------------
# The reference itself must be right, or it is not an answer key
# ----------------------------------------------------------------------


def test_reference_matches_at_the_best_price():
    engine = NaiveEngine()
    engine.submit(1, SELL, LIMIT, 5, 101.0)
    engine.submit(2, SELL, LIMIT, 5, 100.0)

    result = engine.submit(3, BUY, LIMIT, 5, 101.0)

    assert [t.resting_order_id for t in result.trades] == [2]
    assert engine.l3(SELL) == [(1, 101.0, 5)]


def test_reference_is_fifo_within_a_price():
    engine = NaiveEngine()
    engine.submit(1, SELL, LIMIT, 5, 100.0)
    engine.submit(2, SELL, LIMIT, 5, 100.0)

    result = engine.submit(3, BUY, LIMIT, 10, 100.0)

    assert [t.resting_order_id for t in result.trades] == [1, 2]


def test_reference_prints_at_the_resting_price():
    engine = NaiveEngine()
    engine.submit(1, SELL, LIMIT, 5, 100.0)
    result = engine.submit(2, BUY, LIMIT, 5, 105.0)
    assert result.trades[0].price == 100.0


def test_reference_expires_a_market_remainder():
    engine = NaiveEngine()
    engine.submit(1, SELL, LIMIT, 4, 100.0)
    result = engine.submit(2, BUY, MARKET, 10)

    assert result.remaining == 6
    assert not result.resting
    assert len(engine) == 0


def test_reference_cancel_reports_whether_it_hit():
    engine = NaiveEngine()
    engine.submit(1, BUY, LIMIT, 5, 99.0)
    assert engine.cancel(1) is True
    assert engine.cancel(1) is False


# ----------------------------------------------------------------------
# Differential sweeps
# ----------------------------------------------------------------------


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_engines_agree_across_profiles(profile):
    """Every flow shape, several seeds each."""
    for seed in range(12):
        run_case(seed, 300, profile)


def test_engines_agree_on_a_long_run():
    """One long flow — catches state that only corrupts after many operations."""
    run_case(seed=99, ops=5000, profile="tight")


def test_engines_agree_when_the_book_is_repeatedly_emptied():
    """`market_heavy` drains the book often, exercising level create/collect cycles."""
    for seed in range(20, 30):
        run_case(seed, 400, "market_heavy")


def test_engines_agree_under_large_sweeping_orders():
    """Orders big enough to eat many levels at once."""
    for seed in range(40, 50):
        run_case(seed, 400, "sweeping")


# ----------------------------------------------------------------------
# The harness must be able to fail
# ----------------------------------------------------------------------


def test_harness_detects_a_broken_engine(monkeypatch):
    """Break FIFO in the optimized engine; the comparison must catch it.

    Without this, a passing differential suite would prove nothing — it could
    equally mean the comparison never looks at anything.
    """
    from orderbook.price_level import PriceLevel

    monkeypatch.setattr(
        PriceLevel,
        "peek",
        lambda self: self.tail.order if self.tail is not None else None,
    )

    with pytest.raises(DifferentialMismatch):
        run_case(seed=0, ops=300, profile="tight")


def test_harness_detects_a_stale_depth_cache(monkeypatch):
    """Corrupt a cache the reference does not even have; comparison must catch it."""
    from orderbook.order_book import OrderBook

    original = OrderBook.cancel

    def leaky_cancel(self, order_id):
        order = original(self, order_id)
        if order is not None:
            self.bid_depth += 1
        return order

    monkeypatch.setattr(OrderBook, "cancel", leaky_cancel)

    with pytest.raises(DifferentialMismatch, match="bid_depth"):
        run_case(seed=0, ops=300, profile="cancel_heavy")
