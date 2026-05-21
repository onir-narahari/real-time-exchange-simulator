#!/usr/bin/env python3
"""
Exchange simulator benchmark runner.

Runs the core exchange + traders + market maker synchronously with no console
summaries, WebSocket feed, or React dashboard. Intended for throughput and
latency measurement only.

Usage:
    python benchmark.py                    # 10k / 50k / 100k default scenarios
    python benchmark.py --orders 25000       # single custom run
    python benchmark.py --runtime 5        # run until 5 seconds elapsed
    python benchmark.py --seed 42 --traders 5
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence


class MarketMakerLike(Protocol):
    def refresh_quotes(self): ...

from RandomTraders import InventoryAwareMarketMaker, RandomTrader, create_market_maker
from exchange import Exchange
from order_book import BookInstrumentation
import sim_config
from slow_path import SLOW_OP_THRESHOLD_MS, SlowPathLogger, SlowPathSummary, print_slow_path_summary
from timing import BucketStats, TimingCollector, print_timing_breakdown, timed

DEFAULT_SCENARIOS = (10_000, 50_000, 100_000)
DEFAULT_MM_EVERY_N_ORDERS = 5  # production default (best balance of cancels vs book freshness)
RESULTS_JSON = Path("benchmark_results.json")
RESULTS_CSV = Path("benchmark_results.csv")


@dataclass
class BenchmarkConfig:
    """Inputs for a single benchmark run."""

    label: str
    num_orders: Optional[int] = None
    runtime_sec: Optional[float] = None
    num_traders: int = 3
    mm_every_n_orders: int = DEFAULT_MM_EVERY_N_ORDERS
    trader_delay_sec: float = 0.0  # sleep after each trader order (0 = max throughput)
    random_seed: Optional[int] = None
    base_price: float = 100.0
    price_jitter: float = 1.0
    mm_quote_size: int = 10
    mm_type: str = "simple"

    def validate(self) -> None:
        if self.num_orders is None and self.runtime_sec is None:
            raise ValueError("Set num_orders or runtime_sec")
        if self.num_orders is not None and self.num_orders < 1:
            raise ValueError("num_orders must be >= 1")
        if self.runtime_sec is not None and self.runtime_sec <= 0:
            raise ValueError("runtime_sec must be > 0")
        if self.num_traders < 1:
            raise ValueError("num_traders must be >= 1")
        if self.mm_every_n_orders < 1:
            raise ValueError("mm_every_n_orders must be >= 1")
        if self.mm_type not in ("simple", "inventory"):
            raise ValueError("mm_type must be 'simple' or 'inventory'")


@dataclass
class LatencyStats:
    """Order submission latency in milliseconds."""

    samples: int
    avg_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


@dataclass
class BenchmarkResult:
    """Collected metrics for one run."""

    label: str
    config: Dict[str, Any]
    runtime_sec: float
    trader_orders_executed: int
    orders_submitted: int
    trades_executed: int
    total_volume: int
    cancellations: int
    events_generated: int
    orders_per_sec: float
    trades_per_sec: float
    cancellations_per_sec: float
    events_per_sec: float
    latency: LatencyStats
    timing_buckets: List[BucketStats]
    mm_metrics: Optional[Dict[str, Any]] = None
    slow_path_events: Optional[List[Dict[str, Any]]] = None
    slow_path_summary: Optional[Dict[str, Any]] = None
    event_storage: Optional[Dict[str, Any]] = None

    def to_row(self) -> Dict[str, Any]:
        row = {
            "label": self.label,
            "runtime_sec": self.runtime_sec,
            "trader_orders_executed": self.trader_orders_executed,
            "orders_submitted": self.orders_submitted,
            "trades_executed": self.trades_executed,
            "total_volume": self.total_volume,
            "cancellations": self.cancellations,
            "events_generated": self.events_generated,
            "orders_per_sec": self.orders_per_sec,
            "trades_per_sec": self.trades_per_sec,
            "cancellations_per_sec": self.cancellations_per_sec,
            "events_per_sec": self.events_per_sec,
            "latency_samples": self.latency.samples,
            "latency_avg_ms": self.latency.avg_ms,
            "latency_p50_ms": self.latency.p50_ms,
            "latency_p95_ms": self.latency.p95_ms,
            "latency_p99_ms": self.latency.p99_ms,
            "latency_max_ms": self.latency.max_ms,
        }
        row.update({f"cfg_{k}": v for k, v in self.config.items()})
        return row


def build_simulation(
    cfg: BenchmarkConfig,
) -> tuple[Exchange, MarketMakerLike, List[RandomTrader]]:
    """Create exchange, market maker, and N traders (no printing)."""
    exchange = Exchange()
    mm = create_market_maker(
        exchange, mm_type=cfg.mm_type, quote_size=cfg.mm_quote_size
    )
    traders: List[RandomTrader] = []
    half = cfg.num_traders // 2
    for i in range(cfg.num_traders):
        offset = (i - half) * 0.5
        traders.append(
            RandomTrader(
                exchange,
                base_price=round(cfg.base_price + offset, 2),
                jitter=cfg.price_jitter,
            )
        )
    return exchange, mm, traders


def _percentile(sorted_samples: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile on a pre-sorted sequence."""
    n = len(sorted_samples)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_samples[0]
    rank = (pct / 100.0) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_samples[lo] + (sorted_samples[hi] - sorted_samples[lo]) * frac


