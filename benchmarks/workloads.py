"""Deterministic order-flow generators.

A workload is a pure function of ``(count, seed)`` that yields :class:`Op`
records. It has no view of matching outcomes — it just produces flow, exactly
like a real client would. Cancels are issued against ids the workload
previously submitted, so some of them race a fill and miss; that miss rate is
itself worth measuring.

The five workloads below are the frozen baseline set. Each one is meant to make
a *different* part of the engine the bottleneck:

===============  ==========================================================
balanced         the ordinary case — a book that fills in and trades
cancel_heavy     the O(1) cancel path
deep_book        sorted-price insertion as the level count grows
aggressive       the sweep loop and level collection
hot_price        one enormous FIFO queue at a single price
===============  ==========================================================
"""

from __future__ import annotations

import math
import random
from typing import Callable, Dict, Iterator, List, NamedTuple, Optional

from orderbook import Order, OrderType, Side

SUBMIT = "submit"
CANCEL = "cancel"

TICK = 0.01
MID = 100.0

#: The frozen baseline set, in report order.
BASELINE_WORKLOADS = (
    "balanced",
    "cancel_heavy",
    "deep_book",
    "aggressive",
    "hot_price",
)


class Op(NamedTuple):
    kind: str
    order: Optional[Order]
    order_id: int


def _submit(order: Order) -> Op:
    return Op(SUBMIT, order, order.order_id)


def _cancel(order_id: int) -> Op:
    return Op(CANCEL, None, order_id)


def _round_to_tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 2)


# ----------------------------------------------------------------------
# Workloads
# ----------------------------------------------------------------------


def balanced(count: int, seed: int) -> Iterator[Op]:
    """Symmetric two-sided limit flow. Most orders rest, a minority cross.

    The everyday case: a book that fills in, trades at the touch, and stays
    roughly balanced. Nothing in particular is stressed, which is the point —
    it is the reference shape the other four deviate from.
    """
    rng = random.Random(seed)
    for order_id in range(1, count + 1):
        side = Side.BUY if rng.random() < 0.5 else Side.SELL
        offset = rng.uniform(-0.10, 1.00)
        price = MID - offset if side is Side.BUY else MID + offset
        yield _submit(
            Order(
                order_id, side, OrderType.LIMIT, rng.randint(1, 40), _round_to_tick(price)
            )
        )


def cancel_heavy(count: int, seed: int, cancel_ratio: float = 0.5) -> Iterator[Op]:
    """Quote-churn shaped flow: post, pull, repost.

    Half of all operations are cancels. This is the workload the O(1) unlink
    exists for — a book that scans a list to cancel degrades here first and
    worst.
    """
    rng = random.Random(seed)
    # `recent` tracks candidate ids for random cancellation. Both random
    # removal and capping the window use swap-with-last-then-pop, which is
    # O(1) on a list. list.pop(0) — removing from the front to enforce the cap
    # — is O(n) and, with a 5,000-element cap hit on most submissions, turns
    # "bound the window" into a hidden per-submit cost that swamps the engine
    # timing it's supposed to be measuring. Eviction order doesn't need to be
    # FIFO — `recent` is just a pool of live ids, not a queue — so swap-pop
    # works for both cancel selection and cap eviction.
    recent: List[int] = []
    next_id = 1

    for _ in range(count):
        if recent and rng.random() < cancel_ratio:
            index = rng.randrange(len(recent))
            recent[index], recent[-1] = recent[-1], recent[index]
            yield _cancel(recent.pop())
            continue

        side = Side.BUY if rng.random() < 0.5 else Side.SELL
        offset = rng.uniform(0.01, 0.80)
        price = MID - offset if side is Side.BUY else MID + offset
        order = Order(
            next_id, side, OrderType.LIMIT, rng.randint(1, 20), _round_to_tick(price)
        )
        next_id += 1
        recent.append(order.order_id)
        if len(recent) > 5000:
            evict = rng.randrange(len(recent))
            recent[evict], recent[-1] = recent[-1], recent[evict]
            recent.pop()
        yield _submit(order)


