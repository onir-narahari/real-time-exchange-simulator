# Exchange Simulator

A Python limit-order-book exchange simulator with synchronous matching, optional async/WebSocket market feed, React dashboard, and a throughput benchmark harness.

## Features

- Price-level order book with FIFO queues per level and O(1) cancel
- Direct best-price matching engine
- Simple and inventory-aware market makers
- Event-driven lifecycle (`NEW_ORDER`, `ORDER_ADDED`, `TRADE_EXECUTED`, fills, cancellations)
- FastAPI + WebSocket server for live snapshots
- React dashboard for top-of-book, trades, and metrics
- Benchmark mode with timing breakdown, slow-path tracing, and MM metrics

## Quick start

### Benchmark (no UI)

```bash
python benchmark.py --orders 10000 --seed 42 --mm-every 5 --mm-type inventory
```

### Live server + dashboard

```bash
uvicorn server:app --reload
```

Build and serve the dashboard from `dashboard/` (see `dashboard/package.json`).

### Tests

```bash
python -m pytest test_order_book.py -q
```

## Project layout

| Path | Purpose |
|------|---------|
| `exchange.py` | Order submission, matching orchestration, events |
| `order_book.py` | Price-level book |
| `matching_engine.py` | Best-price matching |
| `RandomTraders.py` | Random traders + market makers |
| `benchmark.py` | Throughput/latency benchmark runner |
| `server.py` | WebSocket market feed |
| `dashboard/` | React UI |
| `results/final/` | Curated benchmark outputs for README |
| `scripts/` | Regenerate and cleanup helpers |

## Reproducing final benchmarks

Run both curated 100k runs (simple MM + inventory-aware MM):

```bash
bash scripts/run_final_benchmarks.sh
```

Outputs are written to:

`results/final/`

To move ad-hoc `benchmark_*.csv` / `benchmark_*.json` files from the repo root into an archive:

```bash
bash scripts/clean_benchmarks.sh
```

See `results/README.md` and `results/final/README.md` for details.

## Performance evolution (100k orders, seed 42, MM every 5)

| Version | 100k Runtime | P99 Latency | Max Latency | Key change |
|---|--:|--:|--:|---|
| Original baseline | ~1022s | ~14.2ms | ~534ms | Flat matching + heavy event/book churn |
| Best-level matching / simple MM | ~162.6s | ~3.58ms | ~96.6ms | Direct best-price matching |
| Inventory MM before event cleanup | ~164.5s | ~4.98ms | ~656ms | Inventory-aware quoting |
| Inventory MM after event cleanup | ~23.3s | ~0.097ms | ~24.5ms | Bounded recent events + suppressed benchmark `BOOK_UPDATED` payloads |

Curated result files: `results/final/simple_100k_bestlevel.*` and `results/final/inventory_100k_eventclean.*`

## Market maker comparison (100k, seed 42)

| MM type | Runtime | Trades | Cancels | Final inventory | Max abs inventory | PnL | Notes |
|---|--:|--:|--:|--:|--:|--:|---|
| Simple MM | ~162.6s | ~77,978 | ~37,584 | N/A | N/A | N/A | Optimized matching baseline |
| Inventory MM | ~23.3s | ~79,788 | ~27,809 | -2 | 16 | ~$5,547 | Inventory-controlled; fewer cancels; bounded event overhead |

> **Note:** Benchmark mode suppresses heavy `BOOK_UPDATED` payload construction to measure the exchange engine hot path. Server/dashboard mode still streams market data through the WebSocket layer.

## Inventory MM benchmark details (event-clean run)

From `results/final/inventory_100k_eventclean.json`:

- Recent events stored: 10,000 (bounded deque)
- `BOOK_UPDATED` emitted in benchmark mode: 0
- `BOOK_UPDATED` skipped: ~201,780
- `event_count` (total lifecycle events): ~414,118

## License

MIT (or your chosen license — update as needed).
