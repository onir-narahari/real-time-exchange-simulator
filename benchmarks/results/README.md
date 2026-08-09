# Benchmark results

Curated runs, tracked so the README's numbers are reproducible and so
`analysis/analyze.py` has a baseline to diff against.

| File | Run |
|---|---|
| `baseline_100k.json` | All five workloads, 100,000 ops, seed 42, default settings |
| `baseline_100k.csv` | The same run flattened to one row per workload |
| `baseline_100k_nogc.json` | Identical, with the cyclic collector disabled — isolates GC pauses in the latency tail |

## Regenerate

```bash
python -m benchmarks.benchmark --workload all --ops 100000 --seed 42 --quiet \
    --json results/baseline_100k.json --csv results/baseline_100k.csv

python -m benchmarks.benchmark --workload all --ops 100000 --seed 42 --no-gc --quiet \
    --json results/baseline_100k_nogc.json
```

## Check a change for regressions

```bash
python -m benchmarks.benchmark --workload all --ops 100000 --seed 42 --quiet \
    --json results/candidate.json

python -m analysis.analyze results/candidate.json \
    --baseline results/baseline_100k.json --fail-on-regression
```

Absolute throughput is machine-dependent; the p50 latencies and the
workload-to-workload *ordering* are the stable signals. Ad-hoc output belongs in
`results/scratch/`, which is gitignored.
