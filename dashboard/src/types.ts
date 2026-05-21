export type MessageType =
  | 'snapshot'
  | 'book'
  | 'metrics'
  | 'trades'
  | 'heartbeat';

export interface WsMessage<T = Record<string, unknown>> {
  seq: number;
  timestamp: string;
  type: MessageType;
  data: T;
}

export interface BookState {
  best_bid: number | null;
  best_ask: number | null;
  spread: number | null;
  mid_price: number | null;
  bid_depth: number;
  ask_depth: number;
}

export interface MetricsState {
  orders_submitted: number;
  total_trades: number;
  total_volume: number;
  cancellations: number;
  orders_fully_filled: number;
  fill_rate_pct: number;
  average_spread: number | null;
  event_count: number;
}

export interface Trade {
  trade_id: number;
  price: number;
  quantity: number;
  side: string;
  trade_time: string;
}

export interface HeartbeatData {
  server_status: string;
  market_running: boolean;
  connected_clients: number;
}

export interface ChartPoint {
  time: number;
  label: string;
  price: number;
  source: 'trade' | 'mid';
}

export type ConnectionStatus =
  | 'connecting'
  | 'connected'
  | 'disconnected'
  | 'reconnecting';

export const WS_URL = 'ws://localhost:8000/ws/market';

export const EMPTY_BOOK: BookState = {
  best_bid: null,
  best_ask: null,
  spread: null,
  mid_price: null,
  bid_depth: 0,
  ask_depth: 0,
};

export const EMPTY_METRICS: MetricsState = {
  orders_submitted: 0,
  total_trades: 0,
  total_volume: 0,
  cancellations: 0,
  orders_fully_filled: 0,
  fill_rate_pct: 0,
  average_spread: null,
  event_count: 0,
};
