# Real-Time Exchange Simulator

A trading-systems project that simulates a live limit order book exchange, generates random trader and market-maker order flow, matches orders using price-time priority, streams market data over WebSockets, and benchmarks latency, throughput, inventory risk, and market-maker PnL. The system brings together market microstructure, systems engineering, async concurrency, and benchmark-driven performance profiling in a single codebase.

## What It Does

- Simulates an electronic exchange where traders and market makers submit buy and sell orders into a limit order book, producing a continuous stream of bids, asks, trades, fills, and cancellations.
- Matches orders using price-time priority at the best available price and produces trade executions, partial fills, order lifecycle events, spread, depth, and volume metrics.
- Runs an inventory-aware market maker that shifts quote prices and sizes based on position risk, enforces inventory limits, and tracks cash, PnL, and maximum exposure throughout the session.
- Streams live market data to a React dashboard over WebSockets and includes a benchmark suite for measuring throughput, p99/max latency, slow-path tracing, and market-quality analysis.

## Financial and Market-Making Features

- **Limit order book simulation** with resting bids and asks, best bid/ask tracking, spread, and depth on both sides of the book.
- **Trade tape** with timestamped executions, aggressor side, price, quantity, and trade IDs.
- **Fills and cancellations** with lifecycle events: `NEW_ORDER`, `ORDER_ADDED`, `TRADE_EXECUTED`, `ORDER_FILLED`, `ORDER_PARTIALLY_FILLED`, `ORDER_CANCELLED`, and `BOOK_UPDATED`.
- **Simple market maker** that posts symmetric quotes at midpoint and refreshes every N trader orders.
- **Inventory-aware market maker** that skews reservation price by inventory exposure, adjusts bid/ask sizes based on inventory ratio, and avoids quoting on the risky side when inventory nears limits.
- **Position tracking**: inventory, cash, total PnL (`cash + inventory * fair_value`), max absolute inventory, and per-refresh replacement/skip counters.
- **Market quality metrics**: total trades, total volume, cancellations, fill rate, and average spread, computed from lifecycle events.

## Technical Highlights

- **Price-level order book** with FIFO doubly-linked queues per price level, sorted bid/ask price arrays via `bisect.insort`, and an `orders_by_id` hash map for O(1) order lookup.
- **O(1) cancel via node unlinking**: cancellation removes a `BookNode` from its doubly-linked price-level queue and pops it from `orders_by_id` without scanning the book. Empty price levels are cleaned up immediately.
- **Eliminated full-book cancel scans, list removals, and index rebuilds** in the benchmark hot path. `BookInstrumentation` counters confirm zero `cancel_full_scans`, zero `index_rebuilds`, and zero `list_removals` across 100k-order runs.
- **Best-price matching engine** that reads `best_ask_order()` / `best_bid_order()` directly from the top price level instead of flattening the entire book per match attempt.
- **Async market simulation** with concurrent trader loops, market-maker refresh loop, metrics publishing loop, and a market clock, all coordinated via `asyncio`.
- **Lock-protected exchange mutations**: an `asyncio.Lock` serializes all order book updates so matching and book state remain consistent under concurrent trader activity.
- **FastAPI WebSocket feed** with per-client sessions, monotonic sequence numbers, periodic heartbeats, deduped book snapshots, batched trade delivery (capped at 50 per batch), and exponential-backoff reconnection on the client.
- **Benchmark runner** with nanosecond-resolution timing buckets (`perf_counter_ns`), per-operation breakdown (submit, match, insert, fill, cancel, MM refresh), and CSV/JSON export.
- **Slow-path tracing** that records any operation exceeding 25ms with full book-state context (depth, levels, active orders, instrumentation counters, MM inventory) for post-run root-cause analysis.
- **Bounded recent event history** using a `deque(maxlen=10000)` so memory and append cost stay constant regardless of run length.
- **Benchmark-mode `BOOK_UPDATED` suppression**: in benchmark mode, heavy `BOOK_UPDATED` payload construction and event storage are skipped while spread sampling and counters continue. Server/dashboard mode is unaffected.

## Tech Stack

| Area | Tools |
|------|-------|
| Backend | Python 3, FastAPI, WebSockets, `asyncio` |
| Concurrency | `asyncio.Lock`-protected exchange, concurrent trader/MM/metrics tasks |
| Frontend | React 19, TypeScript, Recharts, Vite |
| Simulation | Limit order book, matching engine, random traders, simple and inventory-aware market makers |
| Performance | `perf_counter_ns` timing buckets, slow-path tracing, p50/p95/p99/max latency, `BookInstrumentation` counters |
| Data output | CSV/JSON benchmark exports, curated results in `results/final/` |