def compute_latency_stats(latencies_sec: List[float]) -> LatencyStats:
    """Summarize per-order latencies (seconds in, milliseconds out)."""
    if not latencies_sec:
        return LatencyStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    ms = sorted(x * 1000.0 for x in latencies_sec)
    return LatencyStats(
        samples=len(ms),
        avg_ms=statistics.mean(ms),
        p50_ms=_percentile(ms, 50),
        p95_ms=_percentile(ms, 95),
        p99_ms=_percentile(ms, 99),
        max_ms=max(ms),
    )


def collect_session_summary(exchange: Exchange) -> None:
    """Mirror feed-style book + metrics read (no printing)."""
    with timed("book_summary"):
        buys = exchange.order_book.getBuyOrders()
        sells = exchange.order_book.getSellOrders()
        best_bid = buys[0].price if buys else None
        best_ask = sells[0].price if sells else None
        if best_bid is not None and best_ask is not None:
            _ = best_ask - best_bid
        _ = sum(o.quantity for o in buys), sum(o.quantity for o in sells)
        m = exchange.metrics
        _ = (
            m.orders_submitted,
            m.total_trades,
            m.total_volume,
            m.cancellation_count,
            len(m.filled_order_ids),
            m.fill_rate,
            m.average_spread,
            exchange.event_count,
        )


def run_benchmark(cfg: BenchmarkConfig) -> BenchmarkResult:
    """
    Execute one benchmark session.

    Each iteration: timed random trader limit order, optional trader delay,
    then market-maker refresh every ``mm_every_n_orders`` trader steps.
    Latency is measured around ``RandomTrader.submit_random_limit`` only.
    """
    cfg.validate()
    if cfg.random_seed is not None:
        random.seed(cfg.random_seed)

    BookInstrumentation.reset()
    sim_config.configure_benchmark_mode()
    exchange, mm, traders = build_simulation(cfg)
    TimingCollector.enable()
    slow_logger = SlowPathLogger.enable(exchange, mm)
    try:
        return _run_benchmark_loop(cfg, exchange, mm, traders, slow_logger)
    finally:
        TimingCollector.disable()
        SlowPathLogger.disable()


def collect_inventory_mm_metrics(
    mm: InventoryAwareMarketMaker,
    inventory_samples: List[int],
) -> Dict[str, Any]:
    """Snapshot inventory MM state after a benchmark run."""
    buys = mm.exchange.order_book.getBuyOrders()
    sells = mm.exchange.order_book.getSellOrders()
    final_fair_value = mm._fair_value(buys, sells)
    inventory_std = (
        statistics.stdev(inventory_samples) if len(inventory_samples) >= 2 else None
    )
    return {
        "final_inventory": mm.inventory,
        "cash": mm.cash,
        "final_fair_value": final_fair_value,
        "total_pnl": mm.cash + mm.inventory * final_fair_value,
        "max_abs_inventory": mm.max_abs_inventory,
        "inventory_std": inventory_std,
        "quote_refreshes": mm.quote_refreshes,
        "quote_replacements": mm.quote_replacements,
        "quote_skips": mm.quote_skips,
        "quote_kept_due_to_threshold": mm.quote_kept_due_to_threshold,
        "quote_replaced_due_to_price": mm.quote_replaced_due_to_price,
        "quote_replaced_due_to_size": mm.quote_replaced_due_to_size,
        "quote_replaced_due_to_age": mm.quote_replaced_due_to_age,
        "quote_replaced_due_to_risk": mm.quote_replaced_due_to_risk,
        "active_bid_order_id": mm._buy_quote_id,
        "active_ask_order_id": mm._sell_quote_id,
        "current_bid_quote": mm.current_bid_quote,
        "current_ask_quote": mm.current_ask_quote,
    }


