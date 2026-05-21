"""
Lightweight perf_counter_ns timing buckets for benchmark profiling.

Enable via TimingCollector.enable() before a benchmark run; instrumentation
in exchange / market maker is a no-op when disabled.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional

SUBMIT_SUB_BUCKETS = (
    "submit_validation",
    "submit_matching_loop",
    "submit_book_insert",
    "submit_fill_processing",
    "submit_book_events",
    "submit_cleanup",
)

BUCKET_ORDER = (
    "submit_order",
    *SUBMIT_SUB_BUCKETS,
    "cancel_order",
    "mm_refresh",
    "event_emit",
    "book_summary",
)


@dataclass
class BucketStats:
    name: str
    total_ns: int
    sample_count: int

    @property
    def total_ms(self) -> float:
        return self.total_ns / 1_000_000.0

    @property
    def avg_ms(self) -> float:
        if self.sample_count == 0:
            return 0.0
        return self.total_ms / self.sample_count

    def runtime_pct(self, runtime_ns: int) -> float:
        if runtime_ns <= 0:
            return 0.0
        return (self.total_ns / runtime_ns) * 100.0


class TimingCollector:
    """Process-wide active collector (single-threaded benchmark use)."""

    _active: Optional["TimingCollector"] = None

    def __init__(self) -> None:
        self._totals: Dict[str, int] = {}
        self._counts: Dict[str, int] = {}

    @classmethod
    def enable(cls) -> "TimingCollector":
        collector = cls()
        cls._active = collector
        return collector

    @classmethod
    def disable(cls) -> None:
        cls._active = None

    @classmethod
    def get(cls) -> Optional["TimingCollector"]:
        return cls._active

    def reset(self) -> None:
        self._totals.clear()
        self._counts.clear()

    def record(self, bucket: str, elapsed_ns: int) -> None:
        self._totals[bucket] = self._totals.get(bucket, 0) + elapsed_ns
        self._counts[bucket] = self._counts.get(bucket, 0) + 1

    def snapshot(self) -> List[BucketStats]:
        names = list(BUCKET_ORDER)
        for name in self._totals:
            if name not in names:
                names.append(name)
        return [
            BucketStats(name, self._totals.get(name, 0), self._counts.get(name, 0))
            for name in names
            if self._counts.get(name, 0)
        ]


@contextmanager
def timed(bucket: str):
    """Record elapsed ns into *bucket* when a collector is active."""
    collector = TimingCollector.get()
    slow_logger = None
    if collector is not None:
        try:
            from slow_path import SlowPathLogger

            slow_logger = SlowPathLogger.get()
        except ImportError:
            slow_logger = None

    if collector is None and slow_logger is None:
        yield
        return

    if slow_logger is not None and bucket == "mm_refresh":
        slow_logger.enter_mm_refresh()
    start = time.perf_counter_ns()
    try:
        yield
    finally:
        elapsed_ns = time.perf_counter_ns() - start
        if collector is not None:
            collector.record(bucket, elapsed_ns)
        if slow_logger is not None:
            slow_logger.record_if_slow(bucket, elapsed_ns)
            if bucket == "mm_refresh":
                slow_logger.exit_mm_refresh()


def print_timing_breakdown(
    label: str,
    buckets: List[BucketStats],
    runtime_sec: float,
) -> None:
    """Print ASCII timing table (percentages vs wall-clock runtime)."""
    runtime_ns = int(runtime_sec * 1_000_000_000)
    width = 88
    print()
    print("=" * width)
    print(f"TIMING BREAKDOWN — {label}")
    print("=" * width)
    header = (
        f"{'Bucket':<22}{'Total ms':>12}{'Avg ms':>12}"
        f"{'Samples':>12}{'Runtime %':>12}"
    )
    print(header)
    print("-" * width)
    for b in buckets:
        label = b.name
        if label in SUBMIT_SUB_BUCKETS:
            label = f"  └ {label}"
        print(
            f"{label:<22}"
            f"{b.total_ms:>12.1f}"
            f"{b.avg_ms:>12.3f}"
            f"{b.sample_count:>12}"
            f"{b.runtime_pct(runtime_ns):>11.1f}%"
        )
    print()
