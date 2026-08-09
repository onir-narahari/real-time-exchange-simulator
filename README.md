# Limit Order Book & Matching Engine

A limit order book and price-time-priority matching engine in Python, with a
benchmark harness and a regression-analysis tool built around it.

```
Order  ->  OrderBook  ->  MatchingEngine  ->  Trade
```

That pipeline is the whole project. Strategies, PnL, transport, and dashboards
live outside it, in `sim/`, and the engine has no knowledge of any of them.

```
orderbook/    the engine — no dependencies, no I/O
tests/        unit tests plus a differential suite
reference/    a naive engine used as an answer key, and the comparison harness
benchmarks/   order-flow workloads and the measurement harness
analysis/     summarize runs, diff against a baseline, flag regressions
sim/          strategies, session, WebSocket feed — downstream of the engine
dashboard/    React live view
```

## Quick start

The engine, tests, benchmarks, and analysis need only Python 3.9+ and `pytest`.

```bash
pip install pytest

python -m pytest tests/ -q                                   # 96 tests
python -m reference.differential                             # engine vs reference
python -m benchmarks.benchmark --workload all --ops 100000   # performance
python -m analysis.analyze results/baseline_100k.json        # read results
```

## The engine

`orderbook/` has no third-party dependencies and no I/O.

| File | Responsibility |
|---|---|
| `order.py` | `Order`, `Side`, `OrderType`, `OrderStatus`; construction-time validation |
| `price_level.py` | `PriceLevel` — a FIFO queue of orders at one price, with incremental aggregates |
| `order_book.py` | Price levels, top-of-book, L2 aggregation, O(1) cancel, invariant checking |
| `matching_engine.py` | The sweep loop: turns an incoming order into trades |
| `trade.py` | `Trade` — the engine's output record |

### What it supports

- **LIMIT** — sweeps every crossing level, rests the remainder.
- **MARKET** — sweeps until filled or the book is dry; the remainder expires
  rather than resting, since a market order has no price to rest at.
- **CANCEL** — removes a resting order in O(1).
- **Price-time priority** — best price first, FIFO within a price.
- **L2 book** — aggregated `(price, quantity, order_count)` per level, best-first,
  optionally depth-capped.
- **Trades** — every trade prints at the *resting* order's price, so an
  aggressive limit receives price improvement rather than paying its own limit.

### Design

**Two maps and two sorted arrays.** Each side keeps `{price: PriceLevel}` plus
an ascending array of live prices, so the best bid is `bid_prices[-1]` and the
best ask is `ask_prices[0]` — O(1) top-of-book, O(log n) insertion via `bisect`.

**Intrusive linked lists.** A price level is a doubly-linked FIFO of `BookNode`s.
`orders_by_id` maps an order id straight to its node, so a cancel unlinks in
O(1) with no scan and no index rebuild. Empty levels are collected immediately.

**Nothing is recomputed.** Level quantity, level order count, and per-side depth
are maintained incrementally on every add, fill, and cancel. The book never
flattens itself to answer a question — an L2 snapshot reads cached aggregates,
and `l2(depth=5)` touches only five levels per side.

**No instrumentation in the hot path.** The engine carries no timing hooks, no
event emission, and no global mode flags. Measurement lives in `benchmarks/`
and wraps calls from the outside, so the benchmark measures the code that ships.

**Invariants are executable.** `OrderBook.validate()` walks the entire book and
asserts every invariant — sorted and deduplicated price arrays, no empty levels,
no non-positive resting quantities, aggregate caches matching a full walk, the
index agreeing with the linked lists, and an uncrossed book. It is O(n) and used
by tests and benchmarks, never by the hot path.

## Correctness

96 tests, no third-party fixtures beyond `pytest`.

```bash
python -m pytest tests/ -q
```

| File | Covers |
|---|---|
| `test_orders.py` | Order validation, adding orders, resting, sequence assignment, top-of-book, depth caches, L2 aggregation and depth capping, price-time iteration order |
| `test_matching.py` | Matching, partial fills, price improvement, FIFO within a level, multi-level sweeps, sweeps stopping at the limit price, market-order semantics and expiry, empty-price-level collection, crossed and locked books, quantity conservation, determinism |
| `test_cancellation.py` | Cancel at head/middle/tail, level collection and price reuse, depth and aggregate updates, cancelling filled or unknown orders, cancel after partial fill, 4,000-op churn ending in a provably empty book |
| `test_differential.py` | The optimized engine against a naive reference over randomized flow |

