"""Async continuous market session.

Traders and the market maker run as concurrent tasks. Matching itself stays
synchronous — an ``asyncio.Lock`` serializes every mutation, so the engine only
ever sees one order at a time and the session stays deterministic in ordering.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import List, Optional

from sim.traders import RandomTrader, new_simulation


class AsyncSimulation:
    def __init__(self, mm_type: str = "simple"):
        self.exchange, self.mm, self.traders = new_simulation(mm_type=mm_type)
        self._lock = asyncio.Lock()

    async def run_trader_order(self, trader: RandomTrader):
        await asyncio.sleep(random.uniform(0.001, 0.015))
        async with self._lock:
            return trader.submit_random_limit()

    async def run_mm_refresh(self):
        await asyncio.sleep(random.uniform(0.001, 0.01))
        async with self._lock:
            return self.mm.refresh_quotes()

    async def print_summary(self, label: str, recent_trades: int = 5):
        async with self._lock:
            print(f"\n=== {label} ===")
            self.exchange.show_market_summary()
            self.exchange.show_recent_trades(recent_trades)

    async def print_metrics(self, label: str = "METRICS"):
        async with self._lock:
            print(f"\n=== {label} ===")
            self.exchange.show_metrics()


async def trader_loop(sim: AsyncSimulation, trader: RandomTrader, stop: asyncio.Event):
    while not stop.is_set():
        await sim.run_trader_order(trader)
        await asyncio.sleep(random.uniform(0.005, 0.02))


async def market_maker_loop(sim: AsyncSimulation, interval: float, stop: asyncio.Event):
    while not stop.is_set():
        await sim.run_mm_refresh()
        await asyncio.sleep(interval)


async def metrics_loop(sim: AsyncSimulation, interval: float, stop: asyncio.Event):
    tick = 0
    while not stop.is_set():
        await asyncio.sleep(interval)
        if stop.is_set():
            break
        tick += 1
        await sim.print_summary(f"Live tick {tick}")
        await sim.print_metrics()


async def market_clock(stop: asyncio.Event, open_seconds: float):
    await asyncio.sleep(open_seconds)
    stop.set()


def spawn_session_tasks(
    sim: AsyncSimulation,
    stop: asyncio.Event,
    *,
    mm_interval: float = 0.10,
    open_seconds: Optional[float] = None,
    metrics_interval: Optional[float] = None,
) -> List[asyncio.Task]:
    """Start trader + maker loops, plus an optional clock and metrics printer."""
    tasks = [
        asyncio.create_task(
            market_maker_loop(sim, mm_interval, stop), name="market-maker"
        )
    ]
    if open_seconds is not None:
        tasks.append(
            asyncio.create_task(market_clock(stop, open_seconds), name="market-clock")
        )
    if metrics_interval is not None:
        tasks.append(
            asyncio.create_task(metrics_loop(sim, metrics_interval, stop), name="metrics")
        )
    for index, trader in enumerate(sim.traders):
        tasks.append(
            asyncio.create_task(trader_loop(sim, trader, stop), name=f"trader-{index}")
        )
    return tasks


async def run_session_until_stopped(
    sim: AsyncSimulation, stop: asyncio.Event, *, mm_interval: float = 0.10
):
    """Run until ``stop`` is set — used by the server's lifespan."""
    tasks = spawn_session_tasks(
        sim, stop, mm_interval=mm_interval, open_seconds=None, metrics_interval=None
    )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _session_results(sim: AsyncSimulation, open_seconds: float, elapsed: float) -> dict:
    metrics = sim.exchange.metrics
    return {
        "market_open_sec": open_seconds,
        "elapsed_sec": round(elapsed, 3),
        "orders_submitted": metrics.orders_submitted,
        "total_trades": metrics.total_trades,
        "total_volume": metrics.total_volume,
        "cancellations": metrics.cancellations,
        "fill_rate_pct": round(metrics.fill_rate * 100, 2),
        "average_spread": (
            round(metrics.average_spread, 4)
            if metrics.average_spread is not None
            else None
        ),
        "resting_orders": len(sim.exchange.book),
    }


async def run_market_session(
    open_seconds: float = 10.0,
    mm_interval: float = 0.10,
    metrics_interval: float = 2.0,
    mm_type: str = "simple",
    print_final: bool = True,
) -> dict:
    """Open the market for ``open_seconds`` with all participants running at once."""
    sim = AsyncSimulation(mm_type=mm_type)
    stop = asyncio.Event()
    started = time.perf_counter()

    print("=" * 50)
    print(f"MARKET OPEN ({open_seconds}s, {mm_type} maker)")
    print("=" * 50)

    tasks = spawn_session_tasks(
        sim,
        stop,
        mm_interval=mm_interval,
        open_seconds=open_seconds,
        metrics_interval=metrics_interval,
    )
    await asyncio.gather(*tasks)

    elapsed = time.perf_counter() - started
    print("\n" + "=" * 50)
    print("MARKET CLOSED")
    print("=" * 50)

    if print_final:
        await sim.print_summary("Final market state")
        await sim.print_metrics("Final session metrics")

    async with sim._lock:
        sim.exchange.book.validate()
        return _session_results(sim, open_seconds, elapsed)


def main() -> None:
    results = asyncio.run(
        run_market_session(open_seconds=20.0, mm_interval=0.10, metrics_interval=10.0)
    )
    print("\n--- Session results ---")
    for key, value in results.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
