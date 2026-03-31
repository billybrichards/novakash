import React from 'react';

/**
 * TradeTable — Paginated table with color-coded win/loss.
 */
export default function TradeTable({ trades }) {
  if (!trades || trades.length === 0) {
    return <div style={{ color: 'var(--text-secondary)' }} className="text-center py-8">No trades</div>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead style={{ background: 'rgba(255,255,255,0.05)', borderBottom: '1px solid var(--border)' }}>
          <tr>
            <th className="text-left py-3 px-4">Market</th>
            <th className="text-left py-3 px-4">Strategy</th>
            <th className="text-center py-3 px-4">Direction</th>
            <th className="text-right py-3 px-4">Stake</th>
            <th className="text-right py-3 px-4">Entry</th>
            <th className="text-right py-3 px-4">PnL</th>
            <th className="text-center py-3 px-4">Outcome</th>
            <th className="text-right py-3 px-4 text-xs">Created</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((trade, idx) => (
            <tr key={trade.id || idx} style={{ borderBottom: '1px solid var(--border)' }}>
              <td className="py-3 px-4 font-mono text-xs">{trade.market_slug}</td>
              <td className="py-3 px-4 text-xs">{trade.strategy}</td>
              <td className="py-3 px-4 text-center">{trade.direction}</td>
              <td className="py-3 px-4 text-right">${(trade.stake_usd || 0).toFixed(2)}</td>
              <td className="py-3 px-4 text-right font-mono">${(trade.entry_price || 0).toFixed(4)}</td>
              <td
                className={`py-3 px-4 text-right font-semibold ${
                  trade.pnl_usd > 0 ? 'text-profit' : trade.pnl_usd < 0 ? 'text-loss' : ''
                }`}
              >
                ${(trade.pnl_usd || 0).toFixed(2)}
              </td>
              <td className="py-3 px-4 text-center text-xs">{trade.outcome || '—'}</td>
              <td className="py-3 px-4 text-right text-xs text-muted">
                {trade.created_at ? new Date(trade.created_at).toLocaleDateString() : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
