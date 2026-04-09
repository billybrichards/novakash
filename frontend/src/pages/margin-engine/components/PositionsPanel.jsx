import React from 'react';
import { T } from './constants.js';

function PositionRow({ position }) {
  const pnl = position.realised_pnl || position.unrealised_pnl || 0;
  const isOpen = position.state === 'OPEN';
  const isWin = pnl > 0;

  return (
    <tr style={{ borderBottom: `1px solid ${T.cardBorder}` }}>
      <td style={{ padding: '8px 10px', fontSize: 10, color: T.text }}>{position.id}</td>
      <td style={{ padding: '8px 10px', fontSize: 10 }}>
        <span style={{
          padding: '2px 6px', borderRadius: 3, fontSize: 8, fontWeight: 800,
          background: position.side === 'LONG' ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)',
          color: position.side === 'LONG' ? T.green : T.red,
          border: `1px solid ${position.side === 'LONG' ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)'}`,
        }}>{position.side}</span>
      </td>
      <td style={{ padding: '8px 10px', fontSize: 10, fontFamily: T.mono, color: T.text }}>
        ${position.entry_price?.toLocaleString(undefined, { minimumFractionDigits: 2 }) ?? '—'}
      </td>
      <td style={{ padding: '8px 10px', fontSize: 10, fontFamily: T.mono, color: T.text }}>
        ${position.notional?.toFixed(2) ?? '—'}
      </td>
      <td style={{ padding: '8px 10px', fontSize: 10, fontFamily: T.mono, color: isWin ? T.green : T.red }}>
        {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}
      </td>
      <td style={{ padding: '8px 10px', fontSize: 10 }}>
        <span style={{
          padding: '2px 6px', borderRadius: 3, fontSize: 8, fontWeight: 700,
          background: isOpen ? 'rgba(6,182,212,0.15)' : pnl > 0 ? 'rgba(16,185,129,0.1)' : 'rgba(239,68,68,0.1)',
          color: isOpen ? T.cyan : pnl > 0 ? T.green : T.red,
        }}>{position.state}</span>
      </td>
      <td style={{ padding: '8px 10px', fontSize: 9, color: T.textMuted }}>
        {position.exit_reason || (isOpen ? '—' : 'unknown')}
      </td>
    </tr>
  );
}

export default function PositionsPanel({ positions }) {
  if (!positions || positions.length === 0) {
    return (
      <div style={{ background: T.card, border: `1px solid ${T.cardBorder}`, borderRadius: 8, padding: 20 }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: T.textMuted, marginBottom: 8 }}>POSITIONS</div>
        <div style={{ fontSize: 10, color: T.textDim }}>No positions yet</div>
      </div>
    );
  }

  return (
    <div style={{ background: T.card, border: `1px solid ${T.cardBorder}`, borderRadius: 8, overflow: 'hidden' }}>
      <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.cardBorder}`, background: T.headerBg }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: T.text }}>POSITIONS</span>
        <span style={{ fontSize: 9, color: T.textMuted, marginLeft: 8 }}>{positions.length} total</span>
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ background: T.headerBg }}>
              {['ID', 'Side', 'Entry', 'Notional', 'P&L', 'State', 'Exit Reason'].map(h => (
                <th key={h} style={{ padding: '6px 10px', fontSize: 8, fontWeight: 700, color: T.textMuted, textAlign: 'left', letterSpacing: '0.08em' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {positions.map(p => <PositionRow key={p.id} position={p} />)}
          </tbody>
        </table>
      </div>
    </div>
  );
}