def print_mm_metrics(label: str, metrics: Dict[str, Any]) -> None:
    """Print inventory market-maker metrics for one benchmark run."""
    print("\n" + "=" * 60)
    print(f"MM METRICS — {label}")
    print("=" * 60)
    print(f"  final_inventory:       {metrics['final_inventory']}")
    print(f"  cash:                  {metrics['cash']:.4f}")
    print(f"  total_pnl:             {metrics['total_pnl']:.4f}")
    print(f"  max_abs_inventory:     {metrics['max_abs_inventory']}")
    inv_std = metrics["inventory_std"]
    if inv_std is not None:
        print(f"  inventory_std:         {inv_std:.4f}")
    else:
        print("  inventory_std:         n/a")
    print(f"  quote_refreshes:       {metrics['quote_refreshes']}")
    print(f"  quote_replacements:    {metrics['quote_replacements']}")
    print(f"  quote_skips:           {metrics['quote_skips']}")
    print(f"  kept (threshold):      {metrics['quote_kept_due_to_threshold']}")
    print(f"  replaced (price):      {metrics['quote_replaced_due_to_price']}")
    print(f"  replaced (size):       {metrics['quote_replaced_due_to_size']}")
    print(f"  replaced (age):        {metrics['quote_replaced_due_to_age']}")
    print(f"  replaced (risk):       {metrics['quote_replaced_due_to_risk']}")
    print(f"  active_bid_order_id:   {metrics['active_bid_order_id']}")
    print(f"  active_ask_order_id:   {metrics['active_ask_order_id']}")
    print(f"  current_bid_quote:     {metrics['current_bid_quote']}")
    print(f"  current_ask_quote:     {metrics['current_ask_quote']}")
    print()


def collect_event_storage_metrics(exchange: Exchange) -> Dict[str, Any]:
    """Book update / event storage counters for benchmark reporting."""
    m = exchange.metrics
    return {
        "event_count": exchange.event_count,
        "recent_events_stored": len(exchange.event_history),
        "max_recent_event_history": sim_config.MAX_EVENT_HISTORY,
        "book_updates_counted": m.book_update_count,
        "book_updated_emitted": m.book_update_events_emitted,
        "book_updated_skipped": m.book_update_events_skipped,
        "book_payload_builds": m.book_event_payload_builds,
    }


