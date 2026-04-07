import React, { useState } from 'react';
import { windowStatusColor, windowStatusLabel, T } from './constants.js';

/**
 * WindowHistoryTable — Paginated table of all 5-minute windows with shadow resolution data.
 *
 * Props:
 *   windows        — Array of window outcome objects from /api/v58/outcomes or /v58/execution-hq
 *   onSelectWindow — Callback when a window row is clicked (receives window object)
 *   selectedTs     — Currently selected window_ts for highlighting
 */
const PAGE_SIZE = 20;

export default function WindowHistoryTable({ windows, onSelectWindow, selectedTs }) {
  const [page, setPage] = useState(0);
  const [missedOnly, setMissedOnly] = useState(false);

  const filtered = missedOnly
    ? windows.filter(w => !w.trade_placed && w.shadow_would_win)
    : windows;

  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const pageWindows = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  const fmtTime = (isoStr) => {
    if (!isoStr) return '—';
    try {
      const d = new Date(isoStr);
      return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' }) + ' UTC';
    } catch { return isoStr; }
  };

  const fmtPct = (v) => v !== null && v !== undefined ? `${(v * 100).toFixed(3)}%` : '—';
  const fmtUsd = (v) => v !== null && v !== undefined ? `$${v >= 0 ? '+' : ''}${v.toFixed(2)}` : '—';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Filter bar */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '8px 0', borderBottom: `1px solid ${T.cardBorder}`,
        fontSize: 11, fontFamily: 'monospace',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ color: T.textMuted }}>{filtered.length} windows</span>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', color: T.amber }}>
            <input
              type="checkbox"
              checked={missedOnly}
              onChange={e => { setMissedOnly(e.target.checked); setPage(0); }}
              style={{ accentColor: T.amber }}
            />
            Missed opportunities only
          </label>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: T.textMuted }}>
          <button
            onClick={() => setPage(p => Math.max(0, p - 1))}
            disabled={page === 0}
            style={{ background: 'none', border: 'none', color: page === 0 ? T.textDim : T.cyan, cursor: 'pointer', fontFamily: 'monospace' }}
          >&lt;</button>
          <span>{page + 1} / {totalPages || 1}</span>
          <button
            onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
            disabled={page >= totalPages - 1}
            style={{ background: 'none', border: 'none', color: page >= totalPages - 1 ? T.textDim : T.cyan, cursor: 'pointer', fontFamily: 'monospace' }}
          >&gt;</button>
        </div>
      </div>

      {/* Table */}
      <div style={{ flex: 1, overflowY: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10, fontFamily: 'monospace' }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${T.cardBorder}`, color: T.textMuted }}>
              <th style={{ padding: '6px 8px', textAlign: 'left' }}>Time</th>
              <th style={{ padding: '6px 8px', textAlign: 'center' }}>Status</th>
              <th style={{ padding: '6px 8px', textAlign: 'center' }}>Agree</th>
              <th style={{ padding: '6px 8px', textAlign: 'center' }}>Dir</th>
              <th style={{ padding: '6px 8px', textAlign: 'center' }}>Tier</th>
              <th style={{ padding: '6px 8px', textAlign: 'center' }}>Type</th>
              <th style={{ padding: '6px 8px', textAlign: 'right' }}>VPIN</th>
              <th style={{ padding: '6px 8px', textAlign: 'right' }}>PnL</th>
              <th style={{ padding: '6px 8px', textAlign: 'left' }}>Reason</th>
            </tr>
          </thead>
          <tbody>
            {pageWindows.map((w, i) => {
              const statusColor = windowStatusColor(w);
              const statusLabel = windowStatusLabel(w);
              const isSelected = w.window_ts === selectedTs;
              const pnl = w.trade_placed ? w.v71_pnl : w.shadow_pnl;
              const pnlColor = pnl > 0 ? T.green : pnl < 0 ? T.red : T.textMuted;

              return (
                <tr
                  key={w.window_ts || i}
                  onClick={() => onSelectWindow?.(w)}
                  style={{
                    borderBottom: `1px solid rgba(30,41,59,0.5)`,
                    cursor: 'pointer',
                    background: isSelected ? 'rgba(6,182,212,0.08)' : 'transparent',
                    transition: 'background 0.15s',
                  }}
                  onMouseEnter={e => { if (!isSelected) e.currentTarget.style.background = 'rgba(30,41,59,0.5)'; }}
                  onMouseLeave={e => { if (!isSelected) e.currentTarget.style.background = 'transparent'; }}
                >
                  <td style={{ padding: '6px 8px', color: T.text, fontFamily: "'JetBrains Mono', monospace" }}>{fmtTime(w.window_ts)}</td>
                  <td style={{ padding: '6px 8px', textAlign: 'center' }}>
                    <span style={{
                      display: 'inline-block', padding: '2px 8px', borderRadius: 2,
                      fontSize: 9, fontWeight: 700,
                      background: `${statusColor}20`,
                      color: statusColor,
                      border: `1px solid ${statusColor}40`,
                    }}>{statusLabel}</span>
                  </td>
                  {/* v9.0 Source Agreement badge */}
                  <td style={{ padding: '6px 8px', textAlign: 'center' }}>
                    {w.source_agreement === true && (
                      <span style={{ color: T.green, fontWeight: 700, fontSize: 12 }}>{'\u2713'}</span>
                    )}
                    {w.source_agreement === false && (
                      <span style={{ color: T.red, fontWeight: 700, fontSize: 12 }}>{'\u2717'}</span>
                    )}
                    {w.source_agreement == null && (
                      <span style={{ color: T.textDim }}>{'\u2014'}</span>
                    )}
                  </td>
                  <td style={{ padding: '6px 8px', textAlign: 'center', color: w.direction === 'UP' ? T.green : T.red }}>
                    {w.direction || '\u2014'}
                  </td>
                  {/* v9.0 Eval Tier badge */}
                  <td style={{ padding: '6px 8px', textAlign: 'center' }}>
                    {w.eval_tier === 'EARLY_CASCADE' && (
                      <span style={{ fontSize: 8, padding: '1px 6px', borderRadius: 2, background: 'rgba(245,158,11,0.15)', color: T.amber, border: '1px solid rgba(245,158,11,0.3)' }}>EARLY</span>
                    )}
                    {w.eval_tier === 'GOLDEN' && (
                      <span style={{ fontSize: 8, padding: '1px 6px', borderRadius: 2, background: 'rgba(168,85,247,0.15)', color: T.purple, border: '1px solid rgba(168,85,247,0.3)' }}>GOLDEN</span>
                    )}
                    {!w.eval_tier && <span style={{ color: T.textDim }}>{'\u2014'}</span>}
                  </td>
                  {/* v9.0 Order Type */}
                  <td style={{ padding: '6px 8px', textAlign: 'center', color: w.order_type === 'FAK' ? T.purple : T.textMuted, fontFamily: "'JetBrains Mono', monospace" }}>
                    {w.order_type || '\u2014'}
                  </td>
                  <td style={{ padding: '6px 8px', textAlign: 'right', color: T.text, fontFamily: "'JetBrains Mono', monospace" }}>{w.vpin?.toFixed(3) ?? '\u2014'}</td>
                  <td style={{ padding: '6px 8px', textAlign: 'right', color: pnlColor, fontWeight: 600, fontFamily: "'JetBrains Mono', monospace" }}>
                    {fmtUsd(pnl)}
                  </td>
                  <td style={{
                    padding: '6px 8px', color: T.textMuted,
                    maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>{w.skip_reason || (w.trade_placed ? 'Executed' : '\u2014')}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
