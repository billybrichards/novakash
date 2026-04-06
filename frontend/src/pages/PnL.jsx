import { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi';
import EquityCurve from '../components/EquityCurve';
import StatCard from '../components/StatCard';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts';
import { formatUSD } from '../lib/utils';

export default function PnL() {
  const api = useApi();
  const [cumulative, setCumulative] = useState([]);
  const [daily, setDaily] = useState([]);
  const [monthly, setMonthly] = useState([]);
  const [byStrategy, setByStrategy] = useState(null);

  useEffect(() => {
    api.get('/api/pnl/cumulative').then(res => setCumulative(res.data.items || [])).catch(() => {});
    api.get('/api/pnl/daily').then(res => setDaily(res.data.items || [])).catch(() => {});
    api.get('/api/pnl/monthly').then(res => setMonthly(res.data.items || [])).catch(() => {});
    api.get('/api/pnl/by-strategy').then(res => setByStrategy(res.data)).catch(() => {});
  }, [api]);

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-semibold text-white">Profit & Loss</h2>

      {/* Key Stats */}
      {byStrategy && (
        <div className="grid grid-cols-2 md:grid-cols-6 gap-4">
          <StatCard label="Total P&L" value={byStrategy.total_pnl} format="usd" colored />
          <StatCard label="Sharpe" value={byStrategy.sharpe_ratio} format="decimal" />
          <StatCard label="Max Drawdown" value={byStrategy.max_drawdown} format="percent" colored invertColor />
          <StatCard label="Win Rate" value={byStrategy.win_rate} format="percent" />
          <StatCard label="Arb P&L" value={byStrategy.arb_pnl} format="usd" colored />
          <StatCard label="VPIN P&L" value={byStrategy.vpin_pnl} format="usd" colored />
        </div>
      )}

      {/* Cumulative Equity Curve */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
        <h3 className="text-sm font-medium text-white/60 mb-3">Cumulative Equity</h3>
        <div className="h-[300px]">
          <EquityCurve data={cumulative} />
        </div>
      </div>

      {/* Daily P&L Bar Chart */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
        <h3 className="text-sm font-medium text-white/60 mb-3">Daily P&L</h3>
        <div className="h-[250px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={daily}>
              <XAxis dataKey="date" tick={{ fill: 'rgba(255,255,255,0.3)', fontSize: 11 }} />
              <YAxis tick={{ fill: 'rgba(255,255,255,0.3)', fontSize: 11 }} tickFormatter={v => `$${v}`} />
              <Tooltip
                contentStyle={{ background: '#0f0f14', border: '1px solid rgba(255,255,255,0.06)' }}
                labelStyle={{ color: 'rgba(255,255,255,0.6)' }}
                formatter={v => formatUSD(v)}
              />
              <Bar dataKey="net_pnl" radius={[2, 2, 0, 0]}>
                {daily.map((entry, i) => (
                  <Cell key={i} fill={entry.net_pnl >= 0 ? '#4ade80' : '#f87171'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Monthly Summary Table */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
        <h3 className="text-sm font-medium text-white/60 mb-3">Monthly Summary</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-white/40 border-b border-[var(--border)]">
                <th className="text-left py-2 font-medium">Month</th>
                <th className="text-right py-2 font-medium">Trades</th>
                <th className="text-right py-2 font-medium">Win Rate</th>
                <th className="text-right py-2 font-medium">Gross P&L</th>
                <th className="text-right py-2 font-medium">Fees</th>
                <th className="text-right py-2 font-medium">Net P&L</th>
              </tr>
            </thead>
            <tbody>
              {monthly.map((m, i) => (
                <tr key={i} className="border-b border-[var(--border)]">
                  <td className="py-2 text-white/80">{m.month}</td>
                  <td className="py-2 text-right text-white/60">{m.trade_count}</td>
                  <td className="py-2 text-right text-white/60">{(m.win_rate * 100).toFixed(1)}%</td>
                  <td className="py-2 text-right text-white/60">{formatUSD(m.gross_pnl)}</td>
                  <td className="py-2 text-right text-[var(--loss)]">{formatUSD(m.fees_paid)}</td>
                  <td className={`py-2 text-right font-medium ${m.net_pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                    {formatUSD(m.net_pnl)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
