import React, { useMemo } from 'react';
import { T } from './constants.js';

/**
 * TradeTicker -- Thin 32px scrolling strip showing recent trade outcomes.
 *
 * Props:
 *   recentTrades -- Array of trade objects from hqData.recent_trades
 */
export default function TradeTicker({ recentTrades }) {
  const items = useMemo(() => {
    if (!recentTrades || recentTrades.length === 0) return [];
    return recentTrades.map(t => {
      const outcome = (t.outcome || t.status || '').toUpperCase();
      const isWin = outcome.includes('WIN');
      const isLoss = outcome.includes('LOSS');
      const pnl = t.pnl_usd;
      const dir = t.direction || '?';
      const entry = t.entry_price;

      if (isWin) {
        return {
          text: `WIN ${pnl != null ? (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2) : ''} ${dir}`,
          color: '#10b981',
          icon: '\u2705',
        };
      }
      if (isLoss) {
        return {
          text: `LOSS ${pnl != null ? (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2) : ''} ${dir}`,
          color: '#ef4444',
          icon: '\u274c',
        };
      }
      // Open / pending
      return {
        text: `OPEN $${entry != null ? entry.toFixed(2) : '?'} ${dir}`,
        color: '#f59e0b',
        icon: '\u23f3',
      };
    });
  }, [recentTrades]);

  if (items.length === 0) return null;

  // Duplicate items for seamless loop
  const tickerContent = [...items, ...items];

  return (
    <div style={{
      height: 32, overflow: 'hidden', flexShrink: 0,
      background: 'rgba(15,23,42,0.6)',
      borderBottom: `1px solid ${T.cardBorder}`,
      display: 'flex', alignItems: 'center',
      position: 'relative',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 0,
        whiteSpace: 'nowrap',
        animation: `tickerScroll ${Math.max(items.length * 4, 20)}s linear infinite`,
      }}>
        {tickerContent.map((item, i) => (
          <span key={i} style={{
            display: 'inline-flex', alignItems: 'center', gap: 4,
            padding: '0 16px', fontSize: 11,
            fontFamily: "'JetBrains Mono', monospace",
            fontWeight: 600, color: item.color,
          }}>
            <span>{item.icon}</span>
            <span>{item.text}</span>
            {i < tickerContent.length - 1 && (
              <span style={{ color: T.textDim, margin: '0 8px' }}>|</span>
            )}
          </span>
        ))}
      </div>

      <style>{`
        @keyframes tickerScroll {
          0% { transform: translateX(0); }
          100% { transform: translateX(-50%); }
        }
      `}</style>
    </div>
  );
}
