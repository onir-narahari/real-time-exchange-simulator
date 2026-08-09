"""Read benchmark JSON and turn it into something you can act on.

Two modes:

    python -m analysis.analyze results/*.json
        Side-by-side summary of every run.

    python -m analysis.analyze results/new.json --baseline results/old.json
        Per-workload deltas against a baseline, with regressions flagged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Sequence

if __package__ in (None, ""):  # allow `python analysis/analyze.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: A run is a regression if it moves this much in the wrong direction.
REGRESSION_PCT = 10.0

def _latency_metric(kind: str, key: str, label: str):
    return (label, lambda r: _lat(r, kind, key), False)


#: (label, extractor, "higher is better"?) — the full frozen metric set.
METRICS = (
    ("throughput/s", lambda r: r["throughput_ops_sec"], True),
    _latency_metric("submit", "p50_us", "submit p50"),
    _latency_metric("submit", "p95_us", "submit p95"),
    _latency_metric("submit", "p99_us", "submit p99"),
    _latency_metric("submit", "p99_9_us", "submit p99.9"),
    _latency_metric("submit", "max_us", "submit max"),
    _latency_metric("cancel", "p50_us", "cancel p50"),
    _latency_metric("cancel", "p95_us", "cancel p95"),
    _latency_metric("cancel", "p99_us", "cancel p99"),
    _latency_metric("cancel", "p99_9_us", "cancel p99.9"),
    _latency_metric("cancel", "max_us", "cancel max"),
)


def _lat(run: dict, kind: str, key: str) -> Optional[float]:
    stats = run.get("latency", {}).get(kind, {})
    if not stats.get("count"):
        return None
    return stats.get(key)


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------


def load_runs(path: str) -> List[dict]:
    """Accept either a single-run file or one holding a ``runs`` list."""
    with open(path) as handle:
        payload = json.load(handle)

    runs = payload["runs"] if "runs" in payload else [payload]
    for run in runs:
        run["_source"] = os.path.basename(path)
        run["_kind"] = payload.get("kind", "adhoc")
        run["_config"] = payload.get("config", {})
        run["_environment"] = payload.get("environment", {})
    return runs


def load_all(paths: Sequence[str]) -> List[dict]:
    runs: List[dict] = []
    for path in paths:
        if not os.path.exists(path):
            raise SystemExit(f"no such file: {path}")
        runs.extend(load_runs(path))
    if not runs:
        raise SystemExit("no runs loaded")
    return runs


def by_workload(runs: Sequence[dict]) -> Dict[str, dict]:
    return {run["workload"]: run for run in runs}


# ----------------------------------------------------------------------
# Formatting
# ----------------------------------------------------------------------


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else "…" + text[-(width - 1) :]


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _delta(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / old * 100.0


def _print_provenance(runs: Sequence[dict], width: int) -> None:
    """A baseline is only comparable if you know what produced it."""
    config = runs[0].get("_config") or {}
    env = runs[0].get("_environment") or {}
    if not config and not env:
        return
    print("-" * width)
    if config:
        print(
            f"  {config.get('ops', '?'):,} ops x {config.get('repeats', 1)} repeats"
            f" ({config.get('warmup', 0)} warmup discarded), seed {config.get('seed', '?')}"
        )
    if env:
        print(
            f"  {env.get('implementation', '?')} {env.get('python', '?')} on "
            f"{env.get('machine', '?')}  |  git {env.get('git_revision') or 'unknown'}"
            f"  |  {env.get('recorded_at', '?')}"
        )


def summarize(runs: Sequence[dict]) -> None:
    label_width = 16
    col_width = max(14, max(len(r["workload"]) for r in runs) + 2)
    total_width = label_width + col_width * len(runs)

    print()
    print("=" * total_width)
    print("BENCHMARK SUMMARY")
    print("=" * total_width)

    header = f"{'':<{label_width}}" + "".join(
        f"{r['workload']:>{col_width}}" for r in runs
    )
    print(header)
    sources = "".join(
        f"{_truncate(r['_source'], col_width - 1):>{col_width}}" for r in runs
    )
    print(f"{'source':<{label_width}}{sources}")
    _print_provenance(runs, total_width)
    print("-" * total_width)

    rows = [
        ("ops", lambda r: r["operations"]["total"]),
        ("repeats", lambda r: r.get("repeats", 1)),
        ("tput spread %", lambda r: r.get("throughput_spread_pct")),
        ("runtime s", lambda r: r["runtime_sec"]),
        ("trades", lambda r: r["market"]["trades"]),
        ("volume", lambda r: r["market"]["volume"]),
        ("resting orders", lambda r: r["final_book"]["resting_orders"]),
        ("bid levels", lambda r: r["final_book"]["bid_levels"]),
        ("ask levels", lambda r: r["final_book"]["ask_levels"]),
    ]
    for label, extract in rows:
        line = f"{label:<{label_width}}"
        for run in runs:
            line += f"{_fmt(extract(run)):>{col_width}}"
        print(line)

    print("-" * total_width)
    for label, extract, _ in METRICS:
        line = f"{label:<{label_width}}"
        for run in runs:
            line += f"{_fmt(extract(run)):>{col_width}}"
        print(line)

    slow = [(r["workload"], len(r.get("slow_ops", []))) for r in runs]
    flagged = [f"{name} ({count})" for name, count in slow if count]
    print("-" * total_width)
    print(f"{'slow ops':<{label_width}}{', '.join(flagged) if flagged else 'none'}")
    print()


def compare(new_runs: Sequence[dict], baseline_runs: Sequence[dict]) -> int:
    """Print per-workload deltas. Returns the number of regressions found."""
    baseline = by_workload(baseline_runs)
    regressions = 0
    width = 76

    print()
    print("=" * width)
    print("REGRESSION CHECK")
    print(f"baseline: {baseline_runs[0]['_source']}   current: {new_runs[0]['_source']}")
    print("=" * width)

    for run in new_runs:
        name = run["workload"]
        old = baseline.get(name)
        if old is None:
            print(f"\n{name}: not in baseline — skipped")
            continue

        print(f"\n{name}")
        print(
            f"  {'metric':<16}{'baseline':>14}{'current':>14}"
            f"{'delta':>12}{'':>6}"
        )
        print("  " + "-" * (width - 4))

        for label, extract, higher_is_better in METRICS:
            new_value = extract(run)
            old_value = extract(old)
            if new_value is None and old_value is None:
                continue

            change = _delta(new_value, old_value)
            if change is None:
                verdict, change_text = "", "—"
            else:
                improved = change > 0 if higher_is_better else change < 0
                degraded = abs(change) >= REGRESSION_PCT and not improved
                verdict = "REGRESSED" if degraded else ("better" if improved else "")
                change_text = f"{change:+.1f}%"
                regressions += degraded

            print(
                f"  {label:<16}{_fmt(old_value):>14}{_fmt(new_value):>14}"
                f"{change_text:>12}  {verdict:<9}"
            )

    print()
    print("=" * width)
    if regressions:
        print(f"{regressions} regression(s) beyond {REGRESSION_PCT:g}%")
    else:
        print(f"no regressions beyond {REGRESSION_PCT:g}%")
    print()
    return regressions


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize and compare engine benchmark results."
    )
    parser.add_argument("paths", nargs="+", help="benchmark JSON file(s)")
    parser.add_argument(
        "--baseline",
        help="compare paths against this JSON and flag regressions",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit non-zero if a regression is found (for CI)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    runs = load_all(args.paths)

    if args.baseline:
        regressions = compare(runs, load_all([args.baseline]))
        if regressions and args.fail_on_regression:
            raise SystemExit(1)
        return

    summarize(runs)


if __name__ == "__main__":
    main()
