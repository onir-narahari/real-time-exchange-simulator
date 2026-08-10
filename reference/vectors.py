"""Cross-language differential test vectors.

The Python optimized engine is the behavioral oracle for the C++ engine.
This module replays the same deterministic flows used by the in-process
differential suite (``reference/differential.py``) and records, after *every*
operation, exactly what the engine did and the state it left behind:

    python -m reference.vectors --seeds 100 --ops 1000 --out vectors/

The C++ checker (``orderbook_differential``) replays the file and must
reproduce every line. While generating, each case is *also* checked against
the naive reference engine, so a green run means all three engines agree.

Vector format (line-based, space-separated; prices are integer ticks =
price x 100, so no floating-point text ever crosses the language boundary):

    case <profile> <seed> <ops>
    op <i> submit <id> <buy|sell> <limit|market> <qty> <ticks|->
    ex <i> trades <n>
    tr <trade_id> <ticks> <qty> <aggr_side> <aggr_id> <rest_id>   (n lines)
    rs <i> <remaining> <rested 0|1>
    op <i> cancel <id>
    cx <i> <hit 0|1>
    st <i> <best_bid|-> <best_ask|-> <bid_depth> <ask_depth> <resting> <l2_hex> <l3_hex>

The two hex digests are FNV-1a over the canonical L2 / L3 book text (see
``_canonical_l2`` / ``_canonical_l3``). A digest mismatch means the books
differ; get the full Python state at the failing op with:

    python -m reference.vectors explain --profile tight --seed 0 --ops 300 --stop-at 42
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import IO, Optional, Sequence

if __package__ in (None, ""):  # allow `python reference/vectors.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderbook import MatchingEngine, Order, OrderBook, Side
from reference.differential import PROFILES, random_ops
from reference.naive_engine import NaiveEngine

TICK = 100  # prices are 2-decimal; ticks = price * 100 (exact for the grids)


# ----------------------------------------------------------------------
# Canonical state text and digests — the C++ checker implements these
# byte-for-byte; change both or nothing.
# ----------------------------------------------------------------------


def _fnv1a(text: str) -> int:
    h = 14695981039346656037
    for byte in text.encode("ascii"):
        h ^= byte
        h = (h * 1099511628211) % (1 << 64)
    return h


def _ticks(price: float) -> int:
    return int(round(price * TICK))


def _canonical_l3(book: OrderBook) -> str:
    lines = []
    for order in book.orders(Side.BUY):
        lines.append(f"b {order.order_id} {_ticks(order.price)} {order.remaining}\n")
    for order in book.orders(Side.SELL):
        lines.append(f"a {order.order_id} {_ticks(order.price)} {order.remaining}\n")
    return "".join(lines)


def _canonical_l2(book: OrderBook) -> str:
    snapshot = book.l2()
    lines = [
        f"B {_ticks(lv.price)} {lv.quantity} {lv.order_count}\n" for lv in snapshot.bids
    ]
    lines += [
        f"A {_ticks(lv.price)} {lv.quantity} {lv.order_count}\n" for lv in snapshot.asks
    ]
    return "".join(lines)


def _state_line(book: OrderBook, index: int) -> str:
    best_bid = book.best_bid()
    best_ask = book.best_ask()
    return (
        f"st {index} "
        f"{_ticks(best_bid) if best_bid is not None else '-'} "
        f"{_ticks(best_ask) if best_ask is not None else '-'} "
        f"{book.bid_depth} {book.ask_depth} {len(book)} "
        f"{_fnv1a(_canonical_l2(book)):016x} {_fnv1a(_canonical_l3(book)):016x}\n"
    )


# ----------------------------------------------------------------------
# Generation
# ----------------------------------------------------------------------


def emit_case(out: IO[str], profile: str, seed: int, ops: int, check_reference: bool) -> int:
    """Replay one flow, writing ops and per-op expected results. Returns trades.

    With ``check_reference`` the naive engine is driven alongside and must
    agree after every op — generation is itself a two-engine differential run.
    """
    book = OrderBook()
    optimized = MatchingEngine()
    reference = NaiveEngine() if check_reference else None
    total_trades = 0

    out.write(f"case {profile} {seed} {ops}\n")

    for index, op in enumerate(random_ops(seed, ops, profile)):
        if op.kind == "submit":
            price = "-" if op.price is None else str(_ticks(op.price))
            out.write(
                f"op {index} submit {op.order_id} {op.side} {op.order_type} "
                f"{op.quantity} {price}\n"
            )

            order = Order(op.order_id, op.side, op.order_type, op.quantity, op.price)
            result = optimized.submit(order, book)
            total_trades += len(result.trades)

            out.write(f"ex {index} trades {len(result.trades)}\n")
            for t in result.trades:
                out.write(
                    f"tr {t.trade_id} {_ticks(t.price)} {t.quantity} "
                    f"{t.aggressor_side.value} {t.aggressor_order_id} "
                    f"{t.resting_order_id}\n"
                )
            out.write(f"rs {index} {result.order.remaining} {int(result.resting)}\n")

            if reference is not None:
                ref = reference.submit(
                    op.order_id, op.side, op.order_type, op.quantity, op.price
                )
                assert ref.remaining == result.order.remaining, (seed, index)
                assert ref.resting == result.resting, (seed, index)
                assert len(ref.trades) == len(result.trades), (seed, index)
        else:
            out.write(f"op {index} cancel {op.order_id}\n")
            cancelled = optimized.cancel(op.order_id, book)
            out.write(f"cx {index} {int(cancelled is not None)}\n")
            if reference is not None:
                assert reference.cancel(op.order_id) == (cancelled is not None), (
                    seed,
                    index,
                )

        out.write(_state_line(book, index))
        book.validate()

    return total_trades


def explain(profile: str, seed: int, ops: int, stop_at: int) -> None:
    """Print the full Python state just after operation ``stop_at``."""
    book = OrderBook()
    engine = MatchingEngine()

    for index, op in enumerate(random_ops(seed, ops, profile)):
        if op.kind == "submit":
            engine.submit(
                Order(op.order_id, op.side, op.order_type, op.quantity, op.price), book
            )
        else:
            engine.cancel(op.order_id, book)

        if index == stop_at:
            print(f"python state after op #{index}: {op.describe()}")
            print(f"  best_bid {book.best_bid()}  best_ask {book.best_ask()}")
            print(f"  bid_depth {book.bid_depth}  ask_depth {book.ask_depth}  "
                  f"resting {len(book)}")
            print(f"  l2 digest {_fnv1a(_canonical_l2(book)):016x}  "
                  f"l3 digest {_fnv1a(_canonical_l3(book)):016x}")
            print("  bids L3:")
            for o in book.orders(Side.BUY):
                print(f"    id={o.order_id} {o.price} x {o.remaining} seq={o.sequence}")
            print("  asks L3:")
            for o in book.orders(Side.SELL):
                print(f"    id={o.order_id} {o.price} x {o.remaining} seq={o.sequence}")
            return

    raise SystemExit(f"case has fewer than {stop_at + 1} operations")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> None:
    # allow_abbrev=False: without it argparse resolves the subcommand's
    # --seed against the main parser's --seeds/--seed-start and fails.
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    sub = parser.add_subparsers(dest="command")

    explain_parser = sub.add_parser("explain", help="print full Python state at one op")
    explain_parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    explain_parser.add_argument("--seed", type=int, required=True)
    explain_parser.add_argument("--ops", type=int, required=True)
    explain_parser.add_argument("--stop-at", type=int, required=True)

    parser.add_argument("--seeds", type=int, default=100, help="seeds per profile")
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument("--ops", type=int, default=1000, help="operations per case")
    parser.add_argument(
        "--profile", default="all", help=f"flow profile, or 'all' ({', '.join(PROFILES)})"
    )
    parser.add_argument("--out", default="vectors", help="output directory")
    parser.add_argument(
        "--skip-reference-check",
        action="store_true",
        help="do not cross-check the naive reference while generating",
    )
    args = parser.parse_args(argv)

    if args.command == "explain":
        explain(args.profile, args.seed, args.ops, args.stop_at)
        return

    profiles = list(PROFILES) if args.profile == "all" else [args.profile]
    if args.profile != "all" and args.profile not in PROFILES:
        raise SystemExit(f"unknown profile {args.profile!r}")

    os.makedirs(args.out, exist_ok=True)
    total_cases = total_ops = total_trades = 0

    for profile in profiles:
        path = os.path.join(args.out, f"{profile}.vec")
        trades = 0
        with open(path, "w") as out:
            for offset in range(args.seeds):
                trades += emit_case(
                    out,
                    profile,
                    args.seed_start + offset,
                    args.ops,
                    check_reference=not args.skip_reference_check,
                )
        total_cases += args.seeds
        total_ops += args.seeds * args.ops
        total_trades += trades
        print(f"  {profile:<14} {args.seeds:>5} seeds x {args.ops:>6,} ops   "
              f"{trades:>9,} trades   -> {path}")

    print(f"  {total_cases:,} cases   {total_ops:,} operations   "
          f"{total_trades:,} trades recorded")


if __name__ == "__main__":
    main()
