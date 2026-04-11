import React, { useMemo } from 'react';
import { T } from './constants.js';
import { sotColors } from './sot.jsx';

/**
 * TradeTicker -- Thin 32px scrolling strip showing recent trade outcomes.
 *
 * Props:
 *   recentTrades -- Array of trade objects from hqData.recent_trades
 *   manualSotRows -- POLY-SOT: optional array from /api/v58/manual-trades-sot.
 *                    When present, recent manual trades are prepended to the
 *                    ticker with a colour-coded SOT chip showing whether
 *                    Polymarket has confirmed the order. Mirrors the
 *                    margin_engine pattern where the exchange is the SOT.
 */
export default function TradeTicker({ recentTrades, manualSotRows }) {
  const items = useMemo(() => {
    const out = [];

    // POLY-SOT: prepend manual trades with their reconciliation chip so any
    // engine_optimistic / diverged row screams red in the always-visible
    // ticker. Cap at 5 so the ticker doesn't fill up with manual trades.
    if (Array.isArray(manualSotRows) && manualSotRows.length > 0) {
      for (const m of manualSotRows.slice(0, 5)) {
        const colors = sotColors(m.sot_reconciliation_state);
        const dir = m.direction || '?';
        const stake = m.stake_usd;
        const tooltipParts = [`SOT: ${colors.label}`];
        if (m.sot_reconciliation_notes) tooltipParts.push(m.sot_reconciliation_notes);
        if (m.polymarket_confirmed_fill_price != null) {
          tooltipParts.push(`poly fill: $${Number(m.polymarket_confirmed_fill_price).toFixed(4)}`);
        }
        out.push({
          key: `sot-${m.trade_id}`,
          isSot: true,
          color: colors.fg,
          bg: colors.bg,
          border: colors.border,
          text: `MANUAL ${dir}${stake != null ? ' $' + Number(stake).toFixed(2) : ''} ${colors.label}`,
          tooltip: tooltipParts.join(' | '),
        });
      }
    }

    if (!recentTrades || recentTrades.length === 0) return out;
    for (const t of recentTrades) {
      const outcome = (t.outcome || t.status || '').toUpperCase();
      const isWin = outcome.includes('WIN');
      const isLoss = outcome.includes('LOSS');
      const pnl = t.pnl_usd;
      const dir = t.direction || '?';
      const entry = t.entry_price;

      if (isWin) {
        out.push({
          key: `t-${t.id || dir}-${pnl}`,
          text: `WIN ${pnl != null ? (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2) : ''} ${dir}`,
          color: '#10b981',
          icon: '\u2705',
        });
        continue;
      }
      if (isLoss) {
        out.push({
          key: `t-${t.id || dir}-${pnl}`,
          text: `LOSS ${pnl != null ? (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2) : ''} ${dir}`,
          color: '#ef4444',
          icon: '\u274c',
        });
        continue;
      }
      // Open / pending
      out.push({
        key: `t-${t.id || dir}-open`,
        text: `OPEN $${entry != null ? entry.toFixed(2) : '?'} ${dir}`,
        color: '#f59e0b',
        icon: '\u23f3',
      });
    }
    return out;
  }, [recentTrades, manualSotRows]);

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
        {tickerContent.map((item, i) => {
          if (item.isSot) {
            // POLY-SOT chip — shows a coloured pill instead of an icon so
            // engine_optimistic / diverged manual trades are immediately
            // distinguishable from regular ENGINE wins/losses.
            return (
              <span
                key={`${item.key}-${i}`}
                title={item.tooltip}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6,
                  padding: '2px 10px', margin: '0 8px',
                  fontSize: 10,
                  fontFamily: "'JetBrains Mono', monospace",
                  fontWeight: 700,
                  color: item.color,
                  background: item.bg,
                  border: `1px solid ${item.border}`,
                  borderRadius: 999,
                  cursor: 'help',
                }}
              >
                <span style={{
                  display: 'inline-block',
                  width: 6, height: 6, borderRadius: '50%',
                  background: item.color,
                }} />
                <span>{item.text}</span>
              </span>
            );
          }
          return (
            <span key={`${item.key}-${i}`} style={{
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
          );
        })}
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
