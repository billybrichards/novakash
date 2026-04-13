import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, utcHHMM } from './components/theme.js';
import { STRATEGIES, STRATEGY_LIST } from '../../constants/strategies.js';

/**
 * Gate Pipeline Monitor — Window History Table.
 *
 * Each row = one completed window (grouped by window_ts).
 * Columns: WINDOW | ACTUAL | DN | ASIAN | V4F | V10 | (more if filter = all)
 * Each strategy cell shows the best decision for that window:
 *   TRADE↑/↓ or SKIP with reason on hover.
 * WIN/LOSS shown for TRADE decisions.
 *
 * Data source: /api/v58/strategy-decisions?limit=200
 * Filter tabs: ALL | DN | ASIAN | V4F | V10
 *
 * Route: /polymarket/gate-monitor
 */

const POLL_MS = 15000;

// Strategies to show in the table (ordered)
const TABLE_STRATEGIES = [
  STRATEGIES.v4_down_only,
  STRATEGIES.v4_up_asian,
  STRATEGIES.v4_fusion,
  STRATEGIES.v10_gate,
];

// Filter pills: map filter id -> strategy id
const FILTERS = [
  { id: 'all', label: 'ALL', color: T.cyan },
  { id: 'v4_down_only', label: 'DN', color: STRATEGIES.v4_down_only.color },
  { id: 'v4_up_asian', label: 'ASIAN', color: STRATEGIES.v4_up_asian.color },
  { id: 'v4_fusion', label: 'V4F', color: STRATEGIES.v4_fusion.color },
  { id: 'v10_gate', label: 'V10', color: STRATEGIES.v10_gate.color },
];

const S = {
  page: {
    minHeight: '100vh', background: T.bg, color: T.text,
    padding: '16px 20px', fontFamily: T.mono,
  },
  header: {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    marginBottom: 12,
  },
  title: { fontSize: 16, fontWeight: 800, color: T.text },
  subtitle: { fontSize: 10, color: T.textMuted, letterSpacing: '0.06em', marginTop: 2 },
  filterRow: {
    display: 'flex', gap: 6, marginBottom: 14, flexWrap: 'wrap',
    alignItems: 'center',
  },
  filterPill: (active, color) => ({
    padding: '4px 12px', borderRadius: 4, fontSize: 10, cursor: 'pointer',
    fontFamily: T.mono, fontWeight: active ? 700 : 400, border: 'none',
    background: active ? `${color}22` : 'rgba(51,65,85,0.3)',
    color: active ? color : T.textMuted,
  }),
  statsRow: {
    display: 'flex', gap: 16, marginBottom: 14, flexWrap: 'wrap',
  },
  statChip: {
    fontSize: 9, color: T.textMuted, fontFamily: T.mono,
  },
  statVal: {
    color: T.text, fontWeight: 600,
  },
  tableWrap: {
    background: T.card, border: `1px solid ${T.cardBorder}`,
    borderRadius: 6, overflow: 'auto',
  },
  th: (color) => ({
    padding: '7px 10px', fontSize: 9, fontFamily: T.mono,
    borderBottom: `1px solid ${T.cardBorder}`,
    color: color || T.textMuted, fontWeight: 700, letterSpacing: '0.05em',
    textTransform: 'uppercase', position: 'sticky', top: 0,
    background: T.headerBg, zIndex: 1, whiteSpace: 'nowrap',
    textAlign: 'center',
  }),
  thLeft: {
    padding: '7px 10px', fontSize: 9, fontFamily: T.mono,
    borderBottom: `1px solid ${T.cardBorder}`,
    color: T.textMuted, fontWeight: 700, letterSpacing: '0.05em',
    textTransform: 'uppercase', position: 'sticky', top: 0,
    background: T.headerBg, zIndex: 1, whiteSpace: 'nowrap',
    textAlign: 'left',
  },
  td: {
    padding: '5px 10px', fontSize: 10, fontFamily: T.mono,
    borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap',
    textAlign: 'center',
  },
  tdLeft: {
    padding: '5px 10px', fontSize: 10, fontFamily: T.mono,
    borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap',
    textAlign: 'left',
  },
  emptyState: {
    textAlign: 'center', padding: 40, color: T.textMuted, fontSize: 11,
  },
};

