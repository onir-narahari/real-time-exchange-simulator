"""
Async continuous market session.

While the market is open, traders, market maker, and metrics all run concurrently.
Exchange matching stays synchronous; asyncio.Lock ensures one book update at a time.
"""

import asyncio
import random
import time
from typing import Optional

from RandomTraders import RandomTrader, _new_simulation


class AsyncSimulation:
    def __init__(self):
        self.exchange, self.mm, self.traders = _new_simulation()
        self._lock = asyncio.Lock()

    async def run_trader_order(self, trader: RandomTrader):
        await asyncio.sleep(random.uniform(0.001, 0.015))
        async with self._lock:
            return trader.submit_random_limit()

    async def run_mm_refresh(self):
        await asyncio.sleep(random.uniform(0.001, 0.01))
        async with self._lock:
            return self.mm.refresh_quotes()

    async def print_summary(self, label: str, recent_trades: int = 5, recent_events: int = 10):
        async with self._lock:
            print(f"\n=== {label} ===")
            self.exchange.show_market_summary()
            self.exchange.show_recent_trades(recent_trades)
            self.exchange.show_recent_events(recent_events)

    async def print_metrics(self, label: str = "METRICS"):
        async with self._lock:
            print(f"\n=== {label} ===")
            self.exchange.show_metrics()


async def trader_loop(sim: AsyncSimulation, trader: RandomTrader, stop: asyncio.Event):
    """Submit orders continuously until the market closes."""
    while not stop.is_set():
        await sim.run_trader_order(trader)
        await asyncio.sleep(random.uniform(0.005, 0.02))


async def market_maker_loop(sim: AsyncSimulation, interval: float, stop: asyncio.Event):
    """Refresh quotes on an interval while the market is open."""
    while not stop.is_set():
        await sim.run_mm_refresh()
        await asyncio.sleep(interval)


async def metrics_loop(sim: AsyncSimulation, interval: float, stop: asyncio.Event):
    """Print market summary + metrics on an interval while open."""
    tick = 0
    while not stop.is_set():
        await asyncio.sleep(interval)
        if stop.is_set():
            break
        tick += 1
        await sim.print_summary(f"Live tick {tick}")
        await sim.print_metrics()


async def market_clock(stop: asyncio.Event, open_seconds: float):
    """Close the market after open_seconds."""
    await asyncio.sleep(open_seconds)
    stop.set()


def spawn_session_tasks(
    sim: AsyncSimulation,
    stop: asyncio.Event,
    *,
    mm_interval: float = 0.10,
    open_seconds: Optional[float] = None,
    metrics_interval: Optional[float] = None,
):
    """
    Start trader + market-maker loops (and optional clock / metrics printer).

    If open_seconds is None, the session runs until stop is set externally.
    If metrics_interval is None, no console metrics loop is started.
    """
    tasks = [
        asyncio.create_task(
            market_maker_loop(sim, mm_interval, stop), name="market-maker"
        ),
    ]
    if open_seconds is not None:
        tasks.append(
            asyncio.create_task(
                market_clock(stop, open_seconds), name="market-clock"
            )
        )
    if metrics_interval is not None:
        tasks.append(
            asyncio.create_task(
                metrics_loop(sim, metrics_interval, stop), name="metrics"
            )
        )
    for i, trader in enumerate(sim.traders):
        tasks.append(
            asyncio.create_task(trader_loop(sim, trader, stop), name=f"trader-{i}")
        )
    return tasks


async def run_session_until_stopped(
    sim: AsyncSimulation,
    stop: asyncio.Event,
    *,
    mm_interval: float = 0.10,
):
    """Run simulation tasks until stop is set (for background server use)."""
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


def _session_results(sim: AsyncSimulation, open_seconds: float, elapsed: float):
    m = sim.exchange.metrics
    return {
        "market_open_sec": open_seconds,
        "elapsed_sec": round(elapsed, 3),
        "orders_submitted": m.orders_submitted,
        "total_trades": m.total_trades,
        "total_volume": m.total_volume,
        "cancellations": m.cancellation_count,
        "fill_rate_pct": round(m.fill_rate * 100, 2),
        "average_spread": (
            round(m.average_spread, 4) if m.average_spread is not None else None
        ),
        "events": sim.exchange.event_count,
    }


async def run_market_session(
    open_seconds: float = 10.0,
    mm_interval: float = 0.10,
    metrics_interval: float = 2.0,
    print_final: bool = True,
):
    """
    Open the market for open_seconds. While open, all participants run at once:

      - random traders (continuous order flow)
      - market maker (quote refresh loop)
      - metrics reporter (periodic summaries)

    Increase open_seconds later for longer stress tests.
    """
    random.seed()
    sim = AsyncSimulation()
    stop = asyncio.Event()
    started = time.perf_counter()

    print("=" * 50)
    print(f"MARKET OPEN  ({open_seconds}s)")
    print("=" * 50)
    print(
        f"  traders: {len(sim.traders)} (concurrent)\n"
        f"  mm refresh every {mm_interval}s\n"
        f"  metrics every {metrics_interval}s\n"
        f"  exchange: lock-protected (one update at a time)"
    )

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
        return _session_results(sim, open_seconds, elapsed)


# Back-compat alias
async def run_async_sim(
    steps_per_trader: int = 50,
    mm_interval: float = 0.10,
    metrics_interval: float = 0.5,
    print_final: bool = True,
):
    """Legacy name: runs a timed session (~steps_per_trader * 0.02s heuristic)."""
    duration = max(5.0, steps_per_trader * 0.025 * 3)
    return await run_market_session(
        open_seconds=duration,
        mm_interval=mm_interval,
        metrics_interval=metrics_interval,
        print_final=print_final,
    )


def main():
    results = asyncio.run(
        run_market_session(
            open_seconds=120.0,
            mm_interval=0.10,
            metrics_interval=10.0,
        )
    )
    print("\n--- Session results ---")
    for key, value in results.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
