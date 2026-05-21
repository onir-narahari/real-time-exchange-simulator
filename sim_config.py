"""
Simulation-wide toggles. Benchmark mode adjusts hot-path behavior without
changing matching or lifecycle event semantics.
"""

MAX_EVENT_HISTORY = 10000
STORE_EVENT_HISTORY = True
EMIT_BOOK_UPDATED_EVENTS = True


def configure_benchmark_mode() -> None:
    """Suppress heavy BOOK_UPDATED payloads during benchmark runs."""
    global EMIT_BOOK_UPDATED_EVENTS
    EMIT_BOOK_UPDATED_EVENTS = False


def configure_server_mode() -> None:
    """Default dashboard / WebSocket behavior."""
    global EMIT_BOOK_UPDATED_EVENTS
    EMIT_BOOK_UPDATED_EVENTS = True