## Project Structure

| Path | Purpose |
|------|---------|
| `exchange.py` | Exchange engine: order submission, matching orchestration, lifecycle events, bounded event history |
| `order_book.py` | Price-level order book with FIFO queues, O(1) cancel, depth tracking, health validation |
| `matching_engine.py` | Best-price matching: incoming orders execute against the top opposing price level |
| `RandomTraders.py` | Simple MM, inventory-aware MM, random traders, and `create_market_maker` factory |
| `async_sim.py` | Async simulation loop: concurrent traders, MM refresh, metrics publishing, market clock |
| `server.py` | FastAPI server with `/health` endpoint and `/ws/market` WebSocket feed |
| `benchmark.py` | Throughput/latency benchmark runner with timing buckets, slow-path tracing, and MM metrics reporting |
| `timing.py` | Nanosecond timing collector and bucket breakdown printer |
| `slow_path.py` | Benchmark-only slow-operation logger with book-state snapshots |
| `sim_config.py` | Benchmark vs. server mode toggles (event storage, `BOOK_UPDATED` suppression) |
| `metrics.py` | Simulation metrics: trades, volume, cancellations, fill rate, average spread |
| `dashboard/` | React + TypeScript dashboard: top-of-book, price chart, trade tape, session metrics, status bar |
| `results/final/` | Curated 100k benchmark outputs (simple MM and inventory MM) |
| `scripts/` | `run_final_benchmarks.sh` and `clean_benchmarks.sh` |

## Architecture

```
Random Traders / Inventory-Aware Market Maker
                    |
            Async Simulation Loop
                    |
        Lock-Protected Exchange Engine
                    |
        Price-Level Limit Order Book
                    |
          Best-Price Matching Engine
                    |
    Events / Metrics / Slow-Path Tracing
                    |
          FastAPI WebSocket Feed
                    |
             React Dashboard
```

Traders and market makers generate continuous order flow into the exchange. The async simulation runs trader tasks, market-maker quote refreshes, and metrics publishing as concurrent `asyncio` tasks. All exchange mutations are serialized through an `asyncio.Lock` so the order book is updated one operation at a time. The order book stores resting liquidity organized by price level, with FIFO queues within each level. The matching engine executes incoming orders against the best available opposing price. The WebSocket server streams market snapshots, sequenced book updates, batched trades, session metrics, and heartbeats to connected React dashboard clients.

## Matching Engine

Incoming buy orders match against the lowest resting ask. Incoming sell orders match against the highest resting bid. Within a price level, orders are filled in FIFO order, preserving time priority. Partial fills reduce the resting order's quantity in place; fully filled orders are unlinked from the book. The matching engine reads directly from `best_ask_order()` / `best_bid_order()` on the price-level book, avoiding any full-book flattening in the matching hot path.

## Inventory-Aware Market Maker

The inventory-aware market maker provides two-sided liquidity while controlling position risk. On each refresh, it computes a reservation price that shifts away from fair value in proportion to current inventory, then places bid and ask quotes around that adjusted center.

**Quote pricing:**

```
fair_value = mid_price
inventory_skew = inventory * inventory_risk_factor
reservation_price = fair_value - inventory_skew

bid = reservation_price - spread / 2
ask = reservation_price + spread / 2
```

**Size adjustment:**

```
inventory_ratio = inventory / max_inventory

bid_size = base_size * (1 - inventory_ratio)
ask_size = base_size * (1 + inventory_ratio)
```

**When inventory is positive (long):** the market maker owns too much. Quotes shift down. Bid size shrinks. Ask size grows. This encourages selling inventory and discourages buying more.

**When inventory is negative (short):** the market maker needs to buy back. Quotes shift up. Bid size grows. Ask size shrinks. This encourages buying inventory and discourages selling more.

**Requote thresholds** prevent unnecessary cancel/repost churn. A resting quote is kept if the desired price is within 3 ticks and the desired size is within 3 units of the current quote, unless the quote age exceeds 50 refreshes or inventory is near the risk limit (80% of max).

**Tracked metrics:** inventory, cash, total PnL, max absolute inventory, quote refreshes, quote replacements, quote skips, quote-kept-due-to-threshold, and per-reason replacement counts (price, size, age, risk).

## Benchmarks

The benchmark suite measures both systems performance and market behavior: runtime, throughput, p50/p95/p99/max latency, trades, volume, cancellations, fill rate, average spread, MM inventory, PnL, and slow-path events.