/** Pick the "best" decision for a strategy in a window. Prefer sweet-spot offset 90-150. */
function bestDecision(candidates) {
  if (!candidates || candidates.length === 0) return null;
  if (candidates.length === 1) return candidates[0];
  const inSweet = (d) => (d.eval_offset || 0) >= 90 && (d.eval_offset || 0) <= 150;
  const sweet = candidates.filter(inSweet);
  if (sweet.length > 0) {
    return sweet.reduce((a, b) =>
      Math.abs((a.eval_offset || 0) - 120) <= Math.abs((b.eval_offset || 0) - 120) ? a : b
    );
  }
  return candidates[0];
}

/** Render a strategy cell: TRADE↑ / SKIP(reason) */
function StratCell({ decision, stratColor }) {
  if (!decision) {
    return (
      <td style={{ ...S.td, color: T.textDim }}>--</td>
    );
  }

  const isTrade = decision.action === 'TRADE';
  const dir = decision.direction === 'UP' ? '\u2191' : decision.direction === 'DOWN' ? '\u2193' : '';
  const isWin = decision.outcome === 'WIN' || decision.resolved_win === true;
  const isLoss = decision.outcome === 'LOSS' || decision.resolved_win === false;

  // For TRADE decisions, check outcome
  let outcomeMarker = null;
  if (isTrade) {
    if (isWin) outcomeMarker = <span style={{ color: T.green, marginLeft: 4 }}>\u2705</span>;
    else if (isLoss) outcomeMarker = <span style={{ color: T.red, marginLeft: 4 }}>\u274C</span>;
    else outcomeMarker = <span style={{ color: T.textDim, marginLeft: 4, fontSize: 8 }}>?</span>;
  }

  const label = isTrade ? `TRADE${dir}` : 'SKIP';
  const reason = decision.skip_reason || decision.entry_reason || '';

  return (
    <td
      style={{ ...S.td, cursor: reason ? 'help' : 'default' }}
      title={reason || undefined}
    >
      <span style={{
        fontSize: 9, fontWeight: isTrade ? 700 : 400,
        color: isTrade ? stratColor : T.textDim,
      }}>
        {label}
      </span>
      {outcomeMarker}
      {!isTrade && reason && (
        <span style={{
          display: 'block', fontSize: 7, color: T.textDim,
          maxWidth: 90, overflow: 'hidden', textOverflow: 'ellipsis',
          whiteSpace: 'nowrap', marginTop: 1,
        }} title={reason}>
          {reason.slice(0, 20)}
        </span>
      )}
    </td>
  );
}

