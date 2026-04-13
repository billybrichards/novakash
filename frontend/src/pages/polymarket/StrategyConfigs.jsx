import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, fmt, pct } from './components/theme.js';
import { STRATEGIES, STRATEGY_LIST, GATES, STRATEGY_GATES } from '../../constants/strategies.js';

/**
 * Strategy Configs — Card grid showing all 5 strategies with gate pipelines.
 *
 * Each card: strategy name (colored), mode badge, direction, asset, timescale.
 * Gate pipeline visualization: horizontal strip of gate badges.
 * Click to expand: full config details, gate params, sizing, description.
 * Win rate and trade count from /v58/strategy-comparison endpoint.
 *
 * Route: /polymarket/strategies
 *
 * TODO: When GET /api/strategies endpoint lands (CA-07), replace static
 * STRATEGIES constant with dynamic data from the registry.
 */

const MODE_COLORS = {
  LIVE: { bg: 'rgba(16,185,129,0.15)', text: '#10b981' },
  GHOST: { bg: 'rgba(168,85,247,0.12)', text: '#a855f7' },
  DISABLED: { bg: 'rgba(100,116,139,0.12)', text: '#64748b' },
  OFF: { bg: 'rgba(100,116,139,0.12)', text: '#64748b' },
};

const DIR_COLORS = {
  UP: T.green,
  DOWN: T.red,
  ANY: T.cyan,
};

const S = {
  page: {
    minHeight: '100vh', background: T.bg, color: T.text,
    padding: '16px 20px', fontFamily: T.mono,
  },
  header: {
    display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20,
  },
  title: {
    fontSize: 16, fontWeight: 800, color: T.text,
  },
  subtitle: {
    fontSize: 10, color: T.textMuted, letterSpacing: '0.06em',
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))',
    gap: 12,
  },
  card: (color, expanded) => ({
    background: T.card,
    border: `1px solid ${expanded ? color : T.cardBorder}`,
    borderRadius: 6, padding: 16, cursor: 'pointer',
    borderLeft: `3px solid ${color}`,
    transition: 'border-color 0.15s',
  }),
  cardHeader: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    marginBottom: 10,
  },
  stratName: (color) => ({
    fontSize: 12, fontWeight: 800, color, letterSpacing: '0.04em',
    fontFamily: T.mono,
  }),
  badge: (mode) => {
    const mc = MODE_COLORS[mode] || MODE_COLORS.DISABLED;
    return {
      fontSize: 8, fontWeight: 700, padding: '2px 8px', borderRadius: 3,
      background: mc.bg, color: mc.text, letterSpacing: '0.06em',
      fontFamily: T.mono, textTransform: 'uppercase',
    };
  },
  metaRow: {
    display: 'flex', gap: 12, marginBottom: 8, flexWrap: 'wrap',
  },
  metaItem: (color) => ({
    fontSize: 9, color: color || T.textMuted, fontFamily: T.mono,
  }),
  description: {
    fontSize: 9, color: T.textMuted, lineHeight: 1.5, marginBottom: 10,
  },
  gateStrip: {
    display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 8,
  },
  gateChip: (active) => ({
    fontSize: 8, padding: '2px 6px', borderRadius: 3,
    background: active ? 'rgba(6,182,212,0.12)' : 'rgba(51,65,85,0.3)',
    color: active ? T.cyan : T.textDim,
    fontWeight: 600, fontFamily: T.mono,
    display: 'flex', alignItems: 'center', gap: 3,
  }),
  expandedSection: {
    marginTop: 12, paddingTop: 12,
    borderTop: `1px solid ${T.cardBorder}`,
  },
  detailRow: {
    display: 'flex', justifyContent: 'space-between', marginBottom: 4,
  },
  detailLabel: {
    fontSize: 9, color: T.textMuted, fontFamily: T.mono,
  },
  detailValue: {
    fontSize: 9, color: T.text, fontFamily: T.mono, fontWeight: 600,
  },
  statsRow: {
    display: 'flex', gap: 16, marginTop: 8,
  },
  statBox: {
    flex: 1, padding: '8px 10px', borderRadius: 4,
    background: 'rgba(15,23,42,0.5)', textAlign: 'center',
  },
  statLabel: {
    fontSize: 8, color: T.textMuted, textTransform: 'uppercase',
    letterSpacing: '0.06em', marginBottom: 2,
  },
  statValue: (color) => ({
    fontSize: 14, fontWeight: 800, color: color || T.text,
    fontFamily: T.mono,
  }),
};

function GateStrip({ strategyId }) {
  const gates = STRATEGY_GATES[strategyId] || [];
  if (gates.length === 0) {
    return (
      <div style={S.gateStrip}>
        <span style={{ fontSize: 8, color: T.textDim, fontStyle: 'italic' }}>
          Custom hook evaluation (no declarative gates)
        </span>
      </div>
    );
  }
  return (
    <div style={S.gateStrip}>
      {gates.map(gateId => {
        const gate = GATES[gateId];
        return (
          <span key={gateId} style={S.gateChip(true)} title={gate?.description || gateId}>
            <span>{gate?.icon || ''}</span>
            <span>{gate?.label || gateId}</span>
          </span>
        );
      })}
    </div>
  );
}

