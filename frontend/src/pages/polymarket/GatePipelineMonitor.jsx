import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, fmt, utcHHMM } from './components/theme.js';
import { STRATEGIES, STRATEGY_LIST, GATES, STRATEGY_GATES } from '../../constants/strategies.js';

/**
 * Gate Pipeline Monitor — Per-strategy gate evaluation results.
 *
 * Shows recent window evaluations with per-gate pass/fail chips.
 * Data source: /api/v58/strategy-decisions (metadata_json has gate results)
 *
 * Route: /polymarket/gate-monitor
 */

const POLL_MS = 15000;

const S = {
  page: {
    minHeight: '100vh', background: T.bg, color: T.text,
    padding: '16px 20px', fontFamily: T.mono,
  },
  header: {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    marginBottom: 16,
  },
  title: { fontSize: 16, fontWeight: 800, color: T.text },
  subtitle: { fontSize: 10, color: T.textMuted, letterSpacing: '0.06em' },
  filterRow: {
    display: 'flex', gap: 6, marginBottom: 16, flexWrap: 'wrap',
  },
  filterPill: (active, color) => ({
    padding: '4px 12px', borderRadius: 4, fontSize: 10, cursor: 'pointer',
    fontFamily: T.mono, fontWeight: active ? 700 : 400, border: 'none',
    background: active ? `${color}22` : 'rgba(51,65,85,0.3)',
    color: active ? color : T.textMuted,
  }),
  card: {
    background: T.card, border: `1px solid ${T.cardBorder}`,
    borderRadius: 6, marginBottom: 8, overflow: 'hidden',
  },
  windowRow: (isHighlight) => ({
    display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px',
    borderBottom: `1px solid ${T.cardBorder}`,
    background: isHighlight ? 'rgba(16,185,129,0.04)' : 'transparent',
  }),
  windowTime: {
    fontSize: 10, fontWeight: 700, color: T.text, fontFamily: T.mono,
    minWidth: 50,
  },
  actionBadge: (action) => ({
    fontSize: 8, fontWeight: 700, padding: '2px 8px', borderRadius: 3,
    fontFamily: T.mono,
    background: action === 'TRADE' ? 'rgba(16,185,129,0.15)' : 'rgba(71,85,105,0.15)',
    color: action === 'TRADE' ? T.green : T.textMuted,
    minWidth: 42, textAlign: 'center',
  }),
  dirBadge: (dir) => ({
    fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3,
    fontFamily: T.mono,
    color: dir === 'UP' ? T.green : dir === 'DOWN' ? T.red : T.textMuted,
  }),
  gateChips: {
    display: 'flex', gap: 3, flexWrap: 'wrap', flex: 1,
  },
  gateChip: (status) => {
    const colors = {
      pass: { bg: 'rgba(16,185,129,0.12)', text: T.green },
      fail: { bg: 'rgba(239,68,68,0.12)', text: T.red },
      skip: { bg: 'rgba(51,65,85,0.3)', text: T.textDim },
    };
    const c = colors[status] || colors.skip;
    return {
      fontSize: 7, padding: '2px 5px', borderRadius: 2,
      background: c.bg, color: c.text,
      fontWeight: 600, fontFamily: T.mono,
      display: 'flex', alignItems: 'center', gap: 2,
      cursor: status === 'fail' ? 'pointer' : 'default',
    };
  },
  skipReason: {
    fontSize: 8, color: T.textMuted, marginLeft: 8,
    maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  emptyState: {
    textAlign: 'center', padding: 40, color: T.textMuted, fontSize: 11,
  },
  stratSection: {
    marginBottom: 20,
  },
  stratHeader: (color) => ({
    fontSize: 11, fontWeight: 700, color, letterSpacing: '0.06em',
    fontFamily: T.mono, padding: '8px 14px',
    borderBottom: `1px solid ${T.cardBorder}`,
    display: 'flex', alignItems: 'center', gap: 8,
  }),
};

function parseGateResults(decision) {
  // Try to extract gate results from metadata_json
  try {
    const meta = typeof decision.metadata_json === 'string'
      ? JSON.parse(decision.metadata_json)
      : decision.metadata_json;
    if (meta?.gate_results) return meta.gate_results;
  } catch { /* ignore parse errors */ }

  // Fallback: derive from skip_reason
  const skipReason = (decision.skip_reason || '').toLowerCase();
  if (!skipReason) return null;

  return { _derived: true, skip_reason: decision.skip_reason };
}

function GateResultChips({ decision, strategyId }) {
  const gates = STRATEGY_GATES[strategyId] || [];
  const gateResults = parseGateResults(decision);
  const skipReason = (decision.skip_reason || '').toLowerCase();

  if (gates.length === 0) {
    // Custom hook strategies -- just show action
    return (
      <div style={S.gateChips}>
        <span style={{ fontSize: 8, color: T.textDim, fontStyle: 'italic' }}>
          hook-based
        </span>
      </div>
    );
  }

  return (
    <div style={S.gateChips}>
      {gates.map(gateId => {
        const gate = GATES[gateId];
        let status = 'skip';

        if (gateResults && !gateResults._derived) {
          // We have actual gate results from metadata
          const result = gateResults[gateId];
          if (result === true || result?.passed === true) status = 'pass';
          else if (result === false || result?.passed === false) status = 'fail';
        } else if (decision.action === 'TRADE') {
          // All gates passed if we got a TRADE
          status = 'pass';
        } else if (skipReason) {
          // Heuristic: check if skip_reason mentions this gate
          const gateLabel = (gate?.label || gateId).toLowerCase();
          if (skipReason.includes(gateId) || skipReason.includes(gateLabel)) {
            status = 'fail';
          } else {
            // Gates before the failing one likely passed
            status = 'pass';
          }
        }

        const failReason = status === 'fail' ? decision.skip_reason : null;

        return (
          <span
            key={gateId}
            style={S.gateChip(status)}
            title={failReason || `${gate?.label}: ${status}`}
          >
            {status === 'pass' ? '\u2705' : status === 'fail' ? '\u274C' : '\u25CB'}
            {gate?.label || gateId}
          </span>
        );
      })}
    </div>
  );
}

export default function GatePipelineMonitor() {
  const api = useApi();
  const [decisions, setDecisions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState('all');
  const [hoveredSkip, setHoveredSkip] = useState(null);

  const fetchDecisions = useCallback(async () => {
    if (!api) return;
    try {
      const res = await api.get('/api/v58/strategy-decisions?limit=100');
      if (Array.isArray(res)) {
        setDecisions(res);
      } else if (res?.decisions) {
        setDecisions(res.decisions);
      }
    } catch { /* endpoint may not exist yet */ }
    setLoading(false);
  }, [api]);

  useEffect(() => {
    fetchDecisions();
    const iv = setInterval(fetchDecisions, POLL_MS);
    return () => clearInterval(iv);
  }, [fetchDecisions]);

  // Group decisions by window_ts then by strategy
  const grouped = useMemo(() => {
    const windows = {};
    for (const d of decisions) {
      const wts = d.window_ts || 0;
      if (!windows[wts]) windows[wts] = {};
      windows[wts][d.strategy_id || 'unknown'] = d;
    }
    // Sort by window_ts descending
    return Object.entries(windows)
      .sort(([a], [b]) => Number(b) - Number(a))
      .slice(0, 30);
  }, [decisions]);

  // Strategies to show based on filter
  const visibleStrategies = useMemo(() => {
    if (filter === 'all') return STRATEGY_LIST;
    return STRATEGY_LIST.filter(s => s.id === filter);
  }, [filter]);

  return (
    <div style={S.page}>
      <div style={S.header}>
        <div>
          <div style={S.title}>Gate Pipeline Monitor</div>
          <div style={S.subtitle}>
            Per-strategy gate evaluation results for recent windows
          </div>
        </div>
      </div>

      {/* Strategy filter pills */}
      <div style={S.filterRow}>
        <button
          style={S.filterPill(filter === 'all', T.cyan)}
          onClick={() => setFilter('all')}
        >
          All
        </button>
        {STRATEGY_LIST.map(s => (
          <button
            key={s.id}
            style={S.filterPill(filter === s.id, s.color)}
            onClick={() => setFilter(s.id)}
          >
            {s.shortLabel}
          </button>
        ))}
      </div>

      {loading && (
        <div style={S.emptyState}>Loading gate evaluations...</div>
      )}

      {!loading && grouped.length === 0 && (
        <div style={S.emptyState}>
          No strategy decisions found. The engine may not be running or
          the /api/v58/strategy-decisions endpoint may not be available.
        </div>
      )}

      {!loading && grouped.length > 0 && (
        <div>
          {/* Table header */}
          <div style={{
            display: 'flex', gap: 10, padding: '6px 14px',
            fontSize: 9, fontWeight: 600, color: T.textMuted,
            letterSpacing: '0.06em', borderBottom: `1px solid ${T.cardBorder}`,
          }}>
            <span style={{ minWidth: 50 }}>WINDOW</span>
            <span style={{ minWidth: 60 }}>STRATEGY</span>
            <span style={{ minWidth: 50 }}>ACTION</span>
            <span style={{ minWidth: 30 }}>DIR</span>
            <span style={{ flex: 1 }}>GATE RESULTS</span>
            <span style={{ minWidth: 150 }}>SKIP REASON</span>
          </div>

          {grouped.map(([wts, stratMap]) => (
            <div key={wts} style={S.card}>
              {visibleStrategies.map(strat => {
                const d = stratMap[strat.id];
                if (!d) return null;

                return (
                  <div
                    key={strat.id}
                    style={S.windowRow(d.action === 'TRADE')}
                  >
                    <span style={S.windowTime}>{utcHHMM(wts)}</span>
                    <span style={{
                      fontSize: 9, fontWeight: 700, color: strat.color,
                      fontFamily: T.mono, minWidth: 60,
                    }}>
                      {strat.shortLabel}
                    </span>
                    <span style={S.actionBadge(d.action)}>
                      {d.action || 'SKIP'}
                    </span>
                    <span style={S.dirBadge(d.direction)}>
                      {d.direction || '--'}
                    </span>
                    <GateResultChips decision={d} strategyId={strat.id} />
                    <span
                      style={S.skipReason}
                      title={d.skip_reason || ''}
                    >
                      {d.skip_reason ? d.skip_reason.slice(0, 40) : ''}
                    </span>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
