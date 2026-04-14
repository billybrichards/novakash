import React from 'react';
import { T } from './constants.js';

function PositionRow({ position }) {
  const pnl = position.realised_pnl || position.unrealised_pnl || 0;
  const isOpen = position.state === 'OPEN';
  const isWin = pnl > 0;
  const totalCommission = (position.entry_commission || 0) + (position.exit_commission || 0);

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
      <td style={{ padding: '8px 10px', fontSize: 9, fontFamily: T.mono, color: T.textMuted }}>
        {position.strategy_version ? position.strategy_version.slice(0, 10) : '—'}
      </td>
      <td style={{ padding: '8px 10px', fontSize: 9, fontFamily: T.mono, color: T.textMuted }}>
        {position.v4_entry_regime || position.v4_entry_macro_bias || '—'}
        {position.v4_entry_regime && (
          <div style={{ fontSize: 7, opacity: 0.7 }}>
            {position.v4_entry_macro_bias && `· ${position.v4_entry_macro_bias}`}
            {position.v4_entry_consensus_safe != null && ` · ${position.v4_entry_consensus_safe ? 'safe' : 'unsafe'}`}
          </div>
        )}
      </td>
      <td style={{ padding: '8px 10px', fontSize: 9, fontFamily: T.mono, color: totalCommission > 0 ? T.text : T.textMuted }}>
        {totalCommission > 0 ? `${totalCommission.toFixed(4)} ($${totalCommission.toFixed(2)})` : '—'}
        {position.entry_commission > 0 && (
          <div style={{ fontSize: 7, opacity: 0.7 }}>
            in:${position.entry_commission.toFixed(3)} out:${position.exit_commission?.toFixed(3) || 0}
          </div>
        )}
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
        {isOpen && position.continuation_count > 0 && (
          <div style={{ fontSize: 7, color: T.cyan }}>
            continued {position.continuation_count}×
          </div>
        )}
        {position.stop_loss_price && (
          <div style={{ fontSize: 7, color: T.red }}>
            SL: ${position.stop_loss_price.toFixed(2)}
          </div>
        )}
        {position.take_profit_price && (
          <div style={{ fontSize: 7, color: T.green }}>
            TP: ${position.take_profit_price.toFixed(2)}
          </div>
        )}
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
              {['ID', 'Side', 'Entry', 'Notional', 'P&L', 'Strategy', 'v4 Context', 'Fees', 'State', 'Exit Reason'].map(h => (
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
