import React from 'react';
import { T, fmt, utcHHMM } from './theme.js';

/**
 * Band 5 — Recent Flow Timeline.
 *
 * Reuses the Factory Floor RECENT FLOW TIMELINE table pattern.
 * Pulls from /api/v58/outcomes. Last 20 windows with columns:
 * TIME | SIGNAL | ACTUAL | SRC | GATES | REASON | RESULT
 */

function outcomeLabel(o) {
  if (!o) return { text: '\u2014', color: T.textDim };
  if (o.v71_correct === true) return { text: 'WIN', color: T.green };
  if (o.v71_correct === false) return { text: 'LOSS', color: T.red };
  if (!o.v71_would_trade && !o.v58_would_trade) return { text: 'SKIP', color: T.textDim };
  if (o.v58_correct === true) return { text: 'WIN', color: T.green };
  if (o.v58_correct === false) return { text: 'LOSS', color: T.red };
  return { text: 'SKIP', color: T.textDim };
}

function outcomeGateString(o) {
  if (!o) return '';
  const skip = (o.skip_reason || '').toUpperCase();
  const checks = [
    !skip.includes('VPIN'),
    !skip.includes('TWAP'),
    !skip.includes('DELTA'),
    !skip.includes('CG'),
    !skip.includes('FLOOR'),
    !skip.includes('CAP'),
  ];
  return checks.map(p => p ? '\u2705' : '\u274C').join('');
}

function actualDirection(o) {
  if (!o) return null;
  // Derive actual from outcome + direction
  if (o.actual_direction) return o.actual_direction;
  if (o.close_price != null && o.open_price != null) {
    return o.close_price > o.open_price ? 'UP' : 'DOWN';
  }
  // From trade outcome
  if (o.v71_correct === true) return o.direction;
  if (o.v71_correct === false) return o.direction === 'UP' ? 'DOWN' : 'UP';
  return null;
}

export default function RecentFlow({ outcomes }) {
  const rows = outcomes || [];

  return (
    <div style={{
      background: T.card, border: `1px solid ${T.cardBorder}`,
      borderRadius: 6, padding: '10px 12px', fontFamily: T.mono,
      flex: 1, minHeight: 0, overflow: 'auto',
    }}>
      <div style={{
        fontSize: 8, color: T.purple, letterSpacing: '0.12em',
        fontWeight: 700, textTransform: 'uppercase', marginBottom: 6,
      }}>Recent Flow Timeline</div>

      {/* Legend */}
      <div style={{
        fontSize: 8, color: T.textDim, marginBottom: 4, lineHeight: 1.4,
      }}>
        <span style={{ fontWeight: 600, color: T.textMuted }}>SIGNAL</span> = predicted direction
        {' \u00b7 '}
        <span style={{ fontWeight: 600, color: T.textMuted }}>ACTUAL</span> = ground truth
        {' \u00b7 '}
        <span style={{ fontWeight: 600, color: T.textMuted }}>GATES</span> = VPIN\u00b7TWAP\u00b7Delta\u00b7CG\u00b7Floor\u00b7Cap
      </div>

      {/* Header */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '52px 50px 50px 34px 1fr 90px 50px',
        gap: 6, padding: '4px 0 5px',
        borderBottom: `1px solid ${T.cardBorder}`,
        fontSize: 8, color: T.textDim, letterSpacing: '0.08em',
      }}>
        <span>TIME</span>
        <span>SIGNAL</span>
        <span>ACTUAL</span>
        <span>SRC</span>
        <span>GATES</span>
        <span>REASON</span>
        <span style={{ textAlign: 'right' }}>RESULT</span>
      </div>

      {/* Rows */}
      {rows.length > 0 ? rows.slice(0, 20).map((o, i) => {
        const result = outcomeLabel(o);
        const gateStr = outcomeGateString(o);
        const actual = actualDirection(o);
        const signalMatchesActual = actual && o.direction && actual === o.direction;
        const rowBg = result.text === 'WIN'
          ? 'rgba(16,185,129,0.03)'
          : result.text === 'LOSS'
          ? 'rgba(239,68,68,0.03)'
          : 'transparent';

        return (
          <div key={i} style={{
            display: 'grid',
            gridTemplateColumns: '52px 50px 50px 34px 1fr 90px 50px',
            gap: 6, padding: '4px 0',
            borderBottom: `1px solid rgba(51,65,85,0.3)`,
            fontSize: 10, background: rowBg,
          }}>
            <span style={{ color: T.textMuted }}>{utcHHMM(o.window_ts)}</span>
            <span style={{
              fontWeight: 600,
              color: o.direction === 'UP' ? T.green : o.direction === 'DOWN' ? T.red : T.textDim,
            }}>
              {o.direction || '\u2014'}
            </span>
            <span style={{
              fontWeight: 600,
              color: actual === 'UP' ? T.green : actual === 'DOWN' ? T.red : T.textDim,
            }}>
              {actual || '\u2014'}
            </span>
            <span style={{ fontSize: 9, color: T.textMuted }}>
              {o.delta_source || '\u2014'}
            </span>
            <span style={{ fontSize: 9, letterSpacing: '0.02em' }}>{gateStr}</span>
            <span style={{
              fontSize: 8, color: T.textMuted, overflow: 'hidden',
              textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }} title={o.skip_reason || 'traded'}>
              {o.skip_reason || (o.trade_placed ? 'traded' : '\u2014')}
            </span>
            <span style={{
              textAlign: 'right', fontWeight: 700, color: result.color,
            }}>
              {result.text}
            </span>
          </div>
        );
      }) : (
        <div style={{ fontSize: 10, color: T.textDim, padding: '10px 0' }}>No recent outcomes</div>
      )}
    </div>
  );
}
