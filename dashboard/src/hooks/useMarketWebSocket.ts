import { useCallback, useEffect, useRef, useState } from 'react';
import {
  type BookState,
  type ChartPoint,
  type ConnectionStatus,
  type MetricsState,
  type Trade,
  EMPTY_BOOK,
  EMPTY_METRICS,
  WS_URL,
  type WsMessage,
} from '../types';

const MAX_TRADES = 100;
const MAX_CHART_POINTS = 300;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

function pickBook(data: Record<string, unknown>): Partial<BookState> {
  return {
    best_bid: data.best_bid as number | null,
    best_ask: data.best_ask as number | null,
    spread: data.spread as number | null,
    mid_price: data.mid_price as number | null,
    bid_depth: (data.bid_depth as number) ?? 0,
    ask_depth: (data.ask_depth as number) ?? 0,
  };
}

function pickMetrics(data: Record<string, unknown>): MetricsState {
  return {
    orders_submitted: (data.orders_submitted as number) ?? 0,
    total_trades: (data.total_trades as number) ?? 0,
    total_volume: (data.total_volume as number) ?? 0,
    cancellations: (data.cancellations as number) ?? 0,
    orders_fully_filled: (data.orders_fully_filled as number) ?? 0,
    fill_rate_pct: (data.fill_rate_pct as number) ?? 0,
    average_spread: (data.average_spread as number | null) ?? null,
    resting_orders: (data.resting_orders as number) ?? 0,
  };
}

function appendChartPoint(
  points: ChartPoint[],
  price: number,
  source: 'trade' | 'mid',
  timestamp: string,
): ChartPoint[] {
  const time = new Date(timestamp).getTime() || Date.now();
  const next = [
    ...points,
    {
      time,
      label: new Date(time).toLocaleTimeString(),
      price,
      source,
    },
  ];
  return next.length > MAX_CHART_POINTS
    ? next.slice(-MAX_CHART_POINTS)
    : next;
}

export interface MarketFeedState {
  connectionStatus: ConnectionStatus;
  lastHeartbeat: string | null;
  messageCount: number;
  latestSeq: number | null;
  marketRunning: boolean | null;
  connectedClients: number | null;
  book: BookState;
  metrics: MetricsState;
  trades: Trade[];
  chartPoints: ChartPoint[];
  totalDroppedTrades: number;
}

export function useMarketWebSocket(): MarketFeedState {
  const [connectionStatus, setConnectionStatus] =
    useState<ConnectionStatus>('connecting');
  const [lastHeartbeat, setLastHeartbeat] = useState<string | null>(null);
  const [messageCount, setMessageCount] = useState(0);
  const [latestSeq, setLatestSeq] = useState<number | null>(null);
  const [marketRunning, setMarketRunning] = useState<boolean | null>(null);
  const [connectedClients, setConnectedClients] = useState<number | null>(
    null,
  );
  const [book, setBook] = useState<BookState>(EMPTY_BOOK);
  const [metrics, setMetrics] = useState<MetricsState>(EMPTY_METRICS);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [chartPoints, setChartPoints] = useState<ChartPoint[]>([]);
  const [totalDroppedTrades, setTotalDroppedTrades] = useState(0);

  const reconnectAttempt = useRef(0);
  const wsRef = useRef<WebSocket | null>(null);
  const unmountedRef = useRef(false);

  const addMidPoint = useCallback((mid: number | null, timestamp: string) => {
    if (mid == null) return;
    setChartPoints((prev) => appendChartPoint(prev, mid, 'mid', timestamp));
  }, []);

  const handleMessage = useCallback(
    (raw: string) => {
      let msg: WsMessage;
      try {
        msg = JSON.parse(raw) as WsMessage;
      } catch {
        return;
      }

      setMessageCount((c) => c + 1);
      setLatestSeq(msg.seq);

      const data = msg.data as Record<string, unknown>;

      switch (msg.type) {
        case 'snapshot': {
          setBook({ ...EMPTY_BOOK, ...pickBook(data) });
          setMetrics(pickMetrics(data));
          setTrades([]);
          setChartPoints([]);
          setTotalDroppedTrades(0);
          addMidPoint(data.mid_price as number | null, msg.timestamp);
          break;
        }
        case 'book': {
          setBook((b) => ({ ...b, ...pickBook(data) }));
          addMidPoint(data.mid_price as number | null, msg.timestamp);
          break;
        }
        case 'metrics': {
          setMetrics(pickMetrics(data));
          break;
        }
        case 'trades': {
          const hasMore = Boolean(data.has_more);
          const droppedCount = Number(data.dropped_count) || 0;
          if (hasMore && droppedCount > 0) {
            setTotalDroppedTrades((t) => t + droppedCount);
          }

          const batch = (data.trades as Trade[]) ?? [];
          if (batch.length === 0) break;
          setTrades((prev) => {
            const merged = [...prev, ...batch];
            return merged.length > MAX_TRADES
              ? merged.slice(-MAX_TRADES)
              : merged;
          });
          setChartPoints((prev) => {
            let next = prev;
            for (const t of batch) {
              next = appendChartPoint(
                next,
                t.price,
                'trade',
                t.trade_time || msg.timestamp,
              );
            }
            return next;
          });
          break;
        }
        case 'heartbeat': {
          setLastHeartbeat(msg.timestamp);
          setMarketRunning(data.market_running as boolean);
          setConnectedClients(data.connected_clients as number);
          break;
        }
        default:
          break;
      }
    },
    [addMidPoint],
  );

  useEffect(() => {
    unmountedRef.current = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const scheduleReconnect = () => {
      if (unmountedRef.current) return;
      const attempt = reconnectAttempt.current;
      const delay = Math.min(
        RECONNECT_BASE_MS * 2 ** attempt,
        RECONNECT_MAX_MS,
      );
      reconnectAttempt.current = attempt + 1;
      setConnectionStatus('reconnecting');
      reconnectTimer = setTimeout(connect, delay);
    };

    const connect = () => {
      if (unmountedRef.current) return;

      if (wsRef.current) {
        wsRef.current.onopen = null;
        wsRef.current.onmessage = null;
        wsRef.current.onerror = null;
        wsRef.current.onclose = null;
        wsRef.current.close();
      }

      setConnectionStatus(
        reconnectAttempt.current === 0 ? 'connecting' : 'reconnecting',
      );

      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        if (unmountedRef.current) return;
        reconnectAttempt.current = 0;
        setConnectionStatus('connected');
      };

      ws.onmessage = (event) => {
        if (typeof event.data === 'string') {
          handleMessage(event.data);
        }
      };

      ws.onerror = () => {
        /* onclose handles reconnect */
      };

      ws.onclose = () => {
        if (unmountedRef.current) return;
        wsRef.current = null;
        setConnectionStatus('disconnected');
        scheduleReconnect();
      };
    };

    connect();

    return () => {
      unmountedRef.current = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [handleMessage]);

  return {
    connectionStatus,
    lastHeartbeat,
    messageCount,
    latestSeq,
    marketRunning,
    connectedClients,
    book,
    metrics,
    trades,
    chartPoints,
    totalDroppedTrades,
  };
}
