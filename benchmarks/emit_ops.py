"""Emit a workload's op stream as a text file for the C++ benchmark.

The five baseline workloads are pure functions of ``(count, seed)`` driven by
``random.Random`` — MT19937 with CPython-specific seeding and ``randbelow``
/``uniform`` details that are easy to re-implement *almost* right. Instead of
porting the RNG, the authoritative stream is recorded here and the C++
benchmark (``orderbook_benchmark``) replays it, so both languages are timed
on byte-identical flow:

    python -m benchmarks.emit_ops --ops 200000 --seed 42 --out ops/

Format — one op per line, prices as integer ticks (price x 100, exact for
these 2-decimal grids), ``-`` for a market order's absent price:

    S <order_id> <buy|sell> <limit|market> <quantity> <ticks|->
    C <order_id>
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

if __package__ in (None, ""):  # allow `python benchmarks/emit_ops.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks import workloads
from benchmarks.workloads import BASELINE_WORKLOADS, SUBMIT


def emit(name: str, ops: int, seed: int, out_dir: str) -> str:
    path = os.path.join(out_dir, f"{name}.ops")
    written = 0
    with open(path, "w") as out:
        out.write(f"# workload {name} ops {ops} seed {seed}\n")
        for op in workloads.get(name)(ops, seed):
            if op.kind == SUBMIT:
                order = op.order
                price = "-" if order.price is None else str(round(order.price * 100))
                out.write(
                    f"S {order.order_id} {order.side.value} {order.order_type.value} "
                    f"{order.quantity} {price}\n"
                )
            else:
                out.write(f"C {op.order_id}\n")
            written += 1
    if written != ops:
        raise AssertionError(f"{name}: generator yielded {written}, expected {ops}")
    return path


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workload", default="all",
        help=f"workload, or 'all' ({', '.join(BASELINE_WORKLOADS)})",
    )
    parser.add_argument("--ops", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="ops", help="output directory")
    args = parser.parse_args(argv)

    names = list(BASELINE_WORKLOADS) if args.workload == "all" else [args.workload]
    os.makedirs(args.out, exist_ok=True)
    for name in names:
        workloads.get(name)  # validates the name
        print(f"  {name:<14} {args.ops:>9,} ops  seed {args.seed}  -> "
              f"{emit(name, args.ops, args.seed, args.out)}")


if __name__ == "__main__":
    main()
