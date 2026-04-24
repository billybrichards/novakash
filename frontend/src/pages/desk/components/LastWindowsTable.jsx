// /desk — last 20 windows table.
//
// Columns: window · You · v6_sniper · v4_fusion · v5_ensemble · v5_fresh ·
// Consensus · actual · Δ final
//
// Data:
//  - `picks`           : GET /api/desk/picks (operator's choices — picks
//                        do carry actual_direction from the server-side
//                        join via the strategy_decisions_resolved view)
//  - `resolvedByStrat` : GET /api/v58/strategy-decisions?resolved=true per sid
//                        (these rows do NOT carry actual_direction — derive
//                        it from direction + outcome via deriveActualDirection)
//  - `actual` + `Δ final` prefer picks; fall back to the derived value
//    from any resolved strategy row with a known outcome.
//  - Rows with sot_reconciliation_state='engine_optimistic' + outcome='LOSS'
//    are flagged as accounting-only losses (wallet untouched, hub note #64).
//    They still count toward WR from the decision's perspective, but the
//    tooltip tells the operator the wallet didn't move.
//
// Footer: rolling WR per column via `computeWr` from src/lib/wr.js.
// Colour-coded with the canonical `wrColor` helper.

import React, { useMemo } from 'react';
import { T, wrColor } from '../../../theme/tokens.js';
import { computeWr } from '../../../lib/wr.js';
import {
  indexByWindowEpoch,
  deriveActualDirection,
  isAccountingOnlyLoss,
} from '../hooks/useResolvedDecisions.js';

const TRACKED = ['v6_sniper', 'v4_fusion', 'v5_ensemble', 'v5_fresh'];

