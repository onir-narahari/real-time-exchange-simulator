"""Benchmark the core engine.

Measurement lives here, not inside the engine: each operation is timed from
the outside with ``perf_counter_ns``, so the hot path carries no
instrumentation and the numbers reflect the code that actually ships.

The baseline is frozen. ``--baseline`` takes no tuning parameters — it always
runs the same five workloads, the same operation count, the same seed, and the
same repeat structure, so two baselines are always comparable:

    python -m benchmarks.benchmark --baseline

Ad-hoc runs are for exploring, not for comparing:

    python -m benchmarks.benchmark --workload hot_price --ops 50000
"""

from __future__ import annotations

import argparse
import array
import csv
import gc
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence

if __package__ in (None, ""):  # allow `python benchmarks/benchmark.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks import workloads
from benchmarks.workloads import BASELINE_WORKLOADS, CANCEL, SUBMIT
from orderbook import MatchingEngine, OrderBook, Side

PERCENTILES = (50.0, 95.0, 99.0, 99.9)
DEFAULT_SLOW_US = 500.0

# ----------------------------------------------------------------------
# The frozen baseline. Changing any of these invalidates comparison against
# every baseline recorded before the change — treat it as a schema change.
# ----------------------------------------------------------------------
BASELINE_OPS = 200_000
BASELINE_SEED = 42
BASELINE_REPEATS = 5
BASELINE_WARMUP = 2

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
BASELINE_PATH = os.path.join(RESULTS_DIR, "baseline.json")


# ----------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------


