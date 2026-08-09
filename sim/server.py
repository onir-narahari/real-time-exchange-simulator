"""
FastAPI market data server backed by the async exchange simulation.

Run:
    uvicorn server:app --reload

Endpoints:
    GET  /health      — liveness + current market snapshot
    WS   /ws/market   — snapshot, metrics, book, batched trades, heartbeats
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Set, Tuple

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect

from sim.async_sim import AsyncSimulation, run_session_until_stopped

logger = logging.getLogger(__name__)

METRICS_INTERVAL = 1.0
BOOK_INTERVAL = 0.5
TRADE_BATCH_INTERVAL = 0.5
HEARTBEAT_INTERVAL = 5.0
MAX_TRADES_PER_BATCH = 50

BookKey = Tuple[Any, Any, Any, Any, int, int]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mid_price(best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    if best_bid is not None and best_ask is not None:
        return round((best_bid + best_ask) / 2, 2)
    return None


def _book_fields(exchange) -> dict:
    """Top-of-book + depth, straight from the book's caches; hold sim._lock."""
    book = exchange.book
    best_bid = book.best_bid()
    best_ask = book.best_ask()
    spread = book.spread()
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(spread, 2) if spread is not None else None,
        "mid_price": _mid_price(best_bid, best_ask),
        "bid_depth": book.bid_depth,
        "ask_depth": book.ask_depth,
    }


def _metrics_fields(exchange) -> dict:
    """Metrics + book fields; caller must hold sim._lock."""
    fields = dict(_book_fields(exchange))
    m = exchange.metrics
    avg_spread = m.average_spread
    fields.update(
        {
            "total_trades": m.total_trades,
            "total_volume": m.total_volume,
            "orders_submitted": m.orders_submitted,
            "cancellations": m.cancellations,
            "orders_fully_filled": m.orders_fully_filled,
            "fill_rate_pct": round(m.fill_rate * 100, 2),
            "average_spread": round(avg_spread, 4) if avg_spread is not None else None,
            "resting_orders": len(exchange.book),
        }
    )
    return fields


def _build_snapshot(exchange) -> dict:
    """Full snapshot; caller must hold sim._lock."""
    return _metrics_fields(exchange)


def _book_key(book: dict) -> BookKey:
    return (
        book["best_bid"],
        book["best_ask"],
        book["spread"],
        book["mid_price"],
        book["bid_depth"],
        book["ask_depth"],
    )


def _trade_payload(trade) -> dict:
    ts = trade.timestamp
    if hasattr(ts, "isoformat"):
        ts = ts.isoformat()
    return {
        "trade_id": trade.trade_id,
        "price": trade.price,
        "quantity": trade.quantity,
        "side": trade.side,
        "trade_time": ts,
    }


def _ws_message(msg_type: str, seq: int, data: dict) -> str:
    payload = {
        "seq": seq,
        "timestamp": _utc_now_iso(),
        "type": msg_type,
        "data": data,
    }
    return json.dumps(payload, default=str)


async def _read_snapshot(sim: AsyncSimulation) -> dict:
    async with sim._lock:
        return _build_snapshot(sim.exchange)


class MarketWsSession:
    """Per-client WebSocket session: seq, broadcasters, clean shutdown."""

    def __init__(
        self,
        websocket: WebSocket,
        sim: AsyncSimulation,
        clients: Set["MarketWsSession"],
        session_running: Callable[[], bool],
    ):
        self.websocket = websocket
        self.sim = sim
        self.clients = clients
        self.session_running = session_running
        self.stop = asyncio.Event()
        self._seq = 0
        self._seq_lock = asyncio.Lock()
        self.last_trade_id_sent = 0
        self._last_book_key: Optional[BookKey] = None
        self._broadcaster_tasks: list[asyncio.Task] = []

    @property
    def connected_clients(self) -> int:
        return len(self.clients)

    async def send(self, msg_type: str, data: dict) -> None:
        if self.stop.is_set():
            return
        async with self._seq_lock:
            self._seq += 1
            seq = self._seq
        try:
            await self.websocket.send_text(_ws_message(msg_type, seq, data))
        except WebSocketDisconnect:
            self.stop.set()
            raise
        except Exception:
            self.stop.set()
            raise

    async def send_snapshot(self) -> None:
        async with self.sim._lock:
            snapshot = _build_snapshot(self.sim.exchange)
            history = self.sim.exchange.trade_history
            self.last_trade_id_sent = history[-1].trade_id if history else 0
        await self.send("snapshot", snapshot)
        self._last_book_key = _book_key(snapshot)

    def cancel_broadcasters(self) -> None:
        self.stop.set()
        for task in self._broadcaster_tasks:
            if not task.done():
                task.cancel()

    async def shutdown(self) -> None:
        self.cancel_broadcasters()
        if self._broadcaster_tasks:
            await asyncio.gather(*self._broadcaster_tasks, return_exceptions=True)
        self.clients.discard(self)


async def _heartbeat_loop(session: MarketWsSession) -> None:
    while not session.stop.is_set():
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        if session.stop.is_set():
            break
        await session.send(
            "heartbeat",
            {
                "server_status": "ok",
                "market_running": session.session_running(),
                "connected_clients": session.connected_clients,
            },
        )