def print_event_storage_section(label: str, metrics: Dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print(f"EVENT STORAGE / BOOK UPDATE — {label}")
    print("=" * 60)
    print(f"  total events:              {metrics['event_count']}")
    print(f"  recent events stored:      {metrics['recent_events_stored']}")
    print(f"  max recent event history:  {metrics['max_recent_event_history']}")
    print(f"  book updates counted:      {metrics['book_updates_counted']}")
    print(f"  BOOK_UPDATED emitted:        {metrics['book_updated_emitted']}")
    print(f"  BOOK_UPDATED skipped:        {metrics['book_updated_skipped']}")
    print(f"  book payload builds:         {metrics['book_payload_builds']}")
    print()


def _run_benchmark_loop(
    cfg: BenchmarkConfig,
    exchange: Exchange,
    mm: MarketMakerLike,
    traders: List[RandomTrader],
    slow_logger: SlowPathLogger,
) -> BenchmarkResult:
    latencies: List[float] = []
    inventory_samples: List[int] = []
    track_inventory = cfg.mm_type == "inventory"
    trader_steps = 0
    started = time.perf_counter()

    def one_trader_step() -> None:
        nonlocal trader_steps
        trader = traders[trader_steps % len(traders)]
        t0 = time.perf_counter()
        trader.submit_random_limit()
        latencies.append(time.perf_counter() - t0)
        trader_steps += 1
        if cfg.trader_delay_sec > 0:
            time.sleep(cfg.trader_delay_sec)
        if trader_steps % cfg.mm_every_n_orders == 0:
            mm.refresh_quotes()
            if track_inventory and isinstance(mm, InventoryAwareMarketMaker):
                inventory_samples.append(mm.inventory)

    if cfg.num_orders is not None:
        for _ in range(cfg.num_orders):
            one_trader_step()
    else:
        deadline = started + cfg.runtime_sec  # type: ignore[operator]
        while time.perf_counter() < deadline:
            one_trader_step()

    runtime_sec = time.perf_counter() - started
    collect_session_summary(exchange)
    collector = TimingCollector.get()
    timing_buckets = collector.snapshot() if collector else []

    m = exchange.metrics
    orders = m.orders_submitted
    trades = m.total_trades
    cancels = m.cancellation_count
    events = exchange.event_count
    safe_runtime = runtime_sec if runtime_sec > 0 else 1e-9

    latency = compute_latency_stats(latencies)
    mm_metrics = None
    if isinstance(mm, InventoryAwareMarketMaker):
        mm_metrics = collect_inventory_mm_metrics(mm, inventory_samples)

    slow_summary = slow_logger.summarize()
    slow_path_summary = {
        "threshold_ms": SLOW_OP_THRESHOLD_MS,
        "total_slow_events": slow_summary.total_slow_events,
        "count_by_bucket": slow_summary.count_by_bucket,
        "max_elapsed_by_bucket": slow_summary.max_elapsed_by_bucket,
        "avg_elapsed_by_bucket": slow_summary.avg_elapsed_by_bucket,
        "top_10_slowest": slow_summary.top_10_slowest,
    }

    return BenchmarkResult(
        label=cfg.label,
        config={
            "num_orders": cfg.num_orders,
            "runtime_sec": cfg.runtime_sec,
            "num_traders": cfg.num_traders,
            "mm_every_n_orders": cfg.mm_every_n_orders,
            "trader_delay_sec": cfg.trader_delay_sec,
            "random_seed": cfg.random_seed,
            "base_price": cfg.base_price,
            "price_jitter": cfg.price_jitter,
            "mm_quote_size": cfg.mm_quote_size,
            "mm_type": cfg.mm_type,
        },
        runtime_sec=round(runtime_sec, 4),
        trader_orders_executed=trader_steps,
        orders_submitted=orders,
        trades_executed=trades,
        total_volume=m.total_volume,
        cancellations=cancels,
        events_generated=events,
        orders_per_sec=round(orders / safe_runtime, 2),
        trades_per_sec=round(trades / safe_runtime, 2),
        cancellations_per_sec=round(cancels / safe_runtime, 2),
        events_per_sec=round(events / safe_runtime, 2),
        latency=latency,
        timing_buckets=timing_buckets,
        mm_metrics=mm_metrics,
        slow_path_events=slow_logger.events,
        slow_path_summary=slow_path_summary,
        event_storage=collect_event_storage_metrics(exchange),
    )


def default_scenario_configs(seed: Optional[int]) -> List[BenchmarkConfig]:
    return [
        BenchmarkConfig(
            label=f"{n // 1000}k_orders",
            num_orders=n,
            random_seed=seed,
        )
        for n in DEFAULT_SCENARIOS
    ]


def print_book_instrumentation() -> None:
    """Legacy-path counters; should all be zero on price-level book."""
    print("BOOK INSTRUMENTATION (expect 0 on hot path)")
    print(f"  cancel_full_scans:  {BookInstrumentation.cancel_full_scans}")
    print(f"  index_rebuilds:     {BookInstrumentation.index_rebuilds}")
    print(f"  list_removals:      {BookInstrumentation.list_removals}")
    print(f"  price_level_creates:{BookInstrumentation.price_level_creates}")
    print(f"  price_level_removes:{BookInstrumentation.price_level_removes}")
    print()


def print_results_table(results: List[BenchmarkResult]) -> None:
    """Print a compact ASCII table of benchmark outcomes."""
    headers = [
        "Scenario",
        "Runtime(s)",
        "Trader steps",
        "Orders",
        "Trades",
        "Volume",
        "Cancels",
        "Events",
        "Ord/s",
        "Trd/s",
        "Avg ms",
        "P50 ms",
        "P95 ms",
        "P99 ms",
        "Max ms",
    ]
    rows = []
    for r in results:
        rows.append(
            [
                r.label,
                f"{r.runtime_sec:.3f}",
                str(r.trader_orders_executed),
                str(r.orders_submitted),
                str(r.trades_executed),
                str(r.total_volume),
                str(r.cancellations),
                str(r.events_generated),
                f"{r.orders_per_sec:,.1f}",
                f"{r.trades_per_sec:,.1f}",
                f"{r.latency.avg_ms:.3f}",
                f"{r.latency.p50_ms:.3f}",
                f"{r.latency.p95_ms:.3f}",
                f"{r.latency.p99_ms:.3f}",
                f"{r.latency.max_ms:.3f}",
            ]
        )

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: List[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    sep = "  ".join("-" * w for w in widths)
    print("\n" + "=" * (sum(widths) + 2 * (len(widths) - 1)))
    print("EXCHANGE SIMULATOR BENCHMARK")
    print("=" * (sum(widths) + 2 * (len(widths) - 1)))
    print(fmt_row(headers))
    print(sep)
    for row in rows:
        print(fmt_row(row))
    print()


def save_results(
    results: List[BenchmarkResult],
    json_path: Path = RESULTS_JSON,
    csv_path: Path = RESULTS_CSV,
) -> None:
    """Write JSON (full detail) and CSV (flat rows)."""
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runs": [
            {
                **{
                    k: v
                    for k, v in asdict(r).items()
                    if k
                    not in (
                        "latency",
                        "slow_path_events",
                        "slow_path_summary",
                        "event_storage",
                    )
                },
                "latency": asdict(r.latency),
                "slow_path_summary": r.slow_path_summary,
                "slow_path_events": r.slow_path_events,
                "event_storage": r.event_storage,
            }
            for r in results
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    rows = [r.to_row() for r in results]
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Benchmark the exchange simulator (no WebSocket / dashboard).",
    )
    p.add_argument(
        "--orders",
        type=int,
        default=None,
        help="Number of trader orders to run (single scenario).",
    )
    p.add_argument(
        "--runtime",
        type=float,
        default=None,
        help="Run until this many seconds elapse (single scenario).",
    )
    p.add_argument(
        "--traders",
        type=int,
        default=3,
        help="Number of random traders.",
    )
    p.add_argument(
        "--mm-every",
        type=int,
        default=DEFAULT_MM_EVERY_N_ORDERS,
        dest="mm_every_n_orders",
        help="Refresh market-maker quotes every N trader orders.",
    )
    p.add_argument(
        "--mm-type",
        choices=("simple", "inventory"),
        default="simple",
        dest="mm_type",
        help="Market maker: simple (default) or inventory-aware.",
    )
    p.add_argument(
        "--trader-delay",
        type=float,
        default=0.0,
        dest="trader_delay_sec",
        help="Sleep seconds after each trader order (0 = none).",
    )
    p.add_argument("--seed", type=int, default=None, help="Random seed.")
    p.add_argument(
        "--base-price",
        type=float,
        default=100.0,
        help="Center price for trader bases.",
    )
    p.add_argument(
        "--jitter",
        type=float,
        default=1.0,
        dest="price_jitter",
        help="Price jitter for random limits.",
    )
    p.add_argument(
        "--scenarios",
        action="store_true",
        help=f"Run default scenarios: {', '.join(str(s) for s in DEFAULT_SCENARIOS)}.",
    )
    p.add_argument(
        "--json",
        type=Path,
        default=RESULTS_JSON,
        help="JSON output path.",
    )
    p.add_argument(
        "--csv",
        type=Path,
        default=RESULTS_CSV,
        help="CSV output path.",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)

    if args.orders is not None or args.runtime is not None:
        label = (
            f"{args.orders}_orders"
            if args.orders is not None
            else f"{args.runtime}s_runtime"
        )
        configs = [
            BenchmarkConfig(
                label=label,
                num_orders=args.orders,
                runtime_sec=args.runtime,
                num_traders=args.traders,
                mm_every_n_orders=args.mm_every_n_orders,
                trader_delay_sec=args.trader_delay_sec,
                random_seed=args.seed,
                base_price=args.base_price,
                price_jitter=args.price_jitter,
                mm_type=args.mm_type,
            )
        ]
    else:
        configs = default_scenario_configs(args.seed)

    results: List[BenchmarkResult] = []
    for cfg in configs:
        print(f"Running {cfg.label} …", flush=True)
        results.append(run_benchmark(cfg))

    print_results_table(results)
    for result in results:
        if result.event_storage is not None:
            print_event_storage_section(result.label, result.event_storage)
    for result in results:
        if result.mm_metrics is not None:
            print_mm_metrics(result.label, result.mm_metrics)
    for result in results:
        if result.slow_path_summary is not None:
            print_slow_path_summary(
                result.label,
                SlowPathSummary(
                    total_slow_events=result.slow_path_summary["total_slow_events"],
                    count_by_bucket=result.slow_path_summary["count_by_bucket"],
                    max_elapsed_by_bucket=result.slow_path_summary[
                        "max_elapsed_by_bucket"
                    ],
                    avg_elapsed_by_bucket=result.slow_path_summary[
                        "avg_elapsed_by_bucket"
                    ],
                    top_10_slowest=result.slow_path_summary["top_10_slowest"],
                ),
            )
    for result in results:
        if result.timing_buckets:
            print_timing_breakdown(
                result.label, result.timing_buckets, result.runtime_sec
            )
    print_book_instrumentation()
    save_results(results, json_path=args.json, csv_path=args.csv)
    print(f"Wrote {args.json} and {args.csv}")


if __name__ == "__main__":
    main()