Three unit tests check properties rather than cases:

- `test_quantity_is_conserved_under_random_flow` — over 2,000 random orders,
  every share bought was sold, and submitted minus traded equals resting depth.
- `test_book_survives_heavy_submit_cancel_churn` — after 4,000 mixed ops and
  cancelling everything left, the book must be empty with zero depth, zero
  levels, and `level_creates == level_removes`.
- `test_repeated_crossing_flow_never_leaves_a_crossed_book` — aggressive flow
  alternating sides, asserting the book is uncrossed after every single step.

### Differential testing

Unit tests check the cases I thought of. To cover the cases I did not, the
engine is checked against a second, independent implementation.

```
                        SAME ORDERS
                             |
                    +--------+--------+
                    v                 v
                Reference          Optimized
              naive_engine.py      orderbook/
                    |                 |
                    +--------+--------+
                             v
                          COMPARE
```

`reference/naive_engine.py` is deliberately the dumbest possible engine: the
book is one flat Python list, finding the best price is a linear scan with
`min`/`max`, cancelling is `list.remove`, and an L2 view is regrouped from
scratch every call. Nothing is cached, so nothing can go stale. It expresses
price-time priority directly as a sort key — `(-price, sequence)` for bids,
`(price, sequence)` for asks — and is short enough to verify by reading it.

It imports nothing from `orderbook/`. The two share no code, so the same bug
would have to be written twice, independently, to slip through.

`reference/differential.py` drives both engines through identical randomized
flow and compares, **after every operation**:

| Compared | Detail |
|---|---|
| Trades | id, price, quantity, aggressor side, aggressor id, resting id hit |
| Incoming order | remaining quantity, whether it rested |
| Book (L3) | every resting order, in price-time priority, both sides |
| Book (L2) | `(price, quantity, order_count)` per level, both sides |
| Top of book | best bid, best ask, per-side depth, resting count |

Comparing per-operation rather than at the end means a divergence is reported
at the operation that caused it, with both books printed and a one-line command
to reproduce that exact seed.

Five flow profiles stress different paths — `tight` (five prices, so constant
FIFO ties and crossing), `wide`, `cancel_heavy`, `market_heavy`, and `sweeping`
(orders large enough to eat many levels at once).

```bash
python -m reference.differential                          # default sweep
python -m reference.differential --seeds 300 --ops 1500   # long sweep
python -m reference.differential --profile tight --seeds 1 --seed-start 42
```

**Current status: 300 cases, 180,000 operations, 111,854 trades compared across
all five profiles with no divergence.**

### Proving the harness can fail

A differential suite that passes because it never really looks at anything is
worse than none. Seven known bugs were injected into the optimized engine to
confirm each is caught:

| Injected bug | Caught by |
|---|---|
| LIFO instead of FIFO within a price level | trade comparison |
| Trades print at the aggressor's price, not the resting price | trade comparison |
| Empty price levels never collected | crash on the next sweep |
| `best_bid` returns the worst bid | book state: `best_bid` |
| Depth cache drifts by one on every cancel | book state: `bid_depth` |
| Market-order remainder rests instead of expiring | incoming order rested |
| Resting fill off by one | trade comparison |

Two of these are kept as permanent tests (`test_harness_detects_a_broken_engine`
and `test_harness_detects_a_stale_depth_cache`), so the harness is re-proven
capable of failing on every run.

The depth-cache case is the one that shows why differential testing earns its
keep here: `bid_depth` is an optimization the reference does not have at all.
The reference has nothing to corrupt, so any drift in the optimized engine's
cache shows up immediately as disagreement.

## Benchmarks

```bash
python -m benchmarks.benchmark --workload all --ops 100000 --seed 42
python -m benchmarks.benchmark --workload cancel_heavy --ops 1000000 --json results/run.json
```

`workloads.py` defines deterministic order-flow generators — pure functions of
`(count, seed)` that emit ops with no view of matching outcomes, exactly like a
real client. Cancels are issued against previously submitted ids, so some race a
fill and miss; that miss rate is reported.

| Workload | Shape |
|---|---|
| `balanced` | Symmetric two-sided limit flow; most orders rest, a minority cross |
| `crossing` | Aggressive flow that mostly trades on arrival — stresses the sweep loop |
| `cancel_heavy` | Post/pull/repost churn — the workload the O(1) cancel path exists for |
| `deep_book` | Passive-only flow over a wide price grid; the book grows without bound |
| `market_sweep` | Resting depth punctuated by market orders that eat several levels |

