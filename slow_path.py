"""
Benchmark-only slow-operation tracing.

Enable via SlowPathLogger.enable(exchange, mm) before a benchmark run.
Hooks through timing.timed(); no effect when disabled (server/dashboard).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from order_book import BookInstrumentation

SLOW_OP_THRESHOLD_MS = 25.0

SLOW_BUCKETS = frozenset(
    {
        "submit_order",
        "submit_book_insert",
        "submit_book_events",
        "cancel_order",
        "mm_refresh",
        "event_emit",
        "book_summary",
    }
)

_ORDER_CONTEXT_BUCKETS = frozenset(
    {
        "submit_order",
        "submit_book_insert",
        "submit_book_events",
        "cancel_order",
    }
)


@dataclass
class SlowPathSummary:
    total_slow_events: int
    count_by_bucket: Dict[str, int]
    max_elapsed_by_bucket: Dict[str, float]
    avg_elapsed_by_bucket: Dict[str, float]
    top_10_slowest: List[Dict[str, Any]]


class SlowPathLogger:
    """Records operations exceeding SLOW_OP_THRESHOLD_MS during benchmarks."""

    _active: Optional["SlowPathLogger"] = None

    def __init__(self, exchange: Any, mm: Any = None) -> None:
        self.exchange = exchange
        self.mm = mm
        self.events: List[Dict[str, Any]] = []
        self._mm_refresh_depth = 0

    @classmethod
    def enable(cls, exchange: Any, mm: Any = None) -> "SlowPathLogger":
        logger = cls(exchange, mm)
        cls._active = logger
        return logger

    @classmethod
    def disable(cls) -> None:
        cls._active = None

    @classmethod
    def get(cls) -> Optional["SlowPathLogger"]:
        return cls._active

    def enter_mm_refresh(self) -> None:
        self._mm_refresh_depth += 1

    def exit_mm_refresh(self) -> None:
        self._mm_refresh_depth = max(0, self._mm_refresh_depth - 1)

    @property
    def during_mm_refresh(self) -> bool:
        return self._mm_refresh_depth > 0

    def record_if_slow(self, bucket: str, elapsed_ns: int) -> None:
        if bucket not in SLOW_BUCKETS:
            return
        elapsed_ms = elapsed_ns / 1_000_000.0
        if elapsed_ms < SLOW_OP_THRESHOLD_MS:
            return
        self.events.append(self._build_event(bucket, elapsed_ms))

    def _build_event(self, bucket: str, elapsed_ms: float) -> Dict[str, Any]:
        book = self.exchange.order_book
        best_bid = book.best_bid_price()
        best_ask = book.best_ask_price()
        spread = (
            round(best_ask - best_bid, 4)
            if best_bid is not None and best_ask is not None
            else None
        )

        event: Dict[str, Any] = {
            "bucket": bucket,
            "elapsed_ms": round(elapsed_ms, 4),
            "during_mm_refresh": self.during_mm_refresh,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "bid_depth": book.get_bid_depth(),
            "ask_depth": book.get_ask_depth(),
            "bid_price_levels": len(book.bid_prices),
            "ask_price_levels": len(book.ask_prices),
            "total_active_orders": len(book.orders_by_id),
            "price_level_creates": BookInstrumentation.price_level_creates,
            "price_level_removes": BookInstrumentation.price_level_removes,
            "cancel_full_scans": BookInstrumentation.cancel_full_scans,
            "index_rebuilds": BookInstrumentation.index_rebuilds,
            "list_removals": BookInstrumentation.list_removals,
            "order_id": None,
            "side": None,
            "price": None,
            "quantity": None,
            "mm_inventory": None,
            "quote_replacements": None,
            "quote_skips": None,
            "event_count": getattr(self.exchange, "event_count", None),
            "recent_event_count": len(self.exchange.event_history),
            "book_update_count": None,
            "book_update_events_emitted": None,
            "book_update_events_skipped": None,
            "book_event_payload_builds": None,
            "mm_side_replaced": None,
            "replacement_reason": None,
        }

        m = self.exchange.metrics
        event["book_update_count"] = m.book_update_count
        event["book_update_events_emitted"] = m.book_update_events_emitted
        event["book_update_events_skipped"] = m.book_update_events_skipped
        event["book_event_payload_builds"] = m.book_event_payload_builds

        if bucket in _ORDER_CONTEXT_BUCKETS:
            event.update(self._infer_order_context(bucket))

        if self.mm is not None:
            event["mm_inventory"] = getattr(self.mm, "inventory", None)
            event["quote_replacements"] = getattr(self.mm, "quote_replacements", None)
            event["quote_skips"] = getattr(self.mm, "quote_skips", None)
            if bucket == "mm_refresh":
                event["mm_side_replaced"] = getattr(
                    self.mm, "last_refresh_mm_side_replaced", None
                )
                event["replacement_reason"] = getattr(
                    self.mm, "last_refresh_replacement_reason", None
                )

        return event

    def _infer_order_context(self, bucket: str) -> Dict[str, Any]:
        history = self.exchange.event_history
        stored = list(history)
        scan = stored[-80:] if len(stored) > 80 else stored

        if bucket == "cancel_order":
            for ev in reversed(scan):
                if ev.type == "ORDER_CANCELLED":
                    data = ev.data
                    return {
                        "order_id": data.get("order_id"),
                        "side": data.get("side"),
                        "price": data.get("price"),
                        "quantity": data.get("quantity"),
                    }
            return {}

        for ev in reversed(scan):
            if ev.type == "NEW_ORDER":
                data = ev.data
                return {
                    "order_id": data.get("id"),
                    "side": data.get("side"),
                    "price": data.get("price"),
                    "quantity": data.get("quantity"),
                }
        return {}

    def summarize(self) -> SlowPathSummary:
        if not self.events:
            return SlowPathSummary(0, {}, {}, {}, [])

        by_bucket: Dict[str, List[float]] = {}
        for ev in self.events:
            by_bucket.setdefault(ev["bucket"], []).append(ev["elapsed_ms"])

        count_by_bucket = {b: len(vals) for b, vals in by_bucket.items()}
        max_elapsed_by_bucket = {b: max(vals) for b, vals in by_bucket.items()}
        avg_elapsed_by_bucket = {
            b: round(statistics.mean(vals), 4) for b, vals in by_bucket.items()
        }
        top_10 = sorted(self.events, key=lambda e: e["elapsed_ms"], reverse=True)[:10]

        return SlowPathSummary(
            total_slow_events=len(self.events),
            count_by_bucket=count_by_bucket,
            max_elapsed_by_bucket=max_elapsed_by_bucket,
            avg_elapsed_by_bucket=avg_elapsed_by_bucket,
            top_10_slowest=top_10,
        )


def print_slow_path_report(label: str, logger: SlowPathLogger) -> None:
    """Print slow-path summary and top events for one benchmark run."""
    print_slow_path_summary(label, logger.summarize())


def print_slow_path_summary(label: str, summary: SlowPathSummary) -> None:
    """Print slow-path summary (from logger.summarize() or stored benchmark data)."""
    width = 72
    print()
    print("=" * width)
    print(f"SLOW PATH REPORT — {label}")
    print("=" * width)
    print(f"  threshold_ms:          {SLOW_OP_THRESHOLD_MS}")
    print(f"  total_slow_events:   {summary.total_slow_events}")

    if summary.total_slow_events == 0:
        print("  (no operations exceeded threshold)")
        print()
        return

    print()
    print("  count by bucket:")
    for bucket in sorted(summary.count_by_bucket, key=summary.count_by_bucket.get, reverse=True):
        print(f"    {bucket:<22} {summary.count_by_bucket[bucket]:>6}")

    print()
    print("  max elapsed by bucket (ms):")
    for bucket in sorted(
        summary.max_elapsed_by_bucket, key=summary.max_elapsed_by_bucket.get, reverse=True
    ):
        print(
            f"    {bucket:<22} {summary.max_elapsed_by_bucket[bucket]:>10.2f}"
        )

    print()
    print("  avg elapsed by bucket (ms):")
    for bucket in sorted(
        summary.avg_elapsed_by_bucket, key=summary.avg_elapsed_by_bucket.get, reverse=True
    ):
        print(
            f"    {bucket:<22} {summary.avg_elapsed_by_bucket[bucket]:>10.2f}"
        )

    print()
    print("  top 10 slowest events:")
    for i, ev in enumerate(summary.top_10_slowest, 1):
        order_bits = []
        if ev.get("order_id") is not None:
            order_bits.append(f"order_id={ev['order_id']}")
        if ev.get("side") is not None:
            order_bits.append(f"side={ev['side']}")
        if ev.get("price") is not None:
            order_bits.append(f"price={ev['price']}")
        if ev.get("quantity") is not None:
            order_bits.append(f"qty={ev['quantity']}")
        order_str = " ".join(order_bits) if order_bits else "order=n/a"

        print(
            f"    #{i} {ev['bucket']:<20} {ev['elapsed_ms']:>8.2f}ms  "
            f"mm_refresh={ev['during_mm_refresh']}  "
            f"bid/ask={ev['best_bid']}/{ev['best_ask']}  "
            f"spread={ev['spread']}  "
            f"depth={ev['bid_depth']}/{ev['ask_depth']}  "
            f"levels={ev['bid_price_levels']}/{ev['ask_price_levels']}  "
            f"orders={ev['total_active_orders']}"
        )
        print(
            f"        {order_str}  "
            f"inv={ev.get('mm_inventory')}  "
            f"repl={ev.get('quote_replacements')}  "
            f"skips={ev.get('quote_skips')}  "
            f"plc={ev['price_level_creates']} plr={ev['price_level_removes']}  "
            f"cfs={ev['cancel_full_scans']} irb={ev['index_rebuilds']}  "
            f"lrm={ev['list_removals']}"
        )
    print()