### Performance Evolution (100k orders, seed 42, MM every 5)

| Version | 100k Runtime | P99 Latency | Max Latency | Key Change |
|---|--:|--:|--:|---|
| Original baseline | ~1,022s | ~14.2ms | ~534ms | Flat matching + heavy event/book churn |
| Best-level matching / Simple MM | ~162.6s | ~3.58ms | ~96.6ms | Direct best-price matching |
| Inventory MM before event cleanup | ~164.5s | ~4.98ms | ~656ms | Inventory-aware quoting |
| Inventory MM after event cleanup | ~23.3s | ~0.097ms | ~24.5ms | Bounded events + suppressed benchmark `BOOK_UPDATED` payloads |

Slow-path tracing showed that max-latency spikes were caused by `mm_refresh` triggering nested `submit_book_insert` and `submit_book_events` operations on a deep book, not the matching loop itself. The fix bounded recent event history to a 10,000-entry deque and suppressed heavy `BOOK_UPDATED` payload construction in benchmark mode, separating engine hot-path measurement from event serialization overhead.

> **Note:** Benchmark mode suppresses `BOOK_UPDATED` payload construction to measure the exchange engine hot path. All lifecycle and trade events are still counted. Server and dashboard mode continues to stream full market data through the WebSocket layer.

### Market Maker Comparison (100k, seed 42)

| MM Type | Runtime | Trades | Cancels | Final Inventory | Max Abs Inventory | PnL | Notes |
|---|--:|--:|--:|--:|--:|--:|---|
| Simple MM | ~162.6s | ~77,978 | ~37,584 | N/A | N/A | N/A | Optimized matching baseline |
| Inventory MM | ~23.3s | ~79,788 | ~27,809 | -2 | 16 | +$5,547 | Inventory-controlled; fewer cancels; bounded event overhead |

Final inventory of -2 means the market maker ended nearly flat. Max absolute inventory of 16 shows inventory risk stayed bounded throughout the 100k-order run.

## Concurrency Model

The simulator separates concurrent market activity from deterministic exchange mutation. Trader tasks, market-maker refresh loops, and metrics publishing run concurrently as `asyncio` tasks, while the exchange engine is protected by an `asyncio.Lock` so order book updates happen one at a time. The WebSocket server runs per-client broadcaster tasks for heartbeats (5s interval), metrics (1s), book snapshots (0.5s, deduped by book key), and batched trades (0.5s, capped at 50 per batch). Clients receive a monotonic sequence number on every message and reconnect with exponential backoff.

## Quick Start

**Run a benchmark:**

```bash
python benchmark.py --orders 10000 --seed 42 --mm-every 5 --mm-type inventory
```

**Regenerate curated final benchmarks:**

```bash
bash scripts/run_final_benchmarks.sh
```

**Run the backend server:**

```bash
uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

**Run the dashboard:**

```bash
cd dashboard
npm install
npm run dev
```

**Run tests:**

```bash
python -m pytest test_order_book.py -q
```

## Benchmark Outputs

Final curated benchmark outputs live in `results/final/`. Raw benchmark outputs generated during local runs are gitignored at the repository root.

To clean root benchmark clutter:

```bash
bash scripts/clean_benchmarks.sh
```

See `results/README.md` and `results/final/README.md` for regeneration instructions and file descriptions.

## Skills Demonstrated

**Trading Systems**
- Limit order book design with price-level organization and FIFO time priority
- Price-time priority matching engine
- Bid/ask spread, depth, fills, cancellations, and trade tape
- Market-maker inventory management, quote skew, and PnL tracking

**Systems Engineering**
- Async simulation with lock-protected shared state
- WebSocket market data feed with sequencing, heartbeats, and batched delivery
- Slow-path tracing for latency root-cause analysis
- Benchmark-driven optimization from ~1,022s to ~23s on 100k orders
- Bounded event storage and observability cleanup

**Full-Stack**
- FastAPI backend with health checks and WebSocket endpoints
- React + TypeScript dashboard with live price chart, trade tape, top-of-book, and session metrics
- CSV/JSON benchmark exports with curated and reproducible results
- Shell scripts for benchmark regeneration and cleanup

## Future Work

- Historical market data replay for backtesting against recorded order flow
- Volatility-aware spread widening during high-activity periods
- Detailed backtest reporting with per-trade attribution
- Additional order types (stop, iceberg, fill-or-kill)
- Deployment packaging for hosted demo environments
