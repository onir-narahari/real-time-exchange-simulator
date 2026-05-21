#!/usr/bin/env bash
# Optional: compare MM refresh rates at 50k orders (default production value is 5).
# Usage: ./run_mm_compare.sh

set -euo pipefail
cd "$(dirname "$0")"

ORDERS=50000
SEED=42

run_one() {
  local n=$1
  echo "=== MM every ${n} trader orders (default: 5) ==="
  python benchmark.py \
    --orders "$ORDERS" \
    --seed "$SEED" \
    --mm-every "$n" \
    --json "benchmark_mm${n}.json" \
    --csv "benchmark_mm${n}.csv"
  echo ""
}

run_one 5
run_one 10
run_one 25

echo "Done. Production default is --mm-every 5."
echo "  benchmark_mm5.json / benchmark_mm10.json / benchmark_mm25.json"