function StrategyCard({ strat, stats, expanded, onToggle }) {
  const mode = stats?.mode || strat.defaultMode;
  const winRate = stats?.win_rate;
  const tradeCount = stats?.trade_count;
  const skipCount = stats?.skip_count;

  return (
    <div style={S.card(strat.color, expanded)} onClick={onToggle}>
      {/* Header */}
      <div style={S.cardHeader}>
        <span style={S.stratName(strat.color)}>{strat.label}</span>
        <span style={S.badge(mode)}>{mode}</span>
      </div>

      {/* Meta: direction, asset, timescale */}
      <div style={S.metaRow}>
        <span style={S.metaItem(DIR_COLORS[strat.direction])}>
          {strat.direction || 'ANY'}
        </span>
        <span style={S.metaItem()}>
          {strat.asset} {strat.timescale}
        </span>
        {strat.gateLabel && (
          <span style={S.metaItem()}>
            {strat.gateLabel}
          </span>
        )}
      </div>

      {/* Description */}
      <div style={S.description}>{strat.description}</div>

      {/* Gate pipeline strip */}
      <div style={{ fontSize: 9, fontWeight: 600, color: T.cyan, marginBottom: 4, letterSpacing: '0.06em' }}>
        GATE PIPELINE
      </div>
      <GateStrip strategyId={strat.id} />

      {/* Stats row */}
      {(winRate != null || tradeCount != null) && (
        <div style={S.statsRow}>
          <div style={S.statBox}>
            <div style={S.statLabel}>Win Rate</div>
            <div style={S.statValue(winRate >= 0.8 ? T.green : winRate >= 0.6 ? T.amber : T.red)}>
              {winRate != null ? pct(winRate) : '--'}
            </div>
          </div>
          <div style={S.statBox}>
            <div style={S.statLabel}>Trades</div>
            <div style={S.statValue()}>{tradeCount ?? '--'}</div>
          </div>
          <div style={S.statBox}>
            <div style={S.statLabel}>Skips</div>
            <div style={S.statValue(T.textMuted)}>{skipCount ?? '--'}</div>
          </div>
        </div>
      )}

      {/* Expanded details */}
      {expanded && (
        <div style={S.expandedSection}>
          <div style={{ fontSize: 9, fontWeight: 600, color: T.cyan, marginBottom: 8, letterSpacing: '0.06em' }}>
            CONFIGURATION
          </div>
          {strat.configKey && (
            <div style={S.detailRow}>
              <span style={S.detailLabel}>Config Key</span>
              <span style={S.detailValue}>{strat.configKey}</span>
            </div>
          )}
          {Object.keys(strat.thresholds || {}).length > 0 && (
            <>
              <div style={{ ...S.detailLabel, marginTop: 8, marginBottom: 4 }}>Thresholds</div>
              {Object.entries(strat.thresholds).map(([k, v]) => (
                <div key={k} style={S.detailRow}>
                  <span style={S.detailLabel}>{k}</span>
                  <span style={S.detailValue}>
                    {Array.isArray(v) ? v.join(', ') : String(v)}
                  </span>
                </div>
              ))}
            </>
          )}

          {/* Gate details */}
          {(STRATEGY_GATES[strat.id] || []).length > 0 && (
            <>
              <div style={{ ...S.detailLabel, marginTop: 12, marginBottom: 4 }}>Gate Details</div>
              {(STRATEGY_GATES[strat.id] || []).map(gateId => {
                const gate = GATES[gateId];
                return (
                  <div key={gateId} style={{ ...S.detailRow, marginBottom: 6 }}>
                    <span style={S.detailLabel}>
                      {gate?.icon} {gate?.label || gateId}
                    </span>
                    <span style={{ ...S.detailValue, color: T.textMuted, fontWeight: 400 }}>
                      {gate?.description || ''}
                    </span>
                  </div>
                );
              })}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default function StrategyConfigs() {
  const api = useApi();
  const [expanded, setExpanded] = useState(null);
  const [stratStats, setStratStats] = useState({});
  const [modes, setModes] = useState({});

  // Fetch strategy comparison data for win rates
  useEffect(() => {
    if (!api) return;
    (async () => {
      try {
        const res = await api.get('/api/v58/strategy-comparison?hours=168');
        if (res?.strategies) {
          const stats = {};
          for (const s of res.strategies) {
            stats[s.strategy_id] = {
              win_rate: s.win_rate,
              trade_count: s.trade_count,
              skip_count: s.skip_count,
            };
          }
          setStratStats(stats);
        }
      } catch { /* endpoint may not exist yet */ }
    })();
  }, [api]);

  // Fetch current modes from config
  useEffect(() => {
    if (!api) return;
    (async () => {
      try {
        const res = await api.get('/api/v58/config?service=engine');
        const keys = res?.keys || [];
        const m = {};
        for (const strat of STRATEGY_LIST) {
          if (!strat.configKey) continue;
          const found = keys.find(k => k.key === strat.configKey);
          m[strat.id] = (found?.current_value ?? strat.defaultMode).toUpperCase();
        }
        setModes(m);
      } catch {
        const m = {};
        for (const strat of STRATEGY_LIST) m[strat.id] = strat.defaultMode;
        setModes(m);
      }
    })();
  }, [api]);

  const toggle = useCallback((id) => {
    setExpanded(prev => prev === id ? null : id);
  }, []);

  return (
    <div style={S.page}>
      <div style={S.header}>
        <div>
          <div style={S.title}>Strategy Configs</div>
          <div style={S.subtitle}>
            {STRATEGY_LIST.length} strategies registered
            {' \u00b7 '}
            {Object.values(modes).filter(m => m === 'LIVE').length} LIVE
            {' \u00b7 '}
            {Object.values(modes).filter(m => m === 'GHOST').length} GHOST
          </div>
        </div>
      </div>

      <div style={S.grid}>
        {STRATEGY_LIST.map(strat => (
          <StrategyCard
            key={strat.id}
            strat={strat}
            stats={{
              ...stratStats[strat.id],
              mode: modes[strat.id] || strat.defaultMode,
            }}
            expanded={expanded === strat.id}
            onToggle={() => toggle(strat.id)}
          />
        ))}
      </div>
    </div>
  );
}
