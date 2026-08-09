"""Differential testing: run the same order flow through both engines.

                        SAME ORDERS
                             |
                    +--------+--------+
                    v                 v
                Reference          Optimized
                (naive_engine)     (orderbook)
                    |                 |
                    +--------+--------+
                             v
                          COMPARE

After *every single operation* the two engines are compared on:

- the trades just emitted — id, price, quantity, aggressor, resting order hit
- what happened to the incoming order — remaining quantity, whether it rested
- the full resting book, order by order, in price-time priority (L3)
- the aggregated book (L2), best bid/ask, and per-side depth

Comparing after every op rather than at the end means a divergence is reported
at the operation that caused it, with both books printed side by side.

Run a sweep directly:

    python -m reference.differential --seeds 200 --ops 500
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from typing import Iterator, List, NamedTuple, Optional, Sequence

if __package__ in (None, ""):  # allow `python reference/differential.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderbook import MatchingEngine, Order, OrderBook, Side
from reference.naive_engine import BUY, LIMIT, MARKET, SELL, NaiveEngine

SUBMIT = "submit"
CANCEL = "cancel"


class OpSpec(NamedTuple):
    """One operation, described in plain values so both engines can build it."""

    kind: str
    order_id: int
    side: str = BUY
    order_type: str = LIMIT
    quantity: int = 0
    price: Optional[float] = None

    def describe(self) -> str:
        if self.kind == CANCEL:
            return f"CANCEL id={self.order_id}"
        price = "MKT" if self.price is None else self.price
        return (
            f"SUBMIT id={self.order_id} {self.side} {self.order_type} "
            f"{self.quantity} @ {price}"
        )


class DifferentialMismatch(AssertionError):
    pass


# ----------------------------------------------------------------------
# Flow generation
# ----------------------------------------------------------------------

#: Profiles are tuned to hit different parts of the engine. ``tight`` is the
#: nastiest: a handful of prices means constant FIFO ties and crossing.
PROFILES = {
    "tight": dict(prices=5, max_qty=8, market_rate=0.05, cancel_rate=0.20),
    "wide": dict(prices=120, max_qty=40, market_rate=0.02, cancel_rate=0.15),
    "cancel_heavy": dict(prices=20, max_qty=15, market_rate=0.02, cancel_rate=0.50),
    "market_heavy": dict(prices=15, max_qty=30, market_rate=0.35, cancel_rate=0.10),
    "sweeping": dict(prices=40, max_qty=250, market_rate=0.15, cancel_rate=0.10),
}


def random_ops(seed: int, count: int, profile: str = "tight") -> Iterator[OpSpec]:
    """Deterministic pseudo-random op stream.

    Cancel targets are drawn from ids already submitted, including ids that
    have since been filled — so cancels that miss are part of the test.
    """
    settings = PROFILES[profile]
    rng = random.Random(seed)
    submitted: List[int] = []
    next_id = 1

    #: A narrow tick grid centred on 100.
    grid = [round(100.0 + (i - settings["prices"] // 2) * 0.01, 2) for i in range(settings["prices"])]

    for _ in range(count):
        if submitted and rng.random() < settings["cancel_rate"]:
            yield OpSpec(CANCEL, order_id=rng.choice(submitted))
            continue

        order_id = next_id
        next_id += 1
        submitted.append(order_id)
        side = BUY if rng.random() < 0.5 else SELL
        quantity = rng.randint(1, settings["max_qty"])

        if rng.random() < settings["market_rate"]:
            yield OpSpec(SUBMIT, order_id, side, MARKET, quantity, None)
        else:
            yield OpSpec(SUBMIT, order_id, side, LIMIT, quantity, rng.choice(grid))


# ----------------------------------------------------------------------
# State extraction
# ----------------------------------------------------------------------


def _optimized_l3(book: OrderBook, side: Side):
    return [(o.order_id, o.price, o.remaining) for o in book.orders(side)]


def _optimized_l2(book: OrderBook, side: Side):
    snapshot = book.l2()
    levels = snapshot.bids if side is Side.BUY else snapshot.asks
    return [(lv.price, lv.quantity, lv.order_count) for lv in levels]


def optimized_state(book: OrderBook) -> dict:
    return {
        "best_bid": book.best_bid(),
        "best_ask": book.best_ask(),
        "bid_depth": book.bid_depth,
        "ask_depth": book.ask_depth,
        "resting_count": len(book),
        "bids_l3": _optimized_l3(book, Side.BUY),
        "asks_l3": _optimized_l3(book, Side.SELL),
        "bids_l2": _optimized_l2(book, Side.BUY),
        "asks_l2": _optimized_l2(book, Side.SELL),
    }


def reference_state(engine: NaiveEngine) -> dict:
    return {
        "best_bid": engine.best_bid(),
        "best_ask": engine.best_ask(),
        "bid_depth": engine.depth(BUY),
        "ask_depth": engine.depth(SELL),
        "resting_count": len(engine),
        "bids_l3": engine.l3(BUY),
        "asks_l3": engine.l3(SELL),
        "bids_l2": engine.l2(BUY),
        "asks_l2": engine.l2(SELL),
    }


def _trade_tuples_optimized(trades) -> list:
    return [
        (
            t.trade_id,
            t.price,
            t.quantity,
            t.aggressor_side.value,
            t.aggressor_order_id,
            t.resting_order_id,
        )
        for t in trades
    ]


def _trade_tuples_reference(trades) -> list:
    return [
        (
            t.trade_id,
            t.price,
            t.quantity,
            t.aggressor_side,
            t.aggressor_order_id,
            t.resting_order_id,
        )
        for t in trades
    ]


# ----------------------------------------------------------------------
# The comparison
# ----------------------------------------------------------------------


def _fail(
    label: str,
    op_index: int,
    op: OpSpec,
    expected,
    actual,
    ref_state: dict,
    opt_state: dict,
    seed: int,
    profile: str,
) -> None:
    lines = [
        "",
        "=" * 70,
        f"DIVERGENCE: {label}",
        "=" * 70,
        f"  seed      {seed}   profile {profile}",
        f"  operation #{op_index}: {op.describe()}",
        "",
        f"  reference  {expected!r}",
        f"  optimized  {actual!r}",
        "",
        "  --- reference book ---",
        f"    bids {ref_state['bids_l3']}",
        f"    asks {ref_state['asks_l3']}",
        "  --- optimized book ---",
        f"    bids {opt_state['bids_l3']}",
        f"    asks {opt_state['asks_l3']}",
        "",
        "  reproduce:",
        f"    python -m reference.differential --seeds 1 --seed-start {seed} "
        f"--profile {profile}",
        "=" * 70,
    ]
    raise DifferentialMismatch("\n".join(lines))


def run_case(seed: int, ops: int, profile: str = "tight") -> int:
    """Drive both engines through one random flow. Returns the trade count.

    Raises :class:`DifferentialMismatch` at the first operation where the two
    engines disagree about anything.
    """
    book = OrderBook()
    optimized = MatchingEngine()
    reference = NaiveEngine()
    total_trades = 0

    for index, op in enumerate(random_ops(seed, ops, profile)):
        if op.kind == SUBMIT:
            # Each engine gets its own Order object; submission mutates it.
            order = Order(op.order_id, op.side, op.order_type, op.quantity, op.price)
            opt_result = optimized.submit(order, book)
            ref_result = reference.submit(
                op.order_id, op.side, op.order_type, op.quantity, op.price
            )

            opt_trades = _trade_tuples_optimized(opt_result.trades)
            ref_trades = _trade_tuples_reference(ref_result.trades)
            total_trades += len(opt_trades)

            ref_state = reference_state(reference)
            opt_state = optimized_state(book)

            if opt_trades != ref_trades:
                _fail("trades", index, op, ref_trades, opt_trades, ref_state, opt_state, seed, profile)
            if opt_result.order.remaining != ref_result.remaining:
                _fail(
                    "incoming order remaining",
                    index, op, ref_result.remaining, opt_result.order.remaining,
                    ref_state, opt_state, seed, profile,
                )
            if opt_result.resting != ref_result.resting:
                _fail(
                    "incoming order rested",
                    index, op, ref_result.resting, opt_result.resting,
                    ref_state, opt_state, seed, profile,
                )
        else:
            opt_cancelled = optimized.cancel(op.order_id, book) is not None
            ref_cancelled = reference.cancel(op.order_id)

            ref_state = reference_state(reference)
            opt_state = optimized_state(book)

            if opt_cancelled != ref_cancelled:
                _fail(
                    "cancel outcome",
                    index, op, ref_cancelled, opt_cancelled,
                    ref_state, opt_state, seed, profile,
                )

        for key in (
            "bids_l3",
            "asks_l3",
            "bids_l2",
            "asks_l2",
            "best_bid",
            "best_ask",
            "bid_depth",
            "ask_depth",
            "resting_count",
        ):
            if opt_state[key] != ref_state[key]:
                _fail(
                    f"book state: {key}",
                    index, op, ref_state[key], opt_state[key],
                    ref_state, opt_state, seed, profile,
                )

        # The optimized book carries caches the reference does not have; make
        # sure they are self-consistent too.
        book.validate()

    return total_trades


def sweep(
    seeds: int, ops: int, profiles: Sequence[str], seed_start: int = 0, verbose: bool = True
) -> dict:
    """Run many cases. Returns aggregate counts."""
    total_trades = 0
    cases = 0

    for profile in profiles:
        profile_trades = 0
        for offset in range(seeds):
            profile_trades += run_case(seed_start + offset, ops, profile)
            cases += 1
        total_trades += profile_trades
        if verbose:
            print(
                f"  {profile:<14} {seeds:>5} seeds x {ops:>6,} ops   "
                f"{profile_trades:>9,} trades   OK"
            )

    return {"cases": cases, "ops": cases * ops, "trades": total_trades}


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Compare the optimized engine against the naive reference."
    )
    parser.add_argument("--seeds", type=int, default=50, help="seeds per profile")
    parser.add_argument("--seed-start", type=int, default=0, help="first seed")
    parser.add_argument("--ops", type=int, default=400, help="operations per case")
    parser.add_argument(
        "--profile",
        default="all",
        help=f"flow profile, or 'all' ({', '.join(PROFILES)})",
    )
    args = parser.parse_args(argv)

    profiles = list(PROFILES) if args.profile == "all" else [args.profile]
    if args.profile != "all" and args.profile not in PROFILES:
        raise SystemExit(f"unknown profile {args.profile!r}")

    print()
    print("=" * 70)
    print("DIFFERENTIAL TEST — optimized engine vs naive reference")
    print("=" * 70)

    result = sweep(args.seeds, args.ops, profiles, seed_start=args.seed_start)

    print("-" * 70)
    print(
        f"  {result['cases']:,} cases   {result['ops']:,} operations   "
        f"{result['trades']:,} trades compared"
    )
    print("  no divergence")
    print()


if __name__ == "__main__":
    main()