Each operation is timed individually with `perf_counter_ns`. The harness reports
throughput, mean/p50/p90/p99/p99.9/max per operation kind, final book shape,
level churn, and captures book context for any op crossing a slow threshold.

### Results

100,000 operations per workload, seed 42, Python 3.9.11, Apple Silicon.
Latencies in microseconds; full output in `results/baseline_100k.json`.

| Workload | Throughput | Trades | submit p50 | submit p99 | cancel p50 | cancel p99 |
|---|--:|--:|--:|--:|--:|--:|
| `balanced` | 183,313 ops/s | 14,116 | 2.12 | 8.88 | — | — |
| `cancel_heavy` | 256,036 ops/s | 0 | 2.50 | 3.33 | 1.38 | 1.83 |
| `crossing` | 144,360 ops/s | 97,440 | 2.54 | 10.96 | — | — |
| `deep_book` | 181,676 ops/s | 0 | 2.17 | 3.50 | — | — |
| `market_sweep` | 139,039 ops/s | 94,876 | 2.50 | 29.62 | — | — |

Two things these numbers show:

**Cancel is cheaper than submit** (1.38us vs 2.50us at p50), and `cancel_heavy`
is the *fastest* workload despite doing the most book mutation. That is the
O(1) unlink paying off; a book that scans to cancel degrades here first.

**`deep_book` holds its p99 at 3.50us with 100,000 resting orders across ~2,500
levels per side** — insertion cost is flat in book depth, which is what the
sorted-array-plus-map layout is for.

### The latency tail is the garbage collector

Max latency on `balanced` and `deep_book` reaches 12–27ms, far above p99.9. The
slow-op capture shows these spikes correlate with resting-order count, not with
level count or sweep length. Re-running with the cyclic collector disabled
confirms it:

| `deep_book`, 100k ops | p50 | p99 | max |
|---|--:|--:|--:|
| GC on | 2.08 | 4.38 | 12,778 |
| GC off (`--no-gc`) | 2.08 | 3.38 | 945 |

p50 is unchanged and max drops by 93%. The tail is CPython reclaiming a growing
heap of `BookNode` objects, not a data-structure cost — so the fix would be GC
tuning or node pooling, not a different book layout. `--no-gc` exists to make
that separation reproducible rather than asserted.

## Analysis

```bash
python -m analysis.analyze results/baseline_100k.json
python -m analysis.analyze results/new.json --baseline results/baseline_100k.json --fail-on-regression
```

Summarizes runs side by side, or diffs a run against a baseline per workload and
flags anything more than 10% worse on throughput or latency. `--fail-on-regression`
exits non-zero, so it drops into CI as-is.

## Outside the core

`sim/` is the simulation layer. It is deliberately downstream of the engine and
nothing in `orderbook/` imports it.

| File | Purpose |
|---|---|
| `exchange.py` | Session wrapper: order-id allocation, trade tape, metrics |
| `market_maker.py` | Simple and inventory-aware market makers; quote skew, position limits, PnL |
| `traders.py` | Random uninformed order flow |
| `async_sim.py` | Concurrent trader/maker/metrics tasks behind an `asyncio.Lock` |
| `server.py` | FastAPI `/health` and `/ws/market` — sequenced snapshots, batched trades, heartbeats |
| `dashboard/` | React + TypeScript live view (top-of-book, price chart, trade tape, metrics) |

```bash
uvicorn sim.server:app --reload --port 8000    # backend
cd dashboard && npm install && npm run dev     # frontend
python -m sim.async_sim                        # headless session
```

## Known limits

Stated because they bound what the numbers above mean:

- **Prices are floats.** A production book would use integer ticks. Price
  handling is centralized enough that this is a contained change, but it has
  not been made.
- **The simulated flow is uninformed.** `sim/traders.py` is uniform noise with
  no drift, so a maker quoting into it faces essentially no adverse selection.
  Maker PnL from a simulation run is therefore not a meaningful result — it is
  a spread capture against a random walk that does not walk.
- **Single-threaded.** The engine assumes one writer. The async layer enforces
  that with a lock rather than the book being thread-safe.
- **No self-trade prevention, no iceberg/stop/FOK orders, no time-in-force
  beyond the implicit day/IOC split between limit and market.**

## License

MIT
