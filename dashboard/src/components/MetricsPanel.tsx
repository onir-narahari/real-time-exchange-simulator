import type { MetricsState } from '../types';
import { formatPct, formatPrice, formatQty } from '../utils/format';

interface MetricsPanelProps {
  metrics: MetricsState;
}

const ROWS: { key: keyof MetricsState; label: string; fmt?: 'pct' | 'spread' | 'num' }[] = [
  { key: 'orders_submitted', label: 'Orders submitted', fmt: 'num' },
  { key: 'total_trades', label: 'Total trades', fmt: 'num' },
  { key: 'total_volume', label: 'Total volume', fmt: 'num' },
  { key: 'cancellations', label: 'Cancellations', fmt: 'num' },
  { key: 'orders_fully_filled', label: 'Fully filled', fmt: 'num' },
  { key: 'fill_rate_pct', label: 'Fill rate', fmt: 'pct' },
  { key: 'average_spread', label: 'Avg spread', fmt: 'spread' },
  { key: 'resting_orders', label: 'Resting orders', fmt: 'num' },
];

function formatMetric(value: number | null, fmt?: string): string {
  if (value == null && fmt === 'spread') return '—';
  switch (fmt) {
    case 'pct':
      return formatPct(value as number);
    case 'spread':
      return formatPrice(value);
    default:
      return formatQty(value as number);
  }
}

export function MetricsPanel({ metrics }: MetricsPanelProps) {
  return (
    <section className="panel panel--metrics">
      <header className="panel-header">
        <h2>Session metrics</h2>
        <span className="panel-tag">Live</span>
      </header>
      <dl className="metrics-grid">
        {ROWS.map(({ key, label, fmt }) => (
          <div key={key} className="metric-item">
            <dt>{label}</dt>
            <dd className="mono">{formatMetric(metrics[key], fmt)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
