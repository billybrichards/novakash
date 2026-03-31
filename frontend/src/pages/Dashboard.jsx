import React, { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi.js';
import StatCard from '../components/StatCard.jsx';
import VPINChart from '../components/VPINChart.jsx';
import ArbMonitor from '../components/ArbMonitor.jsx';
import CascadeIndicator from '../components/CascadeIndicator.jsx';

/**
 * Dashboard — Main overview page.
 *
 * Layout:
 *   - Top stats bar (bankroll, daily PnL, win rate, open trades)
 *   - VPIN chart row
 *   - Arb monitor row
 *   - Recent trades row
 *   - Cascade indicator
 */
export default function Dashboard() {
  const api = useApi();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const res = await api.get('/dashboard');
        setData(res.data);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
    const interval = setInterval(fetchData, 5000); // Refresh every 5s
    return () => clearInterval(interval);
  }, [api]);

  if (loading) return <div className="p-6">Loading dashboard…</div>;
  if (error) return <div className="p-6 text-loss">Error: {error}</div>;
  if (!data) return <div className="p-6">No data</div>;

  const summary = data.summary || {};

  return (
    <div className="space-y-6 p-6">
      {/* Top Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="Bankroll"
          value={summary.bankroll ? `$${summary.bankroll.toFixed(2)}` : '—'}
          change={summary.drawdown_pct ? `${(summary.drawdown_pct * 100).toFixed(1)}% drawdown` : null}
          trend={summary.drawdown_pct ? (summary.drawdown_pct > 0.1 ? 'down' : 'neutral') : 'neutral'}
        />
        <StatCard
          label="Daily P&L"
          value={`$${(summary.daily_pnl || 0).toFixed(2)}`}
          change={summary.daily_pnl && summary.daily_pnl !== 0 ? (summary.daily_pnl > 0 ? '+' : '') : null}
          trend={summary.daily_pnl > 0 ? 'up' : summary.daily_pnl < 0 ? 'down' : 'neutral'}
        />
        <StatCard
          label="Total P&L"
          value={`$${(summary.total_pnl || 0).toFixed(2)}`}
          change={null}
          trend={summary.total_pnl > 0 ? 'up' : 'down'}
        />
        <StatCard
          label="Win Rate"
          value={`${(summary.win_rate * 100 || 0).toFixed(1)}%`}
          change={`${summary.open_trades || 0} open`}
          trend="neutral"
        />
      </div>

      {/* VPIN & Cascade Row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="card p-6">
          <h2 className="text-lg font-semibold mb-4">VPIN Metric</h2>
          <VPINChart signal={data.vpin} />
        </div>
        <div className="card p-6">
          <h2 className="text-lg font-semibold mb-4">Cascade State</h2>
          <CascadeIndicator cascade={data.cascade} />
        </div>
      </div>

      {/* Arb Monitor */}
      <div className="card p-6">
        <h2 className="text-lg font-semibold mb-4">Arb Monitor</h2>
        <ArbMonitor />
      </div>

      {/* Recent Trades */}
      {data.recent_trades && data.recent_trades.length > 0 && (
        <div className="card p-6">
          <h2 className="text-lg font-semibold mb-4">Recent Trades</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead style={{ borderBottom: '1px solid var(--border)' }}>
                <tr>
                  <th className="text-left py-2 px-2">Market</th>
                  <th className="text-left py-2 px-2">Strategy</th>
                  <th className="text-right py-2 px-2">Stake</th>
                  <th className="text-right py-2 px-2">PnL</th>
                  <th className="text-center py-2 px-2">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_trades.slice(0, 5).map(t => (
                  <tr key={t.id} style={{ borderBottom: '1px solid var(--border)' }}>
                    <td className="py-2 px-2 font-mono text-xs">{t.market_slug}</td>
                    <td className="py-2 px-2">{t.strategy}</td>
                    <td className="py-2 px-2 text-right">${t.stake_usd?.toFixed(2)}</td>
                    <td className={`py-2 px-2 text-right font-semibold ${t.pnl_usd > 0 ? 'text-profit' : 'text-loss'}`}>
                      ${t.pnl_usd?.toFixed(2)}
                    </td>
                    <td className="py-2 px-2 text-center text-xs">{t.outcome}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
