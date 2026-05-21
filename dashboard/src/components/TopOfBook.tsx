import type { BookState } from '../types';
import { formatPrice, formatQty } from '../utils/format';

interface TopOfBookProps {
  book: BookState;
}

export function TopOfBook({ book }: TopOfBookProps) {
  const spreadBps =
    book.spread != null && book.mid_price
      ? ((book.spread / book.mid_price) * 10000).toFixed(1)
      : null;

  return (
    <section className="panel panel--book">
      <header className="panel-header">
        <h2>Top of book</h2>
        <span className="panel-tag">SIM / USD</span>
      </header>

      <div className="book-ladder">
        <div className="book-side book-side--ask">
          <span className="book-side-label">Ask</span>
          <span className="book-price book-price--ask">
            {formatPrice(book.best_ask)}
          </span>
          <span className="book-depth">{formatQty(book.ask_depth)}</span>
        </div>

        <div className="book-mid">
          <span className="book-mid-label">Mid</span>
          <span className="book-mid-price">{formatPrice(book.mid_price)}</span>
          <span className="book-spread">
            Spread {formatPrice(book.spread)}
            {spreadBps != null && (
              <span className="book-spread-bps"> ({spreadBps} bps)</span>
            )}
          </span>
        </div>

        <div className="book-side book-side--bid">
          <span className="book-side-label">Bid</span>
          <span className="book-price book-price--bid">
            {formatPrice(book.best_bid)}
          </span>
          <span className="book-depth">{formatQty(book.bid_depth)}</span>
        </div>
      </div>

      <div className="depth-bars">
        <DepthBar
          label="Bid depth"
          value={book.bid_depth}
          total={book.bid_depth + book.ask_depth}
          tone="bid"
        />
        <DepthBar
          label="Ask depth"
          value={book.ask_depth}
          total={book.bid_depth + book.ask_depth}
          tone="ask"
        />
      </div>
    </section>
  );
}

function DepthBar({
  label,
  value,
  total,
  tone,
}: {
  label: string;
  value: number;
  total: number;
  tone: 'bid' | 'ask';
}) {
  const pct = total > 0 ? Math.round((value / total) * 100) : 0;
  return (
    <div className="depth-row">
      <div className="depth-row-head">
        <span>{label}</span>
        <span className="mono">{formatQty(value)}</span>
      </div>
      <div className="depth-track">
        <div
          className={`depth-fill depth-fill--${tone}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
