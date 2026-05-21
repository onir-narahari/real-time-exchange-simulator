import type { ConnectionStatus } from '../types';
import { formatClock, formatTime } from '../utils/format';

interface StatusBarProps {
  connectionStatus: ConnectionStatus;
  lastHeartbeat: string | null;
  messageCount: number;
  latestSeq: number | null;
  marketRunning: boolean | null;
  connectedClients: number | null;
  totalDroppedTrades: number;
}

const STATUS_LABEL: Record<ConnectionStatus, string> = {
  connecting: 'Connecting',
  connected: 'Live',
  disconnected: 'Disconnected',
  reconnecting: 'Reconnecting',
};

export function StatusBar({
  connectionStatus,
  lastHeartbeat,
  messageCount,
  latestSeq,
  marketRunning,
  connectedClients,
  totalDroppedTrades,
}: StatusBarProps) {
  return (
    <header className="status-bar">
      <div className="brand">
        <span className="brand-mark" aria-hidden />
        <div>
          <h1>Exchange Simulator</h1>
          <p className="brand-sub">Live market data</p>
        </div>
      </div>

      <div className="status-pills">
        <Pill
          label="Feed"
          value={STATUS_LABEL[connectionStatus]}
          variant={connectionStatus === 'connected' ? 'ok' : 'warn'}
          dot
        />
        <Pill
          label="Market"
          value={
            marketRunning == null
              ? '—'
              : marketRunning
                ? 'Running'
                : 'Stopped'
          }
          variant={marketRunning ? 'ok' : 'neutral'}
        />
        <Pill label="Messages" value={messageCount.toLocaleString()} />
        <Pill label="Seq" value={latestSeq?.toString() ?? '—'} />
        <Pill
          label="Heartbeat"
          value={formatTime(lastHeartbeat)}
          title={formatClock(lastHeartbeat)}
        />
        <Pill label="Clients" value={connectedClients?.toString() ?? '—'} />
        <div
          className={`dropped-diag${totalDroppedTrades > 0 ? ' dropped-diag--active' : ''}`}
          title="Trades omitted from tape/chart due to batch display limits"
        >
          <span className="dropped-diag-label">Dropped display trades:</span>
          <span className="dropped-diag-value mono">
            {totalDroppedTrades.toLocaleString()}
          </span>
        </div>
      </div>
    </header>
  );
}

function Pill({
  label,
  value,
  variant = 'neutral',
  dot,
  title,
}: {
  label: string;
  value: string;
  variant?: 'ok' | 'warn' | 'neutral';
  dot?: boolean;
  title?: string;
}) {
  return (
    <div className={`pill pill--${variant}`} title={title}>
      <span className="pill-label">{label}</span>
      <span className="pill-value">
        {dot && <span className="pill-dot" aria-hidden />}
        {value}
      </span>
    </div>
  );
}
