import type { Trade } from '../types';
import { formatPrice, formatQty, formatTime } from '../utils/format';

interface TradeTapeProps {
  trades: Trade[];
}

export function TradeTape({ trades }: TradeTapeProps) {
  const rows = [...trades].reverse();

  return (
    <section className="panel panel--trades">
      <header className="panel-header">
        <h2>Recent trades</h2>
        <span className="panel-tag">{trades.length} / 100</span>
      </header>

      <div className="trades-table-wrap">
        <table className="trades-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>ID</th>
              <th>Side</th>
              <th className="align-right">Qty</th>
              <th className="align-right">Price</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={5} className="empty-row">
                  Waiting for trades…
                </td>
              </tr>
            ) : (
              rows.map((t) => (
                <tr key={t.trade_id}>
                  <td className="mono muted">{formatTime(t.trade_time)}</td>
                  <td className="mono muted">#{t.trade_id}</td>
                  <td>
                    <span
                      className={`side-badge side-badge--${t.side.toLowerCase()}`}
                    >
                      {t.side.toUpperCase()}
                    </span>
                  </td>
                  <td className="align-right mono">{formatQty(t.quantity)}</td>
                  <td
                    className={`align-right mono trade-price trade-price--${t.side.toLowerCase()}`}
                  >
                    {formatPrice(t.price)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
