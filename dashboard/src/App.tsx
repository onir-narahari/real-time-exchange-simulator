import { StatusBar } from './components/StatusBar';
import { TopOfBook } from './components/TopOfBook';
import { MetricsPanel } from './components/MetricsPanel';
import { TradeTape } from './components/TradeTape';
import { PriceChart } from './components/PriceChart';
import { useMarketWebSocket } from './hooks/useMarketWebSocket';
import './App.css';

function App() {
  const feed = useMarketWebSocket();

  const lastTrade = feed.trades[feed.trades.length - 1];
  const lastPrice =
    lastTrade?.price ?? feed.book.mid_price ?? feed.book.best_bid;

  return (
    <div className="app">
      <StatusBar
        connectionStatus={feed.connectionStatus}
        lastHeartbeat={feed.lastHeartbeat}
        messageCount={feed.messageCount}
        latestSeq={feed.latestSeq}
        marketRunning={feed.marketRunning}
        connectedClients={feed.connectedClients}
        totalDroppedTrades={feed.totalDroppedTrades}
      />

      <main className="dashboard-grid">
        <div className="col-main">
          <PriceChart points={feed.chartPoints} lastPrice={lastPrice ?? null} />
          <TradeTape trades={feed.trades} />
        </div>
        <aside className="col-side">
          <TopOfBook book={feed.book} />
          <MetricsPanel metrics={feed.metrics} />
        </aside>
      </main>
    </div>
  );
}

export default App;
