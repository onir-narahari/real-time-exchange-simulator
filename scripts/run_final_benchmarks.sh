#!/bin/bash
set -e

mkdir -p results/final

echo "Running final simple MM 100k benchmark..."
python benchmark.py --orders 100000 --seed 42 --mm-every 5 --mm-type simple --json results/final/simple_100k_bestlevel.json --csv results/final/simple_100k_bestlevel.csv

echo "Running final inventory-aware MM 100k benchmark..."
python benchmark.py --orders 100000 --seed 42 --mm-every 5 --mm-type inventory --json results/final/inventory_100k_eventclean.json --csv results/final/inventory_100k_eventclean.csv

echo "Final benchmarks complete. Results saved in results/final/"
