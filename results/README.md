# Benchmark Results

Raw benchmark outputs are generated locally and should not clutter the repository root. Most generated files are ignored by git.

## Final curated results

The benchmark outputs referenced in the project README live in:

`results/final/`

## Regenerate final results

From the repository root:

```bash
bash scripts/run_final_benchmarks.sh
```

This runs the two 100k deterministic benchmarks (simple MM and inventory-aware MM) and writes CSV/JSON into `results/final/`.

## Clean up root benchmark clutter

If benchmark CSV/JSON files appear in the repo root after local runs:

```bash
bash scripts/clean_benchmarks.sh
```

That moves `benchmark_*.csv`, `benchmark_*.json`, `benchmark_results.csv`, and `benchmark_results.json` into `results/old/` without deleting them.

## Archive

Historical test runs are kept under `results/old/` for inspection. That directory is gitignored by default.
