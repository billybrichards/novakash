import React, { useState, useEffect, useCallback } from 'react';
import { T, fmt, utcHHMM } from './theme.js';
import { useApi } from '../../../hooks/useApi.js';
import { STRATEGIES } from '../../../constants/strategies.js';

/**
 * Band 5 — Recent Flow Timeline.
 *
 * Reuses the Factory Floor RECENT FLOW TIMELINE table pattern.
 * Pulls from /api/v58/outcomes. Last 20 windows with columns:
 * TIME | SIGNAL | ACTUAL | SRC | GATES | REASON | V4 | RESULT
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

// Strategy colors and short labels — sourced from shared constants
const STRAT_COLORS = Object.fromEntries(
  Object.entries(STRATEGIES).map(([id, s]) => [id, s.color])
);
const STRAT_SHORT = Object.fromEntries(
  Object.entries(STRATEGIES).map(([id, s]) => [id, s.shortLabel])
);

function StrategyChips({ decisions }) {
  if (!decisions || !decisions.length) return <span style={{ color: T.textDim, fontSize: 8 }}>{'\u2014'}</span>;
  return (
    <div style={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
      {decisions.map((d, i) => {
        const sid = d.strategy_id || '?';
        const color = STRAT_COLORS[sid] || T.textMuted;
        const short = STRAT_SHORT[sid] || sid.slice(0, 4);
        const isTrade = d.action === 'TRADE';
        const dir = d.direction === 'UP' ? '\u2191' : d.direction === 'DOWN' ? '\u2193' : '';
        const label = isTrade ? `${short}${dir}` : short;
        const skip = d.skip_reason || '';
        return (
          <span key={i} title={`${sid}: ${isTrade ? `TRADE ${d.direction}` : `SKIP ${skip}`}`}
            style={{
              fontSize: 7, padding: '0 3px', borderRadius: 2, fontFamily: T.mono,
              fontWeight: isTrade ? 700 : 400,
              background: isTrade ? `${color}25` : 'transparent',
              color: isTrade ? color : T.textDim,
              border: `1px solid ${isTrade ? color : 'transparent'}`,
            }}>
            {label}
          </span>
        );
      })}
    </div>
  );
}

export default function RecentFlow({ outcomes }) {
  const api = useApi();
  const rows = outcomes || [];

  // Fetch V4 strategy decisions and index by window_ts.
  // strategy-decisions returns window_ts as integer epoch seconds.
  // outcomes returns window_ts as ISO string from _row_to_window.
  // We index by both the raw int and ISO string to handle both formats.
  // Fetch ALL strategy decisions (no strategy_id filter) — index by window_ts
  const [stratDecisions, setStratDecisions] = useState({}); // { window_ts: [decisions] }
  const fetchDecisions = useCallback(async () => {
    try {
      const res = await api('GET', '/v58/strategy-decisions?limit=200');
      const data = res?.data || res;
      const list = Array.isArray(data) ? data : (data?.decisions ?? []);
      // Group by (window_ts, strategy_id) — keep best eval_offset per strategy
      // Prefer sweet spot (90-150), then closest to T-120
      const bestPerWindow = {}; // { window_ts: { strategy_id: decision } }
      list.forEach(d => {
        if (d.window_ts == null) return;
        const wts = d.window_ts;
        const sid = d.strategy_id;
        if (!bestPerWindow[wts]) bestPerWindow[wts] = {};
        const existing = bestPerWindow[wts][sid];
        const inSweet = (o) => o >= 90 && o <= 150;
        if (!existing) {
          bestPerWindow[wts][sid] = d;
        } else {
          const eNew = d.eval_offset || 0;
          const eOld = existing.eval_offset || 0;
          if (inSweet(eNew) && !inSweet(eOld)) bestPerWindow[wts][sid] = d;
          else if (inSweet(eNew) && inSweet(eOld) && Math.abs(eNew - 120) < Math.abs(eOld - 120)) bestPerWindow[wts][sid] = d;
        }
      });
      // Convert to array per window_ts
      const byTs = {};
      for (const [wts, strats] of Object.entries(bestPerWindow)) {
        byTs[wts] = Object.values(strats);
        // Also index by ISO string
        try {
          const iso = new Date(Number(wts) * 1000).toISOString().replace('.000Z', '+00:00');
          byTs[iso] = byTs[wts];
          byTs[new Date(Number(wts) * 1000).toISOString()] = byTs[wts];
        } catch (_) {}
      }
      setStratDecisions(byTs);
    } catch (_) {}
  }, [api]);

  useEffect(() => { fetchDecisions(); }, [fetchDecisions]);
  useEffect(() => { if (rows.length > 0) fetchDecisions(); }, [rows.length]); // eslint-disable-line react-hooks/exhaustive-deps

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
        {' \u00b7 '}
        <span style={{ fontWeight: 600, color: T.textMuted }}>V4</span> = V4 strategy decision
      </div>

      {/* Header — prioritise TIME, SIGNAL, ACTUAL, RESULT; truncate REASON */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '52px 46px 46px 30px 70px 70px 110px 46px',
        gap: 4, padding: '4px 0 5px',
        borderBottom: `1px solid ${T.cardBorder}`,
        fontSize: 8, color: T.textDim, letterSpacing: '0.08em',
      }}>
        <span>TIME</span>
        <span>SIGNAL</span>
        <span>ACTUAL</span>
        <span>SRC</span>
        <span>GATES</span>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>REASON</span>
        <span>STRATEGIES</span>
        <span style={{ textAlign: 'right' }}>RESULT</span>
      </div>

      {/* Rows */}
      {rows.length > 0 ? rows.slice(0, 20).map((o, i) => {
        const result = outcomeLabel(o);
        const gateStr = outcomeGateString(o);
        const actual = actualDirection(o);
        // Try all plausible window_ts key formats
        const windowDecs = stratDecisions[o.window_ts] || [];
        const rowBg = result.text === 'WIN'
          ? 'rgba(16,185,129,0.03)'
          : result.text === 'LOSS'
          ? 'rgba(239,68,68,0.03)'
          : 'transparent';

        return (
          <div key={i} style={{
            display: 'grid',
            gridTemplateColumns: '52px 46px 46px 30px 70px 70px 110px 46px',
            gap: 4, padding: '4px 0',
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
            <span style={{ fontSize: 9, color: T.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {o.delta_source || '\u2014'}
            </span>
            <span style={{ fontSize: 9, letterSpacing: '0.02em' }}>{gateStr}</span>
            <span style={{
              fontSize: 8, color: T.textMuted, overflow: 'hidden',
              textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }} title={o.skip_reason || 'traded'}>
              {o.skip_reason ? o.skip_reason.slice(0, 18) : (o.trade_placed ? 'traded' : '\u2014')}
            </span>
            <StrategyChips decisions={windowDecs} />
            <span style={{
              textAlign: 'right', fontWeight: 700, fontSize: 10, color: result.color,
              whiteSpace: 'nowrap',
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