export default function LastWindowsTable({ picks, resolvedByStrategy }) {
  // Index each strategy's resolved rows by window_epoch.
  const indexed = useMemo(() => {
    const out = {};
    for (const sid of TRACKED) {
      out[sid] = indexByWindowEpoch(resolvedByStrategy?.[sid] || []);
    }
    return out;
  }, [resolvedByStrategy]);

  // Union the window-epochs that appear in picks + any resolved row, take
  // the most recent 20.
  const epochs = useMemo(() => {
    const set = new Set();
    for (const p of picks || []) {
      if (p?.window_epoch != null) set.add(Number(p.window_epoch));
    }
    for (const sid of TRACKED) {
      for (const k of indexed[sid].keys()) set.add(k);
    }
    return [...set].sort((a, b) => b - a).slice(0, 20);
  }, [picks, indexed]);

  // Build rows keyed by epoch.
  const picksByEpoch = useMemo(() => {
    const m = new Map();
    for (const p of picks || []) {
      if (p?.window_epoch != null) m.set(Number(p.window_epoch), p);
    }
    return m;
  }, [picks]);

  const rows = epochs.map(epoch => {
    const pick = picksByEpoch.get(epoch) || null;
    const perStrategy = {};
    let actual = pick?.actual_direction || null;
    let pnl = pick?.pnl_usd ?? null;

    let anyAccountingOnlyLoss = false;
    for (const sid of TRACKED) {
      const r = indexed[sid].get(epoch);
      perStrategy[sid] = r || null;
      // Derive actual from direction + outcome — v58 does not expose it.
      if (!actual && r) {
        actual = deriveActualDirection(r.direction, r.outcome);
      }
      if (r && isAccountingOnlyLoss(r)) anyAccountingOnlyLoss = true;
    }
    // Consensus = majority among tracked strategies' direction.
    const dirs = TRACKED
      .map(sid => perStrategy[sid]?.direction)
      .filter(Boolean);
    const counts = dirs.reduce((acc, d) => (acc[d] = (acc[d] || 0) + 1, acc), {});
    const consensus = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0] || null;

    return { epoch, pick, perStrategy, actual, pnl, consensus, anyAccountingOnlyLoss };
  });

  // Compute WR per column.
  const columnRows = (extract) => rows
    .map(r => {
      const picked = extract(r);
      if (!picked || !r.actual) return null;
      return { outcome: picked === r.actual ? 'WIN' : 'LOSS' };
    })
    .filter(Boolean);

  const wrs = {
    You:          computeWr(columnRows(r => r.pick?.pick && r.pick.pick !== 'SKIP' ? r.pick.pick : null)),
    v6_sniper:    computeWr(columnRows(r => r.perStrategy.v6_sniper?.direction)),
    v4_fusion:    computeWr(columnRows(r => r.perStrategy.v4_fusion?.direction)),
    v5_ensemble:  computeWr(columnRows(r => r.perStrategy.v5_ensemble?.direction)),
    v5_fresh:     computeWr(columnRows(r => r.perStrategy.v5_fresh?.direction)),
    Consensus:    computeWr(columnRows(r => r.consensus)),
  };

  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginTop: 12,
    }}>
      <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em', marginBottom: 8 }}>
        LAST {rows.length} WINDOWS
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11, fontFamily: T.font }}>
          <thead>
            <tr style={{ color: T.label, textAlign: 'left' }}>
              <th style={thStyle}>Window</th>
              <th style={thStyle}>You</th>
              <th style={thStyle}>v6_sniper</th>
              <th style={thStyle}>v4_fusion</th>
              <th style={thStyle}>v5_ensemble</th>
              <th style={thStyle}>v5_fresh</th>
              <th style={thStyle}>Consensus</th>
              <th style={thStyle}>Actual</th>
              <th style={thStyle}>Δ final</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.epoch}>
                <td style={tdStyle}>{fmtWindow(r.epoch)}</td>
                <td style={tdStyle}>
                  <DirCell dir={r.pick?.pick} actual={r.actual} skipLabel="SKIP" />
                </td>
                {TRACKED.map(sid => (
                  <td key={sid} style={tdStyle}>
                    <DirCell dir={r.perStrategy[sid]?.direction} actual={r.actual} />
                  </td>
                ))}
                <td style={tdStyle}>
                  <DirCell dir={r.consensus} actual={r.actual} />
                </td>
                <td style={tdStyle}>
                  {r.actual
                    ? <span style={{ color: r.actual === 'UP' ? T.profit : T.loss }}>
                        {r.actual}
                        {r.anyAccountingOnlyLoss ? (
                          <span
                            title="engine_optimistic + LOSS — accounting-only (wallet untouched per hub note #64)"
                            style={{ marginLeft: 4, color: T.label2, fontSize: 10 }}
                          >†</span>
                        ) : null}
                      </span>
                    : <span style={{ color: T.label }}>—</span>}
                </td>
                <td style={tdStyle}>
                  {r.pnl != null
                    ? <span style={{ color: r.pnl >= 0 ? T.profit : T.loss }}>
                        {r.pnl >= 0 ? '+' : ''}{r.pnl.toFixed(2)}
                      </span>
                    : <span style={{ color: T.label }}>—</span>}
                </td>
              </tr>
            ))}
            {rows.length === 0 ? (
              <tr>
                <td style={tdStyle} colSpan={9}>
                  <span style={{ color: T.label }}>No windows yet — make a pick above.</span>
                </td>
              </tr>
            ) : null}
          </tbody>
          <tfoot>
            <tr style={{ borderTop: `1px solid ${T.borderStrong}` }}>
              <td style={{ ...tdStyle, color: T.label2 }}>WR</td>
              {['You','v6_sniper','v4_fusion','v5_ensemble','v5_fresh','Consensus'].map(k => (
                <td key={k} style={{ ...tdStyle, color: wrColor(wrs[k].wr) }}>
                  {wrs[k].n === 0 ? '—' : `${Math.round(wrs[k].wr * 100)}% (${wrs[k].n})`}
                </td>
              ))}
              <td style={tdStyle} colSpan={2} />
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}

function DirCell({ dir, actual, skipLabel }) {
  if (!dir) return <span style={{ color: T.label }}>—</span>;
  if (dir === 'SKIP') return <span style={{ color: T.label2 }}>{skipLabel || 'SKIP'}</span>;
  let colour = dir === 'UP' ? T.profit : T.loss;
  if (actual && actual !== dir) colour = T.loss;
  if (actual && actual === dir) colour = T.profit;
  return <span style={{ color: colour }}>{dir}</span>;
}

const thStyle = { fontWeight: 500, padding: '2px 6px', letterSpacing: '0.05em' };
const tdStyle = { padding: '3px 6px', borderTop: `1px solid ${T.border}` };

function fmtWindow(epoch) {
  const d = new Date(epoch * 1000);
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${hh}:${mm}Z`;
}