def percentile(sorted_us: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile over an ascending sample list."""
    if not sorted_us:
        return 0.0
    rank = max(1, min(len(sorted_us), int(round(pct / 100.0 * len(sorted_us)))))
    return sorted_us[rank - 1]


def _pct_key(pct: float) -> str:
    return f"p{pct:g}".replace(".", "_")


def new_sample_buffer() -> "array.array":
    """A raw double buffer, not a list of Python floats.

    This matters more than it looks. A ``list`` of 200,000 floats is 200,000
    GC-tracked objects that the collector walks on every pass; holding several
    passes' worth makes each later pass measurably slower and inflates
    run-to-run spread. ``array('d')`` stores the samples inline, invisible to
    the collector, so the measurement stops perturbing what it measures.
    """
    return array.array("d")


def summarize_latency(samples_us) -> Dict[str, float]:
    if not samples_us:
        return {"count": 0}
    ordered = sorted(samples_us)
    total = sum(ordered)
    stats: Dict[str, float] = {
        "count": len(ordered),
        "total_ms": round(total / 1000.0, 3),
        "mean_us": round(total / len(ordered), 3),
        "min_us": round(ordered[0], 3),
        "max_us": round(ordered[-1], 3),
    }
    for pct in PERCENTILES:
        stats[f"{_pct_key(pct)}_us"] = round(percentile(ordered, pct), 3)
    return stats


class SlowOp:
    __slots__ = ("index", "kind", "elapsed_us", "context")

    def __init__(self, index: int, kind: str, elapsed_us: float, context: dict):
        self.index = index
        self.kind = kind
        self.elapsed_us = elapsed_us
        self.context = context

    def as_dict(self) -> dict:
        return {
            "op_index": self.index,
            "kind": self.kind,
            "elapsed_us": round(self.elapsed_us, 3),
            **self.context,
        }


# ----------------------------------------------------------------------
# A single pass
# ----------------------------------------------------------------------


def run_once(
    workload_name: str,
    ops: int,
    seed: int,
    slow_us: float = DEFAULT_SLOW_US,
    max_slow_records: int = 25,
    disable_gc: bool = False,
) -> dict:
    """One pass of one workload. Returns raw samples plus counters."""
    generator = workloads.get(workload_name)(ops, seed)
    gc_was_enabled = gc.isenabled()
    if disable_gc:
        gc.disable()

    book = OrderBook()
    engine = MatchingEngine()
    submit_us = new_sample_buffer()
    cancel_us = new_sample_buffer()
    slow_ops: List[SlowOp] = []

    trades = 0
    volume = 0
    notional = 0.0
    rested = 0
    cancel_attempts = 0
    cancel_hits = 0
    market_orders = 0

    started = time.perf_counter_ns()
    for index, op in enumerate(generator):
        if op.kind == SUBMIT:
            order = op.order
            is_market = not order.is_limit

            begin = time.perf_counter_ns()
            result = engine.submit(order, book)
            elapsed = time.perf_counter_ns() - begin

            submit_us.append(elapsed / 1000.0)
            market_orders += is_market
            rested += result.resting
            for trade in result.trades:
                trades += 1
                volume += trade.quantity
                notional += trade.notional
        else:
            cancel_attempts += 1

            begin = time.perf_counter_ns()
            cancelled = engine.cancel(op.order_id, book)
            elapsed = time.perf_counter_ns() - begin

            cancel_us.append(elapsed / 1000.0)
            cancel_hits += cancelled is not None

        elapsed_us = elapsed / 1000.0
        if elapsed_us >= slow_us and len(slow_ops) < max_slow_records:
            slow_ops.append(SlowOp(index, op.kind, elapsed_us, _book_context(book)))

    runtime_sec = (time.perf_counter_ns() - started) / 1e9
    if disable_gc and gc_was_enabled:
        gc.enable()

    book.validate()
    total_ops = len(submit_us) + len(cancel_us)

    return {
        "runtime_sec": runtime_sec,
        "total_ops": total_ops,
        "throughput_ops_sec": total_ops / runtime_sec if runtime_sec else 0.0,
        "submit_us": submit_us,
        "cancel_us": cancel_us,
        "counters": {
            "submits": len(submit_us),
            "market_orders": market_orders,
            "orders_rested": rested,
            "cancel_attempts": cancel_attempts,
            "cancel_hits": cancel_hits,
        },
        "market": {
            "trades": trades,
            "volume": volume,
            "notional": round(notional, 2),
        },
        "final_book": _book_context(book),
        "level_churn": {
            "levels_created": book.level_creates,
            "levels_removed": book.level_removes,
        },
        "slow_ops": [s.as_dict() for s in slow_ops],
    }


def _book_context(book: OrderBook) -> dict:
    spread = book.spread()
    return {
        "resting_orders": len(book),
        "bid_levels": book.level_count(Side.BUY),
        "ask_levels": book.level_count(Side.SELL),
        "bid_depth": book.bid_depth,
        "ask_depth": book.ask_depth,
        "best_bid": book.best_bid(),
        "best_ask": book.best_ask(),
        "spread": round(spread, 4) if spread is not None else None,
    }


# ----------------------------------------------------------------------
# Repeats
# ----------------------------------------------------------------------


def run(
    workload_name: str,
    ops: int,
    seed: int,
    repeats: int = 1,
    warmup: int = 0,
    slow_us: float = DEFAULT_SLOW_US,
    disable_gc: bool = False,
) -> dict:
    """Run a workload ``repeats`` times after ``warmup`` discarded passes.

    Throughput is reported as the median across passes with the observed
    spread, so run-to-run noise is visible rather than hidden. Latency
    percentiles pool every sample from every measured pass — more samples make
    p99.9 mean something.
    """
    for _ in range(warmup):
        run_once(workload_name, ops, seed, slow_us=slow_us, disable_gc=disable_gc)
        gc.collect()

    passes = []
    for _ in range(max(1, repeats)):
        # Collect *between* passes, never during one, so each pass starts from
        # a comparable heap instead of inheriting its predecessors' garbage.
        gc.collect()
        passes.append(
            run_once(workload_name, ops, seed, slow_us=slow_us, disable_gc=disable_gc)
        )

    throughputs = [p["throughput_ops_sec"] for p in passes]
    runtimes = [p["runtime_sec"] for p in passes]

    pooled_submit = new_sample_buffer()
    pooled_cancel = new_sample_buffer()
    for p in passes:
        pooled_submit.extend(p["submit_us"])
        pooled_cancel.extend(p["cancel_us"])

    first = passes[0]
    counters = first["counters"]
    cancel_attempts = counters["cancel_attempts"]

    return {
        "workload": workload_name,
        "description": workloads.describe(workload_name),
        "ops_requested": ops,
        "seed": seed,
        "repeats": len(passes),
        "warmup": warmup,
        "runtime_sec": round(statistics.median(runtimes), 4),
        "throughput_ops_sec": round(statistics.median(throughputs)),
        "throughput_min_ops_sec": round(min(throughputs)),
        "throughput_max_ops_sec": round(max(throughputs)),
        "throughput_spread_pct": (
            round((max(throughputs) - min(throughputs)) / statistics.median(throughputs) * 100, 2)
            if statistics.median(throughputs)
            else 0.0
        ),
        "throughput_per_pass": [round(t) for t in throughputs],
        "operations": {
            "total": first["total_ops"],
            **counters,
            "cancel_hit_rate_pct": (
                round(100.0 * counters["cancel_hits"] / cancel_attempts, 2)
                if cancel_attempts
                else None
            ),
        },
        "market": first["market"],
        "latency": {
            "submit": summarize_latency(pooled_submit),
            "cancel": summarize_latency(pooled_cancel),
        },
        "final_book": first["final_book"],
        "level_churn": first["level_churn"],
        "slow_ops": first["slow_ops"],
        "slow_threshold_us": slow_us,
        "gc_disabled": disable_gc,
    }


# ----------------------------------------------------------------------
# Environment capture — a baseline is only meaningful with its provenance
# ----------------------------------------------------------------------


def _git_revision() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(RESULTS_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return None
        revision = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None

    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=os.path.dirname(RESULTS_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            revision += "-dirty"
    except (OSError, subprocess.SubprocessError):
        pass
    return revision


def environment() -> dict:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "git_revision": _git_revision(),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------


def print_report(result: dict) -> None:
    width = 78
    print()
    print("=" * width)
    print(f"{result['workload'].upper()}  —  {result['description']}")
    print("=" * width)

    ops = result["operations"]
    market = result["market"]
    print(f"{'ops':<20}{ops['total']:>14,}{'seed':>18}{result['seed']:>10}")
    print(
        f"{'runtime (median)':<20}{result['runtime_sec']:>13.3f}s"
        f"{'repeats':>18}{result['repeats']:>10}"
    )
    print(
        f"{'throughput':<20}{result['throughput_ops_sec']:>12,}/s"
        f"{'spread':>18}{result['throughput_spread_pct']:>9.2f}%"
    )
    print(f"{'trades':<20}{market['trades']:>14,}{'volume':>18}{market['volume']:>10,}")
    hit_rate = ops["cancel_hit_rate_pct"]
    print(
        f"{'orders rested':<20}{ops['orders_rested']:>14,}"
        f"{'cancel hit rate':>18}"
        f"{('n/a' if hit_rate is None else f'{hit_rate}%'):>10}"
    )

    print()
    print(_latency_header())
    print("-" * width)
    for name in ("submit", "cancel"):
        line = _latency_row(name, result["latency"][name])
        if line:
            print(line)
    print(f"{'':<10}{'(microseconds)':>68}")

    book = result["final_book"]
    print()
    print(
        f"final book: {book['resting_orders']:,} resting across "
        f"{book['bid_levels']}x{book['ask_levels']} levels  |  "
        f"{book['bid_depth']:,} @ {book['best_bid']} / "
        f"{book['best_ask']} @ {book['ask_depth']:,}"
    )

    slow = result["slow_ops"]
    if slow:
        print(f"\nslow ops (>= {result['slow_threshold_us']}us): {len(slow)} in pass 1")
        for record in slow[:3]:
            print(
                f"  #{record['op_index']:<8} {record['kind']:<8}"
                f"{record['elapsed_us']:>10.1f}us  "
                f"{record['resting_orders']:,} resting"
            )
    print()


def _latency_header() -> str:
    header = f"{'operation':<10}{'count':>10}{'mean':>9}"
    header += "".join(f"{'p' + f'{p:g}':>9}" for p in PERCENTILES)
    return header + f"{'max':>10}"


def _latency_row(name: str, stats: dict) -> Optional[str]:
    if not stats.get("count"):
        return None
    row = f"{name:<10}{stats['count']:>10,}{stats['mean_us']:>9.2f}"
    for pct in PERCENTILES:
        row += f"{stats[f'{_pct_key(pct)}_us']:>9.2f}"
    return row + f"{stats['max_us']:>10.2f}"


def print_baseline_table(payload: dict) -> None:
    runs = payload["runs"]
    width = 96
    config = payload["config"]
    env = payload["environment"]

    print()
    print("=" * width)
    print("BASELINE")
    print("=" * width)
    print(
        f"  {config['ops']:,} ops x {config['repeats']} repeats "
        f"({config['warmup']} warmup discarded), seed {config['seed']}"
    )
    print(
        f"  {env['implementation']} {env['python']} on {env['machine']}  |  "
        f"git {env['git_revision'] or 'unknown'}  |  {env['recorded_at']}"
    )
    print("-" * width)

    header = f"{'workload':<14}{'throughput':>13}{'spread':>8}{'trades':>10}{'op':>8}"
    header += "".join(f"{'p' + f'{p:g}':>9}" for p in PERCENTILES)
    header += f"{'max':>10}"
    print(header)
    print("-" * width)

    for result in runs:
        for op_name in ("submit", "cancel"):
            stats = result["latency"][op_name]
            if not stats.get("count"):
                continue
            first = op_name == "submit"
            row = f"{result['workload'] if first else '':<14}"
            row += f"{result['throughput_ops_sec']:>12,}/s" if first else f"{'':>13}"
            row += (
                f"{result['throughput_spread_pct']:>7.1f}%" if first else f"{'':>8}"
            )
            row += f"{result['market']['trades']:>10,}" if first else f"{'':>10}"
            row += f"{op_name:>8}"
            for pct in PERCENTILES:
                row += f"{stats[f'{_pct_key(pct)}_us']:>9.2f}"
            row += f"{stats['max_us']:>10.2f}"
            print(row)

    print("-" * width)
    print(f"  latencies in microseconds; throughput is the median of {config['repeats']} passes")
    print()


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------


def write_json(payload: dict, path: str) -> None:
    _ensure_parent(path)
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2)
    print(f"wrote {path}")


def write_csv(results: List[dict], path: str) -> None:
    _ensure_parent(path)
    rows = [_flat_row(r) for r in results]

    # Workloads that issue no cancels have no cancel columns; take the union
    # so a mixed run still produces one rectangular table.
    fieldnames: List[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path}")


def _flat_row(result: dict) -> dict:
    row = {
        "workload": result["workload"],
        "ops": result["operations"]["total"],
        "seed": result["seed"],
        "repeats": result["repeats"],
        "runtime_sec": result["runtime_sec"],
        "throughput_ops_sec": result["throughput_ops_sec"],
        "throughput_spread_pct": result["throughput_spread_pct"],
        "trades": result["market"]["trades"],
        "volume": result["market"]["volume"],
        "orders_rested": result["operations"]["orders_rested"],
        "cancel_hit_rate_pct": result["operations"]["cancel_hit_rate_pct"],
        "resting_orders": result["final_book"]["resting_orders"],
        "bid_levels": result["final_book"]["bid_levels"],
        "ask_levels": result["final_book"]["ask_levels"],
    }
    for name, stats in result["latency"].items():
        if not stats.get("count"):
            continue
        row[f"{name}_mean_us"] = stats["mean_us"]
        for pct in PERCENTILES:
            row[f"{name}_{_pct_key(pct)}_us"] = stats[f"{_pct_key(pct)}_us"]
        row[f"{name}_max_us"] = stats["max_us"]
    return row


def _ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


# ----------------------------------------------------------------------
# Baseline
# ----------------------------------------------------------------------


def run_baseline(disable_gc: bool = False) -> dict:
    """Run the frozen baseline. Takes no tuning parameters, by design."""
    runs = [
        run(
            name,
            BASELINE_OPS,
            BASELINE_SEED,
            repeats=BASELINE_REPEATS,
            warmup=BASELINE_WARMUP,
            disable_gc=disable_gc,
        )
        for name in BASELINE_WORKLOADS
    ]
    return {
        "kind": "baseline",
        "config": {
            "workloads": list(BASELINE_WORKLOADS),
            "ops": BASELINE_OPS,
            "seed": BASELINE_SEED,
            "repeats": BASELINE_REPEATS,
            "warmup": BASELINE_WARMUP,
            "percentiles": list(PERCENTILES),
            "gc_disabled": disable_gc,
        },
        "environment": environment(),
        "runs": runs,
    }


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark the order book and matching engine."
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help=(
            f"run the frozen baseline ({BASELINE_OPS:,} ops x {BASELINE_REPEATS} "
            f"repeats, seed {BASELINE_SEED}, all five workloads) and write "
            "benchmarks/results/baseline.json"
        ),
    )
    parser.add_argument(
        "--out",
        help="where to write baseline JSON (default benchmarks/results/baseline.json)",
    )
    parser.add_argument(
        "--workload",
        default="all",
        help=f"ad-hoc workload, or 'all' ({', '.join(BASELINE_WORKLOADS)})",
    )
    parser.add_argument("--ops", type=int, default=100_000, help="operations per pass")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed")
    parser.add_argument("--repeats", type=int, default=1, help="measured passes")
    parser.add_argument("--warmup", type=int, default=0, help="discarded passes")
    parser.add_argument(
        "--slow-us",
        type=float,
        default=DEFAULT_SLOW_US,
        help="capture book context for ops slower than this (microseconds)",
    )
    parser.add_argument(
        "--no-gc",
        dest="disable_gc",
        action="store_true",
        help="disable the cyclic collector during the run "
        "(isolates GC pauses from engine cost in the latency tail)",
    )
    parser.add_argument("--json", dest="json_path", help="write full results as JSON")
    parser.add_argument("--csv", dest="csv_path", help="write a summary row as CSV")
    parser.add_argument("--quiet", action="store_true", help="suppress the table")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)

    if args.baseline:
        payload = run_baseline(disable_gc=args.disable_gc)
        if not args.quiet:
            print_baseline_table(payload)
        write_json(payload, args.out or BASELINE_PATH)
        write_csv(payload["runs"], os.path.splitext(args.out or BASELINE_PATH)[0] + ".csv")
        return

    names = list(BASELINE_WORKLOADS) if args.workload == "all" else [args.workload]
    if args.workload != "all":
        workloads.get(args.workload)  # validates the name

    results = [
        run(
            name,
            args.ops,
            args.seed,
            repeats=args.repeats,
            warmup=args.warmup,
            slow_us=args.slow_us,
            disable_gc=args.disable_gc,
        )
        for name in names
    ]

    if not args.quiet:
        for result in results:
            print_report(result)

    if args.json_path:
        payload = {
            "kind": "adhoc",
            "config": {
                "workloads": names,
                "ops": args.ops,
                "seed": args.seed,
                "repeats": args.repeats,
                "warmup": args.warmup,
                "percentiles": list(PERCENTILES),
                "gc_disabled": args.disable_gc,
            },
            "environment": environment(),
            "runs": results,
        }
        write_json(payload, args.json_path)
    if args.csv_path:
        write_csv(results, args.csv_path)


if __name__ == "__main__":
    main()
