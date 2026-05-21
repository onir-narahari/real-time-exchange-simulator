#!/bin/bash
set -e

mkdir -p results/old

echo "Moving generated benchmark files from repo root to results/old/..."

shopt -s nullglob 2>/dev/null || true
moved=0
for file in benchmark_*.csv benchmark_*.json benchmark_results.csv benchmark_results.json; do
  if [ -f "$file" ]; then
    mv "$file" results/old/
    moved=$((moved + 1))
  fi
done
echo "Moved ${moved} file(s)."

echo "Benchmark cleanup complete."