async def _metrics_loop(session: MarketWsSession) -> None:
    while not session.stop.is_set():
        await asyncio.sleep(METRICS_INTERVAL)
        if session.stop.is_set():
            break
        async with session.sim._lock:
            payload = _metrics_fields(session.sim.exchange)
        await session.send("metrics", payload)


async def _book_loop(session: MarketWsSession) -> None:
    while not session.stop.is_set():
        await asyncio.sleep(BOOK_INTERVAL)
        if session.stop.is_set():
            break
        async with session.sim._lock:
            book = _book_fields(session.sim.exchange)
        key = _book_key(book)
        if key != session._last_book_key:
            session._last_book_key = key
            await session.send("book", book)


async def _trades_loop(session: MarketWsSession) -> None:
    """Batch new trades every TRADE_BATCH_INTERVAL; cap at MAX_TRADES_PER_BATCH."""
    while not session.stop.is_set():
        await asyncio.sleep(TRADE_BATCH_INTERVAL)
        if session.stop.is_set():
            break

        async with session.sim._lock:
            new_trades = [
                t
                for t in session.sim.exchange.trade_history
                if t.trade_id > session.last_trade_id_sent
            ]

        if not new_trades:
            continue

        total_new = len(new_trades)
        dropped_count = max(0, total_new - MAX_TRADES_PER_BATCH)
        has_more = dropped_count > 0
        if has_more:
            new_trades = new_trades[-MAX_TRADES_PER_BATCH:]

        trades_payload = [_trade_payload(t) for t in new_trades]
        first_id = new_trades[0].trade_id
        last_id = new_trades[-1].trade_id

        await session.send(
            "trades",
            {
                "count": len(trades_payload),
                "first_trade_id": first_id,
                "last_trade_id": last_id,
                "has_more": has_more,
                "dropped_count": dropped_count,
                "trades": trades_payload,
            },
        )
        session.last_trade_id_sent = last_id


async def _disconnect_listener(session: MarketWsSession) -> None:
    try:
        while not session.stop.is_set():
            await session.websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        session.stop.set()


async def _run_broadcasters(session: MarketWsSession) -> None:
    session._broadcaster_tasks = [
        asyncio.create_task(_heartbeat_loop(session), name="ws-heartbeat"),
        asyncio.create_task(_metrics_loop(session), name="ws-metrics"),
        asyncio.create_task(_book_loop(session), name="ws-book"),
        asyncio.create_task(_trades_loop(session), name="ws-trades"),
        asyncio.create_task(_disconnect_listener(session), name="ws-listen"),
    ]
    try:
        await asyncio.gather(*session._broadcaster_tasks)
    finally:
        session.cancel_broadcasters()
        await asyncio.gather(*session._broadcaster_tasks, return_exceptions=True)


def _log_feed_config() -> None:
    logger.info(
        "Market data feed config: "
        "METRICS_INTERVAL=%.1fs BOOK_INTERVAL=%.1fs "
        "TRADE_BATCH_INTERVAL=%.1fs HEARTBEAT_INTERVAL=%.1fs "
        "MAX_TRADES_PER_BATCH=%d",
        METRICS_INTERVAL,
        BOOK_INTERVAL,
        TRADE_BATCH_INTERVAL,
        HEARTBEAT_INTERVAL,
        MAX_TRADES_PER_BATCH,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start simulation on startup; stop it on shutdown."""
    logging.basicConfig(level=logging.INFO)
    _log_feed_config()

    sim = AsyncSimulation()
    stop = asyncio.Event()
    session_task = asyncio.create_task(
        run_session_until_stopped(sim, stop, mm_interval=0.10),
        name="exchange-session",
    )

    app.state.sim = sim
    app.state.stop = stop
    app.state.session_task = session_task
    app.state.ws_clients: Set[MarketWsSession] = set()

    logger.info("Exchange simulation session started (market_running=True)")

    yield

    stop.set()
    session_task.cancel()
    try:
        await session_task
    except asyncio.CancelledError:
        pass

    for client in list(app.state.ws_clients):
        client.cancel_broadcasters()
    app.state.ws_clients.clear()
    logger.info("Exchange simulation session stopped")


app = FastAPI(title="Exchange Simulator Market Data", lifespan=lifespan)


@app.get("/health")
async def health(request: Request):
    sim: AsyncSimulation = request.app.state.sim
    snapshot = await _read_snapshot(sim)
    return {
        "status": "ok",
        "service": "exchange-simulator",
        "timestamp": _utc_now_iso(),
        "session_running": not request.app.state.session_task.done(),
        **snapshot,
    }


@app.websocket("/ws/market")
async def ws_market(websocket: WebSocket):
    sim: AsyncSimulation = websocket.app.state.sim
    clients: Set[MarketWsSession] = websocket.app.state.ws_clients
    session_task = websocket.app.state.session_task

    await websocket.accept()

    def market_running() -> bool:
        return not session_task.done()

    session = MarketWsSession(websocket, sim, clients, market_running)
    clients.add(session)
    logger.info("WebSocket client connected (connected_clients=%d)", len(clients))

    try:
        await session.send_snapshot()
        await _run_broadcasters(session)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception:
        logger.exception("WebSocket session error")
    finally:
        await session.shutdown()
        logger.info(
            "WebSocket client removed (connected_clients=%d)", len(clients)
        )
