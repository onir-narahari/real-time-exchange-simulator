# Final Benchmark Results

This folder contains the curated benchmark outputs used in the README.

## Files

### 1. `simple_100k_bestlevel.csv` / `simple_100k_bestlevel.json`

Optimized simple market maker using best-level matching and a price-level order book.

Regenerate:

```bash
python benchmark.py --orders 100000 --seed 42 --mm-every 5 --mm-type simple --json results/final/simple_100k_bestlevel.json --csv results/final/simple_100k_bestlevel.csv
```

### 2. `inventory_100k_eventclean.csv` / `inventory_100k_eventclean.json`

Inventory-aware market maker with bounded recent event history and suppressed heavy `BOOK_UPDATED` payloads in benchmark mode.

Regenerate:

```bash
python benchmark.py --orders 100000 --seed 42 --mm-every 5 --mm-type inventory --json results/final/inventory_100k_eventclean.json --csv results/final/inventory_100k_eventclean.csv
```

## Note

Benchmark runtimes may vary slightly by machine and background load. With the same seed, trade counts and deterministic simulation metrics should stay consistent.