export default function GatePipelineMonitor() {
  const api = useApi();
  const [decisions, setDecisions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [lastFetch, setLastFetch] = useState(null);
  const [filter, setFilter] = useState('all');

  const fetchDecisions = useCallback(async () => {
    if (!api) return;
    try {
      const res = await api.get('/v58/strategy-decisions?limit=200');
      const data = res.data;
      if (Array.isArray(data)) {
        setDecisions(data);
      } else if (data?.decisions) {
        setDecisions(data.decisions);
      } else if (Array.isArray(res)) {
        setDecisions(res);
      }
      setLastFetch(new Date());
    } catch { /* endpoint may not exist yet */ }
    setLoading(false);
  }, [api]);

  useEffect(() => {
    fetchDecisions();
    const iv = setInterval(fetchDecisions, POLL_MS);
    return () => clearInterval(iv);
  }, [fetchDecisions]);

  // Group decisions by window_ts
  // windowMap: { [window_ts]: { [strategy_id]: Decision[] } }
  const windowMap = useMemo(() => {
    const map = {};
    for (const d of decisions) {
      const wts = d.window_ts || 0;
      if (!map[wts]) map[wts] = {};
      const sid = d.strategy_id || d.strategy_name || 'unknown';
      if (!map[wts][sid]) map[wts][sid] = [];
      map[wts][sid].push(d);
    }
    return map;
  }, [decisions]);

  // Sorted window timestamps (newest first), up to 50
  const windowKeys = useMemo(() =>
    Object.keys(windowMap)
      .map(Number)
      .sort((a, b) => b - a)
      .slice(0, 50),
    [windowMap]
  );

  // Strategies visible in the table based on filter
  const visibleStrategies = useMemo(() => {
    if (filter === 'all') return TABLE_STRATEGIES;
    return TABLE_STRATEGIES.filter(s => s.id === filter);
  }, [filter]);

  // Stats: count TRADE decisions, WIN rate across all visible strategies
  const stats = useMemo(() => {
    let trades = 0, wins = 0, losses = 0;
    for (const wts of windowKeys) {
      for (const strat of visibleStrategies) {
        const d = bestDecision(windowMap[wts]?.[strat.id]);
        if (d?.action === 'TRADE') {
          trades++;
          if (d.outcome === 'WIN' || d.resolved_win === true) wins++;
          else if (d.outcome === 'LOSS' || d.resolved_win === false) losses++;
        }
      }
    }
    const wr = trades > 0 ? Math.round((wins / trades) * 100) : null;
    return { trades, wins, losses, wr };
  }, [windowKeys, windowMap, visibleStrategies]);

  return (
    <div style={S.page}>
      <div style={S.header}>
        <div>
          <div style={S.title}>Window History</div>
          <div style={S.subtitle}>
            Per-strategy decisions grouped by window · {windowKeys.length} windows loaded · {decisions.length} rows
            {lastFetch && (
              <span style={{ marginLeft: 8, color: T.textDim }}>
                Updated {lastFetch.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Filter pills */}
      <div style={S.filterRow}>
        {FILTERS.map(f => (
          <button
            key={f.id}
            style={S.filterPill(filter === f.id, f.color)}
            onClick={() => setFilter(f.id)}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Stats row */}
      {stats.trades > 0 && (
        <div style={S.statsRow}>
          <span style={S.statChip}>
            Trades: <span style={S.statVal}>{stats.trades}</span>
          </span>
          <span style={S.statChip}>
            W/L: <span style={{ ...S.statVal, color: T.green }}>{stats.wins}W</span>
            {' / '}
            <span style={{ ...S.statVal, color: T.red }}>{stats.losses}L</span>
          </span>
          {stats.wr != null && (
            <span style={S.statChip}>
              Win Rate: <span style={{
                ...S.statVal,
                color: stats.wr >= 70 ? T.green : stats.wr >= 55 ? T.amber : T.red,
              }}>{stats.wr}%</span>
            </span>
          )}
        </div>
      )}

      {loading && (
        <div style={S.emptyState}>Loading window history...</div>
      )}

      {!loading && windowKeys.length === 0 && (
        <div style={S.emptyState}>
          <div style={{ marginBottom: 8 }}>No strategy decisions found.</div>
          <div style={{ fontSize: 9, color: T.textDim }}>
            The engine may not be running or the /api/v58/strategy-decisions endpoint may not be available.
          </div>
        </div>
      )}

      {!loading && windowKeys.length > 0 && (
        <div style={S.tableWrap}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={S.thLeft}>WINDOW</th>
                <th style={{ ...S.th(), color: T.textMuted }}>ACTUAL</th>
                {visibleStrategies.map(s => (
                  <th key={s.id} style={S.th(s.color)}>{s.shortLabel}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {windowKeys.map((wts, i) => {
                const stratMap = windowMap[wts] || {};

                // Determine actual direction from any decision that has it
                const anyDecision = Object.values(stratMap).flat()[0];
                const actual = anyDecision?.actual_direction || anyDecision?.oracle_direction || null;
                const actualColor = actual === 'UP' ? T.green : actual === 'DOWN' ? T.red : T.textDim;

                return (
                  <tr
                    key={wts}
                    style={{
                      background: i % 2 === 0 ? 'transparent' : 'rgba(15,23,42,0.3)',
                    }}
                  >
                    <td style={{ ...S.tdLeft, color: T.text, fontWeight: 600 }}>
                      {utcHHMM(wts)}
                    </td>
                    <td style={{ ...S.td, color: actualColor, fontWeight: actual ? 600 : 400 }}>
                      {actual || '--'}
                    </td>
                    {visibleStrategies.map(strat => {
                      const candidates = stratMap[strat.id] || [];
                      const d = bestDecision(candidates);
                      return <StratCell key={strat.id} decision={d} stratColor={strat.color} />;
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
