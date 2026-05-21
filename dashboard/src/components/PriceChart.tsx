import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { ChartPoint } from '../types';
import { formatPrice } from '../utils/format';

interface PriceChartProps {
  points: ChartPoint[];
  lastPrice: number | null;
}

export function PriceChart({ points, lastPrice }: PriceChartProps) {
  const domain =
    points.length > 0
      ? (() => {
          const prices = points.map((p) => p.price);
          const min = Math.min(...prices);
          const max = Math.max(...prices);
          const pad = Math.max((max - min) * 0.08, 0.05);
          return [min - pad, max + pad];
        })()
      : undefined;

  return (
    <section className="panel panel--chart">
      <header className="panel-header">
        <div>
          <h2>Price</h2>
          <p className="panel-sub">Last trade & mid over time</p>
        </div>
        <span className="chart-last mono">
          {lastPrice != null ? formatPrice(lastPrice) : '—'}
        </span>
      </header>

      <div className="chart-wrap">
        {points.length < 2 ? (
          <div className="chart-placeholder">Collecting price data…</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={points}
              margin={{ top: 8, right: 12, left: 0, bottom: 0 }}
            >
              <defs>
                <linearGradient id="priceFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.35} />
                  <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="rgba(148, 163, 184, 0.12)"
                vertical={false}
              />
              <XAxis
                dataKey="label"
                tick={{ fill: '#94a3b8', fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                minTickGap={40}
              />
              <YAxis
                domain={domain}
                tick={{ fill: '#94a3b8', fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                width={56}
                tickFormatter={(v) => Number(v).toFixed(2)}
              />
              <Tooltip content={<ChartTooltip />} />
              <Area
                type="monotone"
                dataKey="price"
                stroke="#60a5fa"
                strokeWidth={2}
                fill="url(#priceFill)"
                dot={false}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </section>
  );
}

function ChartTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: { payload: ChartPoint }[];
}) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="chart-tooltip">
      <span className="chart-tooltip-price">{formatPrice(p.price)}</span>
      <span className="chart-tooltip-meta">
        {p.label} · {p.source === 'trade' ? 'Trade' : 'Mid'}
      </span>
    </div>
  );
}