def deep_book(count: int, seed: int, half_width: float = 25.0) -> Iterator[Op]:
    """Passive-only flow spread across a wide price grid.

    Nothing ever crosses, so the book grows without bound and the level count
    climbs into the thousands. This is the stress test for sorted-price
    insertion: if insertion cost is not flat in book depth, it shows up here.
    """
    rng = random.Random(seed)
    for order_id in range(1, count + 1):
        side = Side.BUY if rng.random() < 0.5 else Side.SELL
        offset = rng.uniform(0.05, half_width)
        price = MID - offset if side is Side.BUY else MID + offset
        yield _submit(
            Order(
                order_id, side, OrderType.LIMIT, rng.randint(1, 10), _round_to_tick(price)
            )
        )


def aggressive(
    count: int, seed: int, passive_rate: float = 0.40, market_rate: float = 0.25
) -> Iterator[Op]:
    """Marketable flow that mostly trades on arrival.

    Crossing limits and market orders, with occasional large sizes that eat
    several levels in one submission. A passive minority continuously
    replenishes depth — without it the book drains and the benchmark spends
    its time on the empty-book path instead of the sweep loop.

    Stresses sweeping, resting-order fills, and level collection.
    """
    rng = random.Random(seed)
    for order_id in range(1, count + 1):
        side = Side.BUY if rng.random() < 0.5 else Side.SELL

        if rng.random() < passive_rate:
            # Rest well away from the touch so this order provides liquidity.
            offset = rng.uniform(0.05, 1.50)
            price = MID - offset if side is Side.BUY else MID + offset
            yield _submit(
                Order(
                    order_id, side, OrderType.LIMIT, rng.randint(5, 60),
                    _round_to_tick(price),
                )
            )
            continue

        # Most takers are ordinary size; a tail of large ones forces deep sweeps.
        quantity = rng.randint(1, 40) if rng.random() < 0.85 else rng.randint(60, 250)

        if rng.random() < market_rate:
            yield _submit(Order(order_id, side, OrderType.MARKET, quantity))
            continue

        offset = rng.uniform(-1.20, 0.10)
        price = MID - offset if side is Side.BUY else MID + offset
        yield _submit(
            Order(order_id, side, OrderType.LIMIT, quantity, _round_to_tick(price))
        )


def hot_price(count: int, seed: int, cancel_ratio: float = 0.15) -> Iterator[Op]:
    """Almost everything at one price, with alternating build and drain phases.

    Side pressure oscillates, so a long FIFO queue accumulates at the touch and
    is then swept away order by order. This is the single-level contention
    case: it stresses the price level's linked list — appends, front pops, and
    mid-queue unlinks from cancels — while the level map and sorted-price array
    stay almost completely idle.
    """
    rng = random.Random(seed)
    grid = [MID, round(MID - TICK, 2), round(MID + TICK, 2)]
    recent: List[int] = []
    next_id = 1

    # Twenty build/drain cycles per run, so queue depth scales with the run
    # rather than being capped by a fixed period.
    period = max(500.0, count / 20.0)

    for index in range(count):
        if recent and rng.random() < cancel_ratio:
            position = rng.randrange(len(recent))
            recent[position], recent[-1] = recent[-1], recent[position]
            yield _cancel(recent.pop())
            continue

        # Slowly oscillating side pressure builds a queue, then drains it.
        buy_probability = 0.5 + 0.35 * math.sin(index / period * 2 * math.pi)
        side = Side.BUY if rng.random() < buy_probability else Side.SELL
        price = grid[0] if rng.random() < 0.90 else rng.choice(grid)

        order = Order(next_id, side, OrderType.LIMIT, rng.randint(1, 15), price)
        next_id += 1
        recent.append(order.order_id)
        if len(recent) > 5000:
            # Swap-pop, not pop(0): eviction order doesn't need to be FIFO,
            # and pop(0) is O(n) — see the note in cancel_heavy.
            evict = rng.randrange(len(recent))
            recent[evict], recent[-1] = recent[-1], recent[evict]
            recent.pop()
        yield _submit(order)


WORKLOADS: Dict[str, Callable[..., Iterator[Op]]] = {
    "balanced": balanced,
    "cancel_heavy": cancel_heavy,
    "deep_book": deep_book,
    "aggressive": aggressive,
    "hot_price": hot_price,
}


def get(name: str) -> Callable[..., Iterator[Op]]:
    try:
        return WORKLOADS[name]
    except KeyError:
        raise SystemExit(
            f"unknown workload {name!r}; choose from {', '.join(BASELINE_WORKLOADS)}"
        )


def describe(name: str) -> str:
    doc = (WORKLOADS[name].__doc__ or "").strip()
    return doc.split("\n", 1)[0]
