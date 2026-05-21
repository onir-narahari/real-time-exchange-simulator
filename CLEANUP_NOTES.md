# Benchmark Cleanup Notes

## What changed

- Created `results/final/` for curated README benchmark outputs.
- Created `results/old/` for archived local test runs.
- Moved root-level `benchmark_*.csv`, `benchmark_*.json`, `benchmark_results.csv`, and `benchmark_results.json` into `results/old/` (not deleted).
- Added `scripts/run_final_benchmarks.sh` and `scripts/clean_benchmarks.sh`.
- Updated `.gitignore` to ignore generated root benchmark clutter and `results/old/`, while keeping `results/final/` tracked.

## Final results kept (committed)

| File | Description |
|------|-------------|
| `results/final/simple_100k_bestlevel.csv` | Simple MM, 100k orders, seed 42 |
| `results/final/simple_100k_bestlevel.json` | Same run, full JSON detail |
| `results/final/inventory_100k_eventclean.csv` | Inventory MM after event/book-update cleanup |
| `results/final/inventory_100k_eventclean.json` | Same run, full JSON detail |

Source for initial copy (May 2026):

- Simple: from `benchmark_100k_bestlevel.*` (archived in `results/old/`)
- Inventory: from `benchmark_100k_inventory_run2.*` (archived in `results/old/`)

## Gitignore policy

**Ignored:**

- `benchmark_*.csv`, `benchmark_*.json` at repo root
- `benchmark_results.csv`, `benchmark_results.json` at repo root
- `results/old/`
- Python/Node caches, `.env`, logs, `dashboard/dist/`, etc.

**Tracked:**

- `results/final/*.csv`, `results/final/*.json`
- `results/README.md`, `results/final/README.md`

## Regenerate benchmarks

```bash
bash scripts/run_final_benchmarks.sh
```

## Move new root clutter

```bash
bash scripts/clean_benchmarks.sh
```

## Reminder

Do not commit random `benchmark_*.csv` / `benchmark_*.json` files at the repository root. Use `results/final/` for presentation outputs or `results/old/` for local archives.
