// /desk — current-window strategy decisions panel.
//
// Pulls GET /api/v58/strategy-decisions?timeframe=5m and filters down to
// the strategies tracked in spec #218 (v6_sniper is the sole LIVE per
// commit 8289e91; others shadow). Each row shows direction + stake +
// conviction. Skip reasons are shown on hover via `title=`.

import React, { useEffect, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';
import { T } from '../../../theme/tokens.js';

const TRACKED = ['v6_sniper', 'v4_fusion', 'v5_ensemble', 'v5_fresh'];

export default function StrategyDecisions({ windowEpoch, pollMs = 4_000 }) {
  const api = useApi();
  const [rows, setRows] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.get('/api/v58/strategy-decisions?timeframe=5m&limit=40');
        const body = r?.data ?? r;
        const raw = Array.isArray(body?.rows)
          ? body.rows
          : Array.isArray(body?.decisions)
            ? body.decisions
            : Array.isArray(body) ? body : [];
        if (!cancelled) {
          setRows(raw);
          setError(null);
          setLoading(false);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e.message || 'decisions fetch failed');
          setLoading(false);
        }
      }
    };
    load();
    const h = setInterval(load, pollMs);
    return () => { cancelled = true; clearInterval(h); };
  }, [api, pollMs]);

  // Pick the most recent row per strategy_id (server returns dedup'd but
  // we may have multiple windows when pollMs < window duration).
  const byStrategy = new Map();
  for (const r of rows) {
    const sid = r.strategy_id;
    if (!sid) continue;
    const prev = byStrategy.get(sid);
    const t = new Date(r.evaluated_at ?? r.window_ts ?? 0).getTime();
    const pt = prev ? new Date(prev.evaluated_at ?? prev.window_ts ?? 0).getTime() : -1;
    if (!prev || t > pt) byStrategy.set(sid, r);
  }

  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em', marginBottom: 8 }}>
        STRATEGY DECISIONS · current window
      </div>
      {error ? <div style={{ color: T.loss, fontSize: 11, marginBottom: 4 }}>{error}</div> : null}
      {loading && !rows.length ? <div style={{ color: T.label, fontSize: 11 }}>Loading…</div> : null}
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11, fontFamily: T.font }}>
        <thead>
          <tr style={{ color: T.label, textAlign: 'left' }}>
            <th style={thStyle}>Strategy</th>
            <th style={thStyle}>Dir</th>
            <th style={thStyle}>Conv</th>
            <th style={thStyle}>Stake</th>
            <th style={thStyle}>Mode</th>
            <th style={thStyle}>Action</th>
          </tr>
        </thead>
        <tbody>
          {TRACKED.map(sid => {
            const r = byStrategy.get(sid);
            if (!r) {
              return (
                <tr key={sid}>
                  <td style={tdStyle}>{sid}</td>
                  <td style={tdStyle} colSpan={5}><span style={{ color: T.label }}>no current decision</span></td>
                </tr>
              );
            }
            const dir = r.direction;
            const dirColour = dir === 'UP' ? T.profit : dir === 'DOWN' ? T.loss : T.label;
            const stake = num(r.entry_cap) ?? num(r.collateral_pct);
            const action = r.action || (r.executed ? 'EXEC' : r.skip_reason ? 'SKIP' : '?');
            return (
              <tr key={sid} title={r.skip_reason || ''}>
                <td style={tdStyle}>{sid}</td>
                <td style={{ ...tdStyle, color: dirColour }}>{dir || '—'}</td>
                <td style={tdStyle}>{r.confidence || (num(r.confidence_score) != null ? num(r.confidence_score).toFixed(2) : '—')}</td>
                <td style={tdStyle}>{stake != null ? stake.toFixed(2) : '—'}</td>
                <td style={tdStyle}>{r.mode || '—'}</td>
                <td style={{ ...tdStyle, color: action === 'SKIP' ? T.label2 : T.text }}>{action}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const thStyle = { fontWeight: 500, padding: '2px 4px', letterSpacing: '0.05em' };
const tdStyle = { padding: '3px 4px', borderTop: `1px solid ${T.border}` };

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
