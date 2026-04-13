import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, fmt, pct } from './components/theme.js';
import WindowAnalysisModal from './components/WindowAnalysisModal.jsx';
import { STRATEGY_LIST } from '../../constants/strategies.js';

/**
 * Strategy Lab — Historical replay and gate impact analysis.
 *
 * Tab A: Historical Replay — toggle gates + adjust thresholds, see what-if W/L.
 * Tab B: Gate Impact Analysis — per-gate counterfactual table.
 *
 * Data source: /api/v58/outcomes?limit=N
 * All replay logic runs client-side in JavaScript.
 */

// ── Strategy Config Panel ───────────────────────────────────────────────────
// Dynamic strategy selector — reads live modes from config API, allows inline
// LIVE / GHOST / OFF toggle per strategy without leaving the page.
// Source of truth: constants/strategies.js

const STRATEGIES_META = STRATEGY_LIST;

const MODE_COLORS = {
  LIVE: { bg: 'rgba(16,185,129,0.15)', color: '#10b981' },
  GHOST: { bg: 'rgba(168,85,247,0.15)', color: '#a855f7' },
  OFF: { bg: 'rgba(100,116,139,0.12)', color: '#64748b' },
};

function StrategyConfigPanel({ api }) {
  const [modes, setModes] = useState({});   // { V4_DOWN_ONLY_MODE: 'LIVE', ... }
  const [saving, setSaving] = useState(null);
  const [error, setError] = useState(null);

  // Fetch current modes on mount
  useEffect(() => {
    if (!api) return;
    (async () => {
      try {
        const res = await api.get('/api/v58/config?service=engine');
        const keys = res?.keys || [];
        const m = {};
        for (const strat of STRATEGIES_META) {
          const found = keys.find(k => k.key === strat.configKey);
          m[strat.configKey] = (found?.current_value ?? strat.defaultMode).toUpperCase();
        }
        setModes(m);
      } catch {
        // Fallback to defaults
        const m = {};
        for (const s of STRATEGIES_META) m[s.configKey] = s.defaultMode;
        setModes(m);
      }
    })();
  }, [api]);

  const setMode = async (configKey, newMode) => {
    if (saving) return;

    // Enforce: direction-exclusive strategies can both be LIVE (they never fire in the
    // same window). Non-directional strategies still require single-LIVE per account.
    // v4_down_only (DOWN) + v4_up_asian (UP) = safe together — mutually exclusive signals.
    if (newMode === 'LIVE') {
      const thisMeta = STRATEGIES_META.find(s => s.configKey === configKey);
      const otherLive = STRATEGIES_META.find(
        s => s.configKey !== configKey && modes[s.configKey] === 'LIVE'
      );
      if (otherLive) {
        // Allow if both strategies are direction-exclusive (one UP, one DOWN)
        const thisDir = thisMeta?.direction;
        const otherDir = otherLive.direction;
        const directionExclusive = thisDir && otherDir && thisDir !== otherDir;
        if (!directionExclusive) {
          setError(`${otherLive.label} is already LIVE. Set it to GHOST first, or use direction-exclusive strategies (UP+DOWN can both be LIVE).`);
          return;
        }
      }
    }

    setSaving(configKey);
    setError(null);
    try {
      await api.post('/api/v58/config/upsert', {
        service: 'engine',
        key: configKey,
        value: newMode,
        reason: `strategy mode set to ${newMode} via StrategyLab`,
      });
      setModes(prev => ({ ...prev, [configKey]: newMode }));
    } catch (e) {
      setError(`Failed to set ${configKey}: ${e.message}`);
    } finally {
      setSaving(null);
    }
  };

  const liveCounts = STRATEGIES_META.filter(s => modes[s.configKey] === 'LIVE').length;

  return (
    <div style={{
      background: 'rgba(15,23,42,0.9)',
      border: `1px solid rgba(51,65,85,1)`,
      borderLeft: `3px solid #10b981`,
      borderRadius: 6,
      marginBottom: 14,
      padding: '10px 14px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <span style={{ fontSize: 9, color: '#10b981', letterSpacing: '0.1em', fontWeight: 700 }}>
          STRATEGY SELECTOR
        </span>
        <span style={{ fontSize: 9, color: T.textMuted }}>
          {liveCounts} LIVE · {STRATEGIES_META.length - liveCounts} GHOST/OFF
        </span>
        {error && (
          <span style={{ fontSize: 9, color: T.red, marginLeft: 8 }}>{error}</span>
        )}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10 }}>
        {STRATEGIES_META.map(strat => {
          const currentMode = modes[strat.configKey] || strat.defaultMode;
          const isSaving = saving === strat.configKey;
          return (
            <div key={strat.id} style={{
              border: `1px solid ${currentMode === 'LIVE' ? strat.color : 'rgba(51,65,85,0.8)'}`,
              borderRadius: 5,
              padding: '8px 10px',
              background: currentMode === 'LIVE' ? `${strat.color}10` : 'transparent',
              transition: 'all 0.15s',
            }}>
              {/* Header */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                <span style={{ fontSize: 10, fontWeight: 700, color: strat.color }}>{strat.label}</span>
                {strat.badge && (
                  <span style={{
                    fontSize: 8, padding: '1px 5px', borderRadius: 2,
                    background: `${strat.color}25`, color: strat.color, fontWeight: 700,
                  }}>{strat.badge}</span>
                )}
              </div>
              <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 8, lineHeight: 1.4 }}>
                {strat.description}
              </div>

              {/* Mode toggle buttons — disabled for strategies not yet deployed */}
              {strat.deployed === false ? (
                <div style={{ fontSize: 9, color: T.textMuted, fontStyle: 'italic', padding: '3px 0' }}>
                  Not yet deployed — ships with CA-07
                </div>
              ) : (
              <div style={{ display: 'flex', gap: 4 }}>
                {['LIVE', 'GHOST', 'OFF'].map(mode => {
                  const active = currentMode === mode;
                  const mc = MODE_COLORS[mode];
                  return (
                    <button
                      key={mode}
                      onClick={() => !active && setMode(strat.configKey, mode)}
                      disabled={isSaving}
                      style={{
                        flex: 1,
                        padding: '3px 0',
                        fontSize: 9,
                        fontFamily: T.mono,
                        fontWeight: active ? 700 : 400,
                        border: `1px solid ${active ? mc.color : 'rgba(51,65,85,0.6)'}`,
                        borderRadius: 3,
                        background: active ? mc.bg : 'transparent',
                        color: active ? mc.color : T.textMuted,
                        cursor: active ? 'default' : 'pointer',
                        opacity: isSaving ? 0.5 : 1,
                        transition: 'all 0.1s',
                      }}
                    >
                      {isSaving && active ? '…' : mode}
                    </button>
                  );
                })}
              </div>
              )}
            </div>
          );
        })}
      </div>

      <div style={{ marginTop: 8, fontSize: 9, color: T.textDim, lineHeight: 1.5 }}>
        LIVE = executes paper/real trades · GHOST = evaluates only, no execution · OFF = disabled.
        Engine picks up mode change within 10s (hot-reload via DB config sync).{' '}
        <span style={{ color: '#10b981' }}>
          Direction-exclusive strategies (DOWN + UP) can both be LIVE simultaneously — they never fire in the same window.
        </span>
        {' '}Non-directional strategies require single-LIVE per account (MULTI-ACCOUNT-01).
      </div>
    </div>
  );
}

// ── Gate definitions ────────────────────────────────────────────────────────

const GATE_DEFS = [
  {
    key: 'vpin',
    label: 'VPIN',
    description: 'Volume-synchronized probability of informed trading',
    hasThreshold: true,
    thresholdMin: 0,
    thresholdMax: 1,
    thresholdStep: 0.01,
    thresholdDefault: 0.45,
    thresholdLabel: 'Min VPIN',
    // Check: VPIN must be >= threshold to pass (indicates informed flow)
    // Actually VPIN gate FAILS when VPIN is below threshold (low informed flow = no edge)
    evaluate: (window, threshold) => {
      const vpin = window.vpin;
      if (vpin == null) return false;
      return vpin >= threshold;
    },
    getValue: (w) => w.vpin,
  },
  {
    key: 'source_agreement',
    label: 'Source Agreement',
    description: 'Number of price sources that agree on direction',
    hasThreshold: true,
    thresholdMin: 1,
    thresholdMax: 3,
    thresholdStep: 1,
    thresholdDefault: 2,
    thresholdLabel: 'Required sources',
    evaluate: (window, threshold) => {
      const sa = window.source_agreement;
      if (sa == null) return false;
      // source_agreement is like "CL+TI" or "CL+TI+BN" — count the segments
      if (typeof sa === 'string') {
        const count = sa.split('+').filter(Boolean).length;
        return count >= threshold;
      }
      if (typeof sa === 'number') return sa >= threshold;
      return false;
    },
    getValue: (w) => {
      const sa = w.source_agreement;
      if (!sa) return null;
      if (typeof sa === 'string') return sa.split('+').filter(Boolean).length;
      return sa;
    },
  },
  {
    key: 'delta_magnitude',
    label: 'Delta Magnitude',
    description: 'Minimum price delta required for a signal',
    hasThreshold: true,
    thresholdMin: 0,
    thresholdMax: 0.5,
    thresholdStep: 0.005,
    thresholdDefault: 0.01,
    thresholdLabel: 'Min |delta|%',
    evaluate: (window, threshold) => {
      const delta = window.delta_pct;
      if (delta == null) return false;
      return Math.abs(delta) >= threshold;
    },
    getValue: (w) => w.delta_pct != null ? Math.abs(w.delta_pct) : null,
  },
  {
    key: 'eval_offset_bounds',
    label: 'Eval Offset Bounds',
    description: 'Evaluation must happen within timing window',
    hasThreshold: false,
    thresholdDefault: null,
    evaluate: (window) => {
      // This gate checks the eval_offset is within acceptable bounds.
      // From skip_reason: if it contains "eval_offset" or "OFFSET", gate failed.
      const skip = (window.skip_reason || '').toLowerCase();
      if (skip.includes('offset') || skip.includes('eval_offset')) return false;
      return true;
    },
    getValue: () => null,
  },
  {
    key: 'dune_confidence',
    label: 'DUNE Confidence',
    description: 'Sequoia model probability threshold',
    hasThreshold: true,
    thresholdMin: 0.5,
    thresholdMax: 0.95,
    thresholdStep: 0.01,
    thresholdDefault: 0.55,
    thresholdLabel: 'Min P(direction)',
    evaluate: (window, threshold) => {
      const p = window.dune_probability_up;
      if (p == null) return false;
      // The relevant probability is the one in the signal direction
      const dirP = window.direction === 'UP' ? p : 1 - p;
      return dirP >= threshold;
    },
    getValue: (w) => {
      const p = w.dune_probability_up;
      if (p == null) return null;
      return w.direction === 'UP' ? p : 1 - p;
    },
  },
  {
    key: 'cg_confirmation',
    label: 'CG Confirmation',
    description: 'CoinGlass data confirms directional bias',
    hasThreshold: false,
    thresholdDefault: null,
    evaluate: (window) => {
      const skip = (window.skip_reason || '').toLowerCase();
      if (skip.includes('cg') || skip.includes('coinglass')) return false;
      return true;
    },
    getValue: () => null,
  },
  {
    key: 'spread_gate',
    label: 'Spread Gate',
    description: 'Market spread must be within acceptable range',
    hasThreshold: false,
    thresholdDefault: null,
    evaluate: (window) => {
      const skip = (window.skip_reason || '').toLowerCase();
      if (skip.includes('spread')) return false;
      return true;
    },
    getValue: () => null,
  },
  {
    key: 'dynamic_cap',
    label: 'Dynamic Cap',
    description: 'Position size dynamic ceiling/floor',
    hasThreshold: false,
    thresholdDefault: null,
    evaluate: (window) => {
      const skip = (window.skip_reason || '').toLowerCase();
      if (skip.includes('cap') || skip.includes('floor') || skip.includes('ceiling')) return false;
      return true;
    },
    getValue: () => null,
  },
];

// ── Styles ──────────────────────────────────────────────────────────────────

const S = {
  page: {
    background: T.bg,
    minHeight: '100vh',
    color: T.text,
    fontFamily: T.mono,
    padding: '16px 20px',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    marginBottom: 16,
  },
  title: {
    fontSize: 18,
    fontWeight: 700,
    color: T.white,
    letterSpacing: '0.02em',
  },
  badge: {
    fontSize: 9,
    padding: '2px 8px',
    borderRadius: 4,
    background: T.cyanDim,
    color: T.cyan,
    fontWeight: 600,
    letterSpacing: '0.08em',
  },
  tabs: {
    display: 'flex',
    gap: 0,
    marginBottom: 16,
    borderBottom: `1px solid ${T.cardBorder}`,
  },
  tab: (active) => ({
    padding: '8px 20px',
    fontSize: 11,
    fontWeight: 600,
    color: active ? T.cyan : T.textMuted,
    borderBottom: active ? `2px solid ${T.cyan}` : '2px solid transparent',
    cursor: 'pointer',
    background: 'none',
    border: 'none',
    fontFamily: T.mono,
    letterSpacing: '0.06em',
  }),
  card: {
    background: T.card,
    border: `1px solid ${T.cardBorder}`,
    borderRadius: 6,
    padding: '14px 16px',
    marginBottom: 12,
  },
  cardTitle: {
    fontSize: 9,
    color: T.purple,
    letterSpacing: '0.12em',
    fontWeight: 700,
    textTransform: 'uppercase',
    marginBottom: 10,
  },
  gateRow: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    padding: '6px 0',
    borderBottom: `1px solid ${T.border}`,
  },
  toggle: (on) => ({
    width: 36,
    height: 18,
    borderRadius: 9,
    background: on ? T.cyan : T.textDim,
    position: 'relative',
    cursor: 'pointer',
    transition: 'background 0.2s',
    flexShrink: 0,
  }),
  toggleKnob: (on) => ({
    width: 14,
    height: 14,
    borderRadius: 7,
    background: T.white,
    position: 'absolute',
    top: 2,
    left: on ? 20 : 2,
    transition: 'left 0.2s',
  }),
  slider: {
    flex: 1,
    maxWidth: 200,
    accentColor: T.cyan,
  },
  summaryGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
    gap: 10,
    marginBottom: 16,
  },
  summaryCard: {
    background: T.headerBg,
    border: `1px solid ${T.border}`,
    borderRadius: 6,
    padding: '10px 12px',
    textAlign: 'center',
  },
  summaryValue: {
    fontSize: 20,
    fontWeight: 700,
    color: T.white,
  },
  summaryLabel: {
    fontSize: 9,
    color: T.textMuted,
    letterSpacing: '0.08em',
    marginTop: 2,
  },
  barContainer: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    padding: '4px 0',
  },
  barLabel: {
    width: 120,
    fontSize: 10,
    color: T.textMuted,
    textAlign: 'right',
    flexShrink: 0,
  },
  barTrack: {
    flex: 1,
    height: 16,
    background: T.headerBg,
    borderRadius: 3,
    overflow: 'hidden',
    position: 'relative',
  },
  barFill: (width, color) => ({
    height: '100%',
    width: `${width}%`,
    background: color,
    borderRadius: 3,
    transition: 'width 0.3s',
  }),
  barValue: {
    fontSize: 10,
    color: T.textMuted,
    width: 50,
    textAlign: 'right',
    flexShrink: 0,
  },
  loadingBox: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 60,
    color: T.textMuted,
    fontSize: 12,
  },
  errorBox: {
    background: 'rgba(239, 68, 68, 0.1)',
    border: `1px solid ${T.red}`,
    borderRadius: 6,
    padding: '12px 16px',
    color: T.red,
    fontSize: 11,
    marginBottom: 12,
  },
};

// ── Replay logic ────────────────────────────────────────────────────────────

function deriveProductionGateResult(window) {
  // Derive which gates passed/failed based on skip_reason + trade_placed + v71
  // If trade was placed (v71_would_trade or trade_placed), all gates passed.
  if (window.v71_would_trade || window.trade_placed) {
    return { allPassed: true, failedGate: null };
  }
  const skip = (window.skip_reason || window.v71_skip_reason || '').toLowerCase();
  if (!skip) return { allPassed: true, failedGate: null };

  // Map skip reason keywords to gate keys
  const gateMap = [
    { keywords: ['vpin'], gate: 'vpin' },
    { keywords: ['source_agreement', 'agreement', 'srcagree'], gate: 'source_agreement' },
    { keywords: ['delta_magnitude', 'delta'], gate: 'delta_magnitude' },
    { keywords: ['offset', 'eval_offset'], gate: 'eval_offset_bounds' },
    { keywords: ['dune', 'confidence', 'timesfm', 'probability'], gate: 'dune_confidence' },
    { keywords: ['cg', 'coinglass', 'taker'], gate: 'cg_confirmation' },
    { keywords: ['spread'], gate: 'spread_gate' },
    { keywords: ['cap', 'floor', 'ceiling'], gate: 'dynamic_cap' },
  ];

  for (const { keywords, gate } of gateMap) {
    if (keywords.some(kw => skip.includes(kw))) {
      return { allPassed: false, failedGate: gate };
    }
  }

  return { allPassed: false, failedGate: 'unknown' };
}

function runReplay(windows, gateConfig) {
  const results = [];
  for (const w of windows) {
    if (!w.direction || !w.actual_direction) continue;

    // Check each enabled gate
    let allPass = true;
    let failedGate = null;
    for (const gateDef of GATE_DEFS) {
      const cfg = gateConfig[gateDef.key];
      if (!cfg || !cfg.enabled) continue;
      const passed = gateDef.evaluate(w, cfg.threshold);
      if (!passed) {
        allPass = false;
        failedGate = gateDef.key;
        break;
      }
    }

    const wouldTrade = allPass;
    const correct = w.direction === w.actual_direction;

    results.push({
      window: w,
      wouldTrade,
      failedGate,
      correct,
      pnl: wouldTrade
        ? (correct ? calcWinPnl(w) : calcLossPnl(w))
        : 0,
    });
  }
  return results;
}

function calcWinPnl(w) {
  // If gamma prices available, calculate actual PnL
  const up = w.gamma_up_price;
  const down = w.gamma_down_price;
  if (up && down && up > 0.01 && up < 0.99) {
    const entry = w.direction === 'UP' ? up : down;
    return +(((1 - entry) * 4).toFixed(4));
  }
  // Fallback: assume ~$0.50 entry → $0.50 profit on $4
  return 2.0;
}

function calcLossPnl(w) {
  const up = w.gamma_up_price;
  const down = w.gamma_down_price;
  if (up && down && up > 0.01 && up < 0.99) {
    const entry = w.direction === 'UP' ? up : down;
    return +((-entry * 4).toFixed(4));
  }
  return -2.0;
}

function computeStats(results) {
  const traded = results.filter(r => r.wouldTrade);
  const skipped = results.filter(r => !r.wouldTrade);
  const wins = traded.filter(r => r.correct);
  const losses = traded.filter(r => !r.correct);
  const cumPnl = traded.reduce((s, r) => s + r.pnl, 0);

  return {
    total: results.length,
    traded: traded.length,
    skipped: skipped.length,
    wins: wins.length,
    losses: losses.length,
    winRate: traded.length > 0 ? wins.length / traded.length : 0,
    cumPnl,
  };
}

function computeEquityCurve(results) {
  const curve = [];
  let cumPnl = 0;
  for (const r of results) {
    if (r.wouldTrade) {
      cumPnl += r.pnl;
    }
    curve.push(cumPnl);
  }
  return curve;
}

// ── Components ──────────────────────────────────────────────────────────────

function Toggle({ on, onChange }) {
  return (
    <div style={S.toggle(on)} onClick={() => onChange(!on)}>
      <div style={S.toggleKnob(on)} />
    </div>
  );
}

function GateControl({ gateDef, config, onChange }) {
  return (
    <div style={S.gateRow}>
      <Toggle on={config.enabled} onChange={(v) => onChange({ ...config, enabled: v })} />
      <div style={{ flex: '0 0 130px' }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: config.enabled ? T.white : T.textDim }}>
          {gateDef.label}
        </div>
        <div style={{ fontSize: 8, color: T.textDim }}>{gateDef.description}</div>
      </div>
      {gateDef.hasThreshold && config.enabled && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1 }}>
          <span style={{ fontSize: 9, color: T.textMuted, minWidth: 80 }}>
            {gateDef.thresholdLabel}:
          </span>
          <input
            type="range"
            min={gateDef.thresholdMin}
            max={gateDef.thresholdMax}
            step={gateDef.thresholdStep}
            value={config.threshold ?? gateDef.thresholdDefault}
            onChange={(e) => onChange({ ...config, threshold: parseFloat(e.target.value) })}
            style={S.slider}
          />
          <span style={{ fontSize: 11, color: T.cyan, fontWeight: 700, minWidth: 40 }}>
            {gateDef.thresholdStep >= 1
              ? (config.threshold ?? gateDef.thresholdDefault)
              : (config.threshold ?? gateDef.thresholdDefault).toFixed(
                  gateDef.thresholdStep < 0.01 ? 3 : 2
                )}
          </span>
        </div>
      )}
    </div>
  );
}

function SummaryCards({ stats, productionStats, ungatedStats }) {
  const cards = [
    { label: 'TRADES (YOUR CONFIG)', value: stats.traded, color: T.white },
    { label: 'SKIPPED', value: stats.skipped, color: T.textMuted },
    {
      label: 'W/L (YOUR CONFIG)',
      value: `${stats.wins}W/${stats.losses}L`,
      color: stats.winRate >= 0.55 ? T.green : stats.winRate >= 0.45 ? T.amber : T.red,
      sub: pct(stats.winRate),
    },
    {
      label: 'W/L (PRODUCTION)',
      value: `${productionStats.wins}W/${productionStats.losses}L`,
      color: productionStats.winRate >= 0.55 ? T.green : productionStats.winRate >= 0.45 ? T.amber : T.red,
      sub: pct(productionStats.winRate),
    },
    {
      label: 'W/L (UNGATED)',
      value: `${ungatedStats.wins}W/${ungatedStats.losses}L`,
      color: ungatedStats.winRate >= 0.55 ? T.green : ungatedStats.winRate >= 0.45 ? T.amber : T.red,
      sub: pct(ungatedStats.winRate),
    },
    {
      label: 'CUM P&L (YOUR)',
      value: `$${stats.cumPnl >= 0 ? '+' : ''}${stats.cumPnl.toFixed(2)}`,
      color: stats.cumPnl >= 0 ? T.green : T.red,
    },
  ];

  return (
    <div style={S.summaryGrid}>
      {cards.map((c, i) => (
        <div key={i} style={S.summaryCard}>
          <div style={{ ...S.summaryValue, color: c.color }}>{c.value}</div>
          {c.sub && <div style={{ fontSize: 11, color: c.color, fontWeight: 600 }}>{c.sub}</div>}
          <div style={S.summaryLabel}>{c.label}</div>
        </div>
      ))}
    </div>
  );
}

function EquityCurveChart({ yourCurve, prodCurve, ungatedCurve }) {
  if (!yourCurve.length) return null;

  const all = [...yourCurve, ...prodCurve, ...ungatedCurve];
  const minVal = Math.min(...all);
  const maxVal = Math.max(...all);
  const range = maxVal - minVal || 1;

  const W = 700;
  const H = 200;
  const padL = 50;
  const padR = 10;
  const padT = 10;
  const padB = 20;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  function toPath(curve) {
    if (!curve.length) return '';
    const step = plotW / Math.max(curve.length - 1, 1);
    return curve.map((v, i) => {
      const x = padL + i * step;
      const y = padT + plotH - ((v - minVal) / range) * plotH;
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
  }

  // Zero line
  const zeroY = padT + plotH - ((0 - minVal) / range) * plotH;

  // Y-axis labels
  const yLabels = [minVal, 0, maxVal].filter((v, i, arr) => arr.indexOf(v) === i);

  return (
    <div style={S.card}>
      <div style={S.cardTitle}>Equity Curve Comparison</div>
      <svg width={W} height={H} style={{ display: 'block', maxWidth: '100%' }}>
        {/* Zero line */}
        <line x1={padL} y1={zeroY} x2={W - padR} y2={zeroY}
          stroke={T.textDim} strokeWidth={0.5} strokeDasharray="4,3" />

        {/* Y-axis labels */}
        {yLabels.map((v, i) => {
          const y = padT + plotH - ((v - minVal) / range) * plotH;
          return (
            <text key={i} x={padL - 4} y={y + 3}
              fill={T.textDim} fontSize={8} textAnchor="end" fontFamily={T.mono}>
              ${v.toFixed(0)}
            </text>
          );
        })}

        {/* Ungated curve */}
        <path d={toPath(ungatedCurve)} fill="none" stroke={T.textDim} strokeWidth={1.2}
          strokeDasharray="3,3" opacity={0.6} />

        {/* Production curve */}
        <path d={toPath(prodCurve)} fill="none" stroke={T.amber} strokeWidth={1.5} opacity={0.8} />

        {/* Your config curve */}
        <path d={toPath(yourCurve)} fill="none" stroke={T.cyan} strokeWidth={2} />
      </svg>

      {/* Legend */}
      <div style={{ display: 'flex', gap: 16, marginTop: 6 }}>
        {[
          { color: T.cyan, label: 'Your Config', width: 2 },
          { color: T.amber, label: 'Production', width: 1.5 },
          { color: T.textDim, label: 'Ungated', width: 1.2, dash: true },
        ].map((l, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <svg width={20} height={8}>
              <line x1={0} y1={4} x2={20} y2={4}
                stroke={l.color} strokeWidth={l.width}
                strokeDasharray={l.dash ? '3,3' : 'none'} />
            </svg>
            <span style={{ fontSize: 9, color: T.textMuted }}>{l.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function GateKillChart({ results, gateConfig }) {
  // Count how many trades each gate blocked
  const gateCounts = {};
  const gateSaves = {};
  for (const gateDef of GATE_DEFS) {
    gateCounts[gateDef.key] = 0;
    gateSaves[gateDef.key] = 0;
  }

  // For each skipped window, count which gate blocked it
  for (const r of results) {
    if (!r.wouldTrade && r.failedGate && gateCounts[r.failedGate] != null) {
      gateCounts[r.failedGate]++;
      // Was the blocked trade going to be a loss?
      if (!r.correct) {
        gateSaves[r.failedGate]++;
      }
    }
  }

  const maxCount = Math.max(...Object.values(gateCounts), 1);

  return (
    <div style={S.card}>
      <div style={S.cardTitle}>Per-Gate Kill Count & Save Rate</div>
      <div style={{ fontSize: 8, color: T.textDim, marginBottom: 8 }}>
        Blocked = trades this gate prevented.
        Saved = blocked trades that would have lost (gate correctly protected you).
      </div>
      {GATE_DEFS.filter(g => gateConfig[g.key]?.enabled).map((gateDef) => {
        const kills = gateCounts[gateDef.key];
        const saves = gateSaves[gateDef.key];
        const killPct = (kills / maxCount) * 100;
        const savePct = kills > 0 ? (saves / kills) * 100 : 0;
        return (
          <div key={gateDef.key} style={S.barContainer}>
            <div style={S.barLabel}>{gateDef.label}</div>
            <div style={S.barTrack}>
              <div style={{
                height: '100%',
                display: 'flex',
                borderRadius: 3,
                overflow: 'hidden',
              }}>
                <div style={{
                  width: `${(saves / Math.max(maxCount, 1)) * 100}%`,
                  background: T.green,
                  height: '100%',
                  transition: 'width 0.3s',
                }} />
                <div style={{
                  width: `${((kills - saves) / Math.max(maxCount, 1)) * 100}%`,
                  background: T.red,
                  height: '100%',
                  opacity: 0.6,
                  transition: 'width 0.3s',
                }} />
              </div>
            </div>
            <div style={S.barValue}>
              <span style={{ color: T.green }}>{saves}</span>
              <span style={{ color: T.textDim }}>/</span>
              <span style={{ color: T.text }}>{kills}</span>
            </div>
          </div>
        );
      })}
      <div style={{ display: 'flex', gap: 16, marginTop: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <div style={{ width: 10, height: 10, background: T.green, borderRadius: 2 }} />
          <span style={{ fontSize: 9, color: T.textMuted }}>Saved (would have lost)</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <div style={{ width: 10, height: 10, background: T.red, borderRadius: 2, opacity: 0.6 }} />
          <span style={{ fontSize: 9, color: T.textMuted }}>Blocked (would have won)</span>
        </div>
      </div>
    </div>
  );
}

// ── Tab B: Gate Impact Analysis ─────────────────────────────────────────────

function GateImpactAnalysis({ windows }) {
  // For each gate: compute "what if I disabled ONLY this gate" vs production.
  // Production = all gates at default settings.
  const prodConfig = {};
  for (const g of GATE_DEFS) {
    prodConfig[g.key] = { enabled: true, threshold: g.thresholdDefault };
  }

  // Production stats (all gates on, default thresholds)
  const prodResults = runReplay(windows, prodConfig);
  const prodStats = computeStats(prodResults);

  const rows = useMemo(() => {
    return GATE_DEFS.map((gateDef) => {
      // Config with this one gate disabled
      const withoutConfig = { ...prodConfig };
      withoutConfig[gateDef.key] = { enabled: false, threshold: gateDef.thresholdDefault };

      const withoutResults = runReplay(windows, withoutConfig);
      const withoutStats = computeStats(withoutResults);

      const tradesGained = withoutStats.traded - prodStats.traded;
      const extraWins = withoutStats.wins - prodStats.wins;
      const extraLosses = withoutStats.losses - prodStats.losses;
      const netPnlImpact = withoutStats.cumPnl - prodStats.cumPnl;

      return {
        gate: gateDef.label,
        key: gateDef.key,
        prodWR: prodStats.winRate,
        prodWL: `${prodStats.wins}W/${prodStats.losses}L`,
        withoutWR: withoutStats.winRate,
        withoutWL: `${withoutStats.wins}W/${withoutStats.losses}L`,
        tradesGained,
        extraWins,
        extraLosses,
        netPnlImpact,
      };
    });
  }, [windows]);

  const tblCell = {
    padding: '6px 10px',
    fontSize: 10,
    borderBottom: `1px solid ${T.border}`,
    whiteSpace: 'nowrap',
  };
  const tblHead = {
    ...tblCell,
    color: T.textMuted,
    fontSize: 9,
    letterSpacing: '0.06em',
    fontWeight: 700,
    textTransform: 'uppercase',
    position: 'sticky',
    top: 0,
    background: T.headerBg,
  };

  return (
    <div>
      <div style={S.card}>
        <div style={S.cardTitle}>Per-Gate Counterfactual Analysis</div>
        <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 10 }}>
          For each gate: what would happen if you disabled ONLY that gate
          (keeping all others at production defaults)?
          Positive net impact = gate is hurting you (blocking more winners than losers).
          Negative net impact = gate is helping you (blocking more losers than winners).
        </div>

        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={tblHead}>Gate</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>With Gate (W/L)</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>Without Gate (W/L)</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>Trades Gained</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>Extra Wins</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>Extra Losses</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>Net P&L Impact</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const verdict = r.netPnlImpact > 1
                  ? { text: 'HURTING', color: T.red }
                  : r.netPnlImpact < -1
                    ? { text: 'HELPING', color: T.green }
                    : { text: 'NEUTRAL', color: T.textMuted };
                return (
                  <tr key={r.key}>
                    <td style={{ ...tblCell, color: T.white, fontWeight: 600 }}>{r.gate}</td>
                    <td style={{ ...tblCell, textAlign: 'center' }}>
                      {r.prodWL}
                      <span style={{ color: T.textDim, marginLeft: 4 }}>({pct(r.prodWR)})</span>
                    </td>
                    <td style={{ ...tblCell, textAlign: 'center' }}>
                      {r.withoutWL}
                      <span style={{ color: T.textDim, marginLeft: 4 }}>({pct(r.withoutWR)})</span>
                    </td>
                    <td style={{ ...tblCell, textAlign: 'center', color: r.tradesGained > 0 ? T.cyan : T.textMuted }}>
                      {r.tradesGained > 0 ? '+' : ''}{r.tradesGained}
                    </td>
                    <td style={{ ...tblCell, textAlign: 'center', color: T.green }}>
                      {r.extraWins > 0 ? '+' : ''}{r.extraWins}
                    </td>
                    <td style={{ ...tblCell, textAlign: 'center', color: T.red }}>
                      {r.extraLosses > 0 ? '+' : ''}{r.extraLosses}
                    </td>
                    <td style={{
                      ...tblCell,
                      textAlign: 'center',
                      color: r.netPnlImpact >= 0 ? T.red : T.green,
                      fontWeight: 700,
                    }}>
                      ${r.netPnlImpact >= 0 ? '+' : ''}{r.netPnlImpact.toFixed(2)}
                    </td>
                    <td style={{
                      ...tblCell,
                      textAlign: 'center',
                      color: verdict.color,
                      fontWeight: 700,
                      fontSize: 9,
                      letterSpacing: '0.06em',
                    }}>
                      {verdict.text}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Summary insight */}
      <div style={S.card}>
        <div style={S.cardTitle}>Insight</div>
        <div style={{ fontSize: 11, color: T.text, lineHeight: 1.6 }}>
          {(() => {
            const hurting = rows.filter(r => r.netPnlImpact > 1);
            const helping = rows.filter(r => r.netPnlImpact < -1);
            if (!hurting.length && !helping.length) {
              return 'All gates have minimal individual impact on P&L at production thresholds. The gate pipeline is well-calibrated.';
            }
            const parts = [];
            if (hurting.length) {
              parts.push(
                `${hurting.map(r => r.gate).join(', ')} ${hurting.length === 1 ? 'is' : 'are'} blocking profitable trades — consider relaxing ${hurting.length === 1 ? 'its' : 'their'} threshold or disabling.`
              );
            }
            if (helping.length) {
              parts.push(
                `${helping.map(r => r.gate).join(', ')} ${helping.length === 1 ? 'is' : 'are'} protecting you from losses — keep enabled.`
              );
            }
            return parts.join(' ');
          })()}
        </div>
      </div>
    </div>
  );
}

// ── Tab C: Live Shadow Comparison ──────────────────────────────────────────

function ShadowComparison({ api }) {
  const [decisions, setDecisions] = useState([]);
  const [comparison, setComparison] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [days, setDays] = useState(7);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        // strategy-comparison may not exist yet — use allSettled so decisions still load
        const [decRes, cmpRes] = await Promise.allSettled([
          api('GET', `/v58/strategy-decisions?limit=200`),
          api('GET', `/v58/strategy-comparison?days=${days}`),
        ]);
        if (cancelled) return;
        if (decRes.status === 'fulfilled') {
          setDecisions(decRes.value?.data?.decisions || []);
        }
        if (cmpRes.status === 'fulfilled') {
          setComparison(cmpRes.value?.data || { strategies: [] });
        } else {
          // Endpoint doesn't exist yet — use empty comparison, decisions still usable
          setComparison(null);
        }
      } catch (err) {
        if (!cancelled) setError(err.message || 'Failed to fetch strategy data');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [api, days]);

  if (loading) return <div style={S.loadingBox}>Loading strategy decisions...</div>;
  if (error) return <div style={S.errorBox}>{error}</div>;

  // comparison may be null if /v58/strategy-comparison endpoint doesn't exist yet
  const comparisonUnavailable = comparison === null;
  const strategies = comparison?.strategies || [];
  const v10 = strategies.find(s => s.strategy_id === 'v10_gate');
  const v4 = strategies.find(s => s.strategy_id === 'v4_fusion');
  const v4down = strategies.find(s => s.strategy_id === 'v4_down_only');

  // Group decisions by window_ts for side-by-side timeline
  const windowMap = {};
  for (const d of decisions) {
    const key = d.window_ts;
    if (!windowMap[key]) windowMap[key] = {};
    const sid = d.strategy_id || d.strategy_name || 'unknown';
    windowMap[key][sid] = d;
  }
  const windowKeys = Object.keys(windowMap).sort((a, b) => b - a);

  // Find disagreements
  const disagreements = windowKeys.filter(k => {
    const m = windowMap[k];
    const v10d = m['v10_gate'];
    const v4d = m['v4_fusion'];
    if (!v10d || !v4d) return false;
    return v10d.action !== v4d.action;
  });

  // Build equity curves from daily data
  const v10Daily = v10?.daily || [];
  const v4Daily = v4?.daily || [];
  const allDates = [...new Set([...v10Daily.map(d => d.date), ...v4Daily.map(d => d.date)])].sort();
  const v10Curve = [];
  const v4Curve = [];
  let v10Cum = 0, v4Cum = 0;
  for (const date of allDates) {
    const v10Day = v10Daily.find(d => d.date === date);
    const v4Day = v4Daily.find(d => d.date === date);
    v10Cum += v10Day?.pnl || 0;
    v4Cum += v4Day?.pnl || 0;
    v10Curve.push(v10Cum);
    v4Curve.push(v4Cum);
  }

  const tblCell = {
    padding: '5px 8px',
    fontSize: 10,
    borderBottom: `1px solid ${T.border}`,
    whiteSpace: 'nowrap',
  };
  const tblHead = {
    ...tblCell,
    color: T.textMuted,
    fontSize: 9,
    letterSpacing: '0.06em',
    fontWeight: 700,
    textTransform: 'uppercase',
    position: 'sticky',
    top: 0,
    background: T.headerBg,
  };

  const actionColor = (a) => {
    if (a === 'TRADE') return T.cyan;
    if (a === 'SKIP') return T.textMuted;
    if (a === 'ERROR') return T.red;
    return T.textDim;
  };

  return (
    <div>
      {/* Info banner when strategy-comparison endpoint is unavailable */}
      {comparisonUnavailable && (
        <div style={{
          padding: '10px 14px', borderRadius: 4, marginBottom: 12,
          background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.25)',
          fontSize: 10, color: T.amber, lineHeight: 1.5,
        }}>
          <strong>Gate Impact Analysis</strong> uses historical window data.
          Powered by <code style={{ color: T.cyan }}>/api/v58/strategy-decisions</code> — summary stats (W/L by strategy)
          will populate as the <code style={{ color: T.cyan }}>/api/v58/strategy-comparison</code> endpoint comes online.
          The side-by-side decision timeline below is available now.
        </div>
      )}

      {/* Days selector */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'center' }}>
        <span style={{ fontSize: 9, color: T.textMuted, letterSpacing: '0.06em' }}>LOOKBACK:</span>
        {[3, 7, 14, 30].map(d => (
          <button
            key={d}
            onClick={() => setDays(d)}
            style={{
              background: days === d ? T.cyan : T.headerBg,
              color: days === d ? '#000' : T.text,
              border: `1px solid ${days === d ? T.cyan : T.border}`,
              borderRadius: 4,
              padding: '4px 10px',
              fontSize: 10,
              fontFamily: T.mono,
              cursor: 'pointer',
              fontWeight: days === d ? 700 : 400,
            }}
          >
            {d}d
          </button>
        ))}
      </div>

      {/* Summary cards: all 3 strategies */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 16 }}>
        {[
          { strat: v4down, label: 'V4 DOWN-ONLY', currentMode: 'LIVE', borderColor: '#10b981', badge: 'PRIMARY' },
          { strat: v4, label: 'V4 FUSION', currentMode: 'GHOST', borderColor: T.cyan, badge: null },
          { strat: v10, label: 'V10 GATE', currentMode: 'GHOST', borderColor: T.purple, badge: null },
        ].map(({ strat, label, currentMode, borderColor, badge }) => {
          // historical mode stored in DB rows (may differ from current config)
          const histMode = strat?.mode?.toUpperCase() || currentMode;
          const modeColor = currentMode === 'LIVE' ? T.cyan : T.purple;
          const pnlLabel = currentMode === 'LIVE' ? 'ACTUAL P&L' : 'WOULD-BE P&L';
          return (
          <div key={label} style={{
            ...S.card,
            borderColor,
            borderWidth: 2,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
              <span style={{ fontSize: 12, fontWeight: 700, color: borderColor }}>{label}</span>
              {badge && (
                <span style={{
                  fontSize: 7, padding: '1px 5px', borderRadius: 2,
                  background: `${borderColor}25`, color: borderColor, fontWeight: 700,
                }}>{badge}</span>
              )}
              {/* Current mode badge */}
              <span style={{
                fontSize: 8, padding: '1px 6px', borderRadius: 3,
                background: currentMode === 'LIVE' ? 'rgba(6,182,212,0.15)' : 'rgba(168,85,247,0.15)',
                color: modeColor, fontWeight: 700, letterSpacing: '0.08em',
              }}>
                {currentMode}
              </span>
              {/* Note if historical data was recorded under a different mode */}
              {histMode && histMode !== currentMode && (
                <span style={{
                  fontSize: 7, padding: '1px 5px', borderRadius: 3,
                  background: 'rgba(245,158,11,0.12)', color: T.amber,
                  fontWeight: 600, letterSpacing: '0.05em',
                }} title={`Historical rows were written when strategy was ${histMode}`}>
                  hist: {histMode}
                </span>
              )}
            </div>
            {strat ? (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
                <div style={S.summaryCard}>
                  <div style={{ ...S.summaryValue, fontSize: 18 }}>{strat.trades}</div>
                  <div style={S.summaryLabel}>TRADES</div>
                </div>
                <div style={S.summaryCard}>
                  <div style={{
                    ...S.summaryValue,
                    fontSize: 18,
                    color: (strat.accuracy || 0) >= 55 ? T.green : (strat.accuracy || 0) >= 45 ? T.amber : T.red,
                  }}>
                    {strat.wins}W/{strat.losses}L
                  </div>
                  <div style={S.summaryLabel}>
                    {strat.accuracy != null ? `${strat.accuracy}%` : '--'}
                  </div>
                </div>
                <div style={S.summaryCard}>
                  <div style={{
                    ...S.summaryValue,
                    fontSize: 18,
                    color: strat.cum_pnl >= 0 ? T.green : T.red,
                  }}>
                    ${strat.cum_pnl >= 0 ? '+' : ''}{strat.cum_pnl.toFixed(2)}
                  </div>
                  <div style={S.summaryLabel}>{pnlLabel}</div>
                </div>
              </div>
            ) : (
              <div style={{ fontSize: 10, color: T.textDim, padding: 12, textAlign: 'center' }}>
                No data for {label}
              </div>
            )}
          </div>
          );
        })}
      </div>

      {/* Equity curve comparison */}
      {allDates.length > 1 && (
        <div style={S.card}>
          <div style={S.cardTitle}>Equity Curve: V4 LIVE (actual) vs V10 GHOST (would-be)</div>
          {(() => {
            const all = [...v10Curve, ...v4Curve];
            const minVal = Math.min(...all, 0);
            const maxVal = Math.max(...all, 0);
            const range = maxVal - minVal || 1;
            const W = 700, H = 180;
            const padL = 50, padR = 10, padT = 10, padB = 20;
            const plotW = W - padL - padR;
            const plotH = H - padT - padB;
            const toPath = (curve, color) => {
              if (!curve.length) return null;
              const step = plotW / Math.max(curve.length - 1, 1);
              const d = curve.map((v, i) => {
                const x = padL + i * step;
                const y = padT + plotH - ((v - minVal) / range) * plotH;
                return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
              }).join(' ');
              return <path d={d} fill="none" stroke={color} strokeWidth={2} />;
            };
            const zeroY = padT + plotH - ((0 - minVal) / range) * plotH;
            return (
              <svg width={W} height={H} style={{ display: 'block', maxWidth: '100%' }}>
                <line x1={padL} y1={zeroY} x2={W - padR} y2={zeroY}
                  stroke={T.textDim} strokeWidth={0.5} strokeDasharray="4,3" />
                {[minVal, 0, maxVal].filter((v, i, a) => a.indexOf(v) === i).map((v, i) => {
                  const y = padT + plotH - ((v - minVal) / range) * plotH;
                  return (
                    <text key={i} x={padL - 4} y={y + 3}
                      fill={T.textDim} fontSize={8} textAnchor="end" fontFamily={T.mono}>
                      ${v.toFixed(0)}
                    </text>
                  );
                })}
                {toPath(v4Curve, T.cyan)}
                {toPath(v10Curve, T.purple)}
              </svg>
            );
          })()}
          <div style={{ display: 'flex', gap: 16, marginTop: 6 }}>
            {[
              { color: T.cyan, label: 'V4 Fusion (LIVE)' },
              { color: T.purple, label: 'V10 Gate (GHOST)' },
            ].map((l, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                <svg width={20} height={8}>
                  <line x1={0} y1={4} x2={20} y2={4} stroke={l.color} strokeWidth={2} />
                </svg>
                <span style={{ fontSize: 9, color: T.textMuted }}>{l.label}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Disagreement highlight */}
      {disagreements.length > 0 && (
        <div style={{
          ...S.card,
          borderColor: T.amber,
          borderWidth: 2,
        }}>
          <div style={{ ...S.cardTitle, color: T.amber }}>
            Disagreements ({disagreements.length} windows)
          </div>
          <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 8 }}>
            Windows where V10 and V4 made different decisions.
          </div>
          <div style={{ maxHeight: 300, overflowY: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={tblHead}>Window</th>
                  <th style={{ ...tblHead, textAlign: 'center' }}>V10 Action</th>
                  <th style={{ ...tblHead, textAlign: 'center' }}>V10 Dir</th>
                  <th style={{ ...tblHead, textAlign: 'center' }}>V4 Action</th>
                  <th style={{ ...tblHead, textAlign: 'center' }}>V4 Dir</th>
                  <th style={tblHead}>V10 Reason</th>
                  <th style={tblHead}>V4 Reason</th>
                </tr>
              </thead>
              <tbody>
                {disagreements.slice(0, 50).map(k => {
                  const v10d = windowMap[k]['v10_gate'] || {};
                  const v4d = windowMap[k]['v4_fusion'] || {};
                  const ts = new Date(parseInt(k) * 1000);
                  return (
                    <tr key={k} onClick={() => setAnalysisWindow(parseInt(k))}
                      style={{ cursor: 'pointer' }} title="Click to analyze window">
                      <td style={{ ...tblCell, color: T.text }}>
                        {ts.toLocaleDateString()} {ts.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                      </td>
                      <td style={{ ...tblCell, textAlign: 'center', color: actionColor(v10d.action), fontWeight: 600 }}>
                        {v10d.action || '--'}
                      </td>
                      <td style={{ ...tblCell, textAlign: 'center', color: v10d.direction === 'UP' ? T.green : T.red }}>
                        {v10d.direction || '--'}
                      </td>
                      <td style={{ ...tblCell, textAlign: 'center', color: actionColor(v4d.action), fontWeight: 600 }}>
                        {v4d.action || '--'}
                      </td>
                      <td style={{ ...tblCell, textAlign: 'center', color: v4d.direction === 'UP' ? T.green : T.red }}>
                        {v4d.direction || '--'}
                      </td>
                      <td style={{ ...tblCell, color: T.textDim, fontSize: 9, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {v10d.action === 'SKIP' ? v10d.skip_reason : v10d.entry_reason || '--'}
                      </td>
                      <td style={{ ...tblCell, color: T.textDim, fontSize: 9, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {v4d.action === 'SKIP' ? v4d.skip_reason : v4d.entry_reason || '--'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Strategy Notes */}
      <div style={{ ...S.card, borderColor: T.purple, borderWidth: 2, marginBottom: 16 }}>
        <div style={{ ...S.cardTitle, color: T.purple }}>Strategy Notes — Window Analysis Findings</div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          {/* Magic window */}
          <div>
            <div style={{ fontSize: 9, color: T.cyan, letterSpacing: '0.08em', fontWeight: 700, textTransform: 'uppercase', marginBottom: 6 }}>
              Magic Window
            </div>
            <div style={{ fontSize: 11, color: T.white, fontWeight: 700, marginBottom: 4 }}>
              T-120 to T-150
            </div>
            <div style={{ fontSize: 10, color: T.text, lineHeight: 1.6 }}>
              Highest accuracy zone. Price signal has formed but market has not yet corrected.
              Both Sequoia confidence and source agreement peak here. Optimal entry timing.
            </div>
          </div>

          {/* T-90 cliff */}
          <div>
            <div style={{ fontSize: 9, color: T.amber, letterSpacing: '0.08em', fontWeight: 700, textTransform: 'uppercase', marginBottom: 6 }}>
              T-90 Cliff
            </div>
            <div style={{ fontSize: 11, color: T.white, fontWeight: 700, marginBottom: 4 }}>
              Accuracy drops sharply inside T-90
            </div>
            <div style={{ fontSize: 10, color: T.text, lineHeight: 1.6 }}>
              Inside T-90s, informed traders have already repositioned. CLOB prices converge toward
              resolution, eliminating the edge. Sequoia confidence frequently disagrees with CLOB
              implied probability — this divergence is the CLOB divergence gate.
            </div>
          </div>

          {/* Confidence threshold */}
          <div>
            <div style={{ fontSize: 9, color: T.green, letterSpacing: '0.08em', fontWeight: 700, textTransform: 'uppercase', marginBottom: 6 }}>
              Confidence Threshold Rationale
            </div>
            <div style={{ fontSize: 11, color: T.white, fontWeight: 700, marginBottom: 4 }}>
              confidence_distance ≥ 0.12 (not raw P ≥ 0.62)
            </div>
            <div style={{ fontSize: 10, color: T.text, lineHeight: 1.6 }}>
              Using distance from 0.5 rather than absolute probability corrects for the 84% DOWN
              bias in the training set. At conf_dist 0.12, backtested accuracy is ~68% vs 54% for
              all signals. Below 0.08, win rate collapses to near 50%.
            </div>
          </div>

          {/* V4 vs V10 rationale */}
          <div>
            <div style={{ fontSize: 9, color: T.textMuted, letterSpacing: '0.08em', fontWeight: 700, textTransform: 'uppercase', marginBottom: 6 }}>
              V4 LIVE / V10 GHOST Rationale
            </div>
            <div style={{ fontSize: 11, color: T.white, fontWeight: 700, marginBottom: 4 }}>
              Flipped 2026-04-12 after window analysis
            </div>
            <div style={{ fontSize: 10, color: T.text, lineHeight: 1.6 }}>
              V10 uses tighter T-90 to T-120 window — good on backtests but insufficient live sample.
              V4 runs T-30 to T-180 with confidence_distance gate as primary filter, giving more
              trade opportunities while preserving edge. V10 runs ghost to build comparison data.
            </div>
          </div>
        </div>
      </div>

      {/* Decision timeline */}
      <div style={S.card}>
        <div style={S.cardTitle}>Decision Timeline (latest {Math.min(windowKeys.length, 100)} windows)</div>
        <div style={{ maxHeight: 500, overflowY: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={tblHead}>Window</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>Offset</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>V10</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>V10 Dir</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>V4</th>
                <th style={{ ...tblHead, textAlign: 'center' }}>V4 Dir</th>
                <th style={tblHead}>Reason</th>
              </tr>
            </thead>
            <tbody>
              {windowKeys.slice(0, 100).map(k => {
                const v10d = windowMap[k]['v10_gate'] || {};
                const v4d = windowMap[k]['v4_fusion'] || {};
                const ts = new Date(parseInt(k) * 1000);
                const isDisagree = v10d.action && v4d.action && v10d.action !== v4d.action;
                return (
                  <tr key={k} style={isDisagree ? { background: 'rgba(245,158,11,0.08)' } : undefined}>
                    <td style={{ ...tblCell, color: T.text }}>
                      {ts.toLocaleDateString()} {ts.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                    </td>
                    <td style={{ ...tblCell, textAlign: 'center', color: T.textMuted }}>
                      {v10d.eval_offset ?? v4d.eval_offset ?? '--'}s
                    </td>
                    <td style={{ ...tblCell, textAlign: 'center', color: actionColor(v10d.action), fontWeight: 600 }}>
                      {v10d.action || '--'}
                    </td>
                    <td style={{ ...tblCell, textAlign: 'center', color: v10d.direction === 'UP' ? T.green : v10d.direction === 'DOWN' ? T.red : T.textDim }}>
                      {v10d.direction || '--'}
                    </td>
                    <td style={{ ...tblCell, textAlign: 'center', color: actionColor(v4d.action), fontWeight: 600 }}>
                      {v4d.action || '--'}
                    </td>
                    <td style={{ ...tblCell, textAlign: 'center', color: v4d.direction === 'UP' ? T.green : v4d.direction === 'DOWN' ? T.red : T.textDim }}>
                      {v4d.direction || '--'}
                    </td>
                    <td style={{ ...tblCell, color: T.textDim, fontSize: 9, maxWidth: 250, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {v10d.skip_reason || v4d.skip_reason || v10d.entry_reason || '--'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ── Main component ──────────────────────────────────────────────────────────

const LIMIT_OPTIONS = [
  { value: 100, label: 'Last 100 windows' },
  { value: 500, label: 'Last 500 windows' },
  { value: 1000, label: 'Last 1000 windows' },
];

export default function StrategyLab() {
  const api = useApi();
  const [activeTab, setActiveTab] = useState('replay');
  const [windows, setWindows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [limit, setLimit] = useState(500);

  // Window analysis modal
  const [analysisWindow, setAnalysisWindow] = useState(null);

  // Gate config: { [key]: { enabled: boolean, threshold: number } }
  const [gateConfig, setGateConfig] = useState(() => {
    const cfg = {};
    for (const g of GATE_DEFS) {
      cfg[g.key] = { enabled: true, threshold: g.thresholdDefault };
    }
    return cfg;
  });

  // Fetch data
  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api('GET', `/v58/outcomes?limit=${limit}`);
      const data = res.data?.outcomes || res.data || [];
      // Reverse so oldest first for equity curve
      setWindows(Array.isArray(data) ? [...data].reverse() : []);
    } catch (err) {
      setError(err.message || 'Failed to fetch outcomes data');
      setWindows([]);
    } finally {
      setLoading(false);
    }
  }, [api, limit]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Filter to windows with actual outcomes
  const resolvedWindows = useMemo(() =>
    windows.filter(w => w.direction && w.actual_direction),
    [windows]
  );

  // Replay with user's config
  const userResults = useMemo(() => runReplay(resolvedWindows, gateConfig), [resolvedWindows, gateConfig]);
  const userStats = useMemo(() => computeStats(userResults), [userResults]);

  // Production replay (all gates on, default thresholds)
  const prodConfig = useMemo(() => {
    const cfg = {};
    for (const g of GATE_DEFS) {
      cfg[g.key] = { enabled: true, threshold: g.thresholdDefault };
    }
    return cfg;
  }, []);
  const prodResults = useMemo(() => runReplay(resolvedWindows, prodConfig), [resolvedWindows, prodConfig]);
  const prodStats = useMemo(() => computeStats(prodResults), [prodResults]);

  // Ungated replay (all gates off)
  const ungatedConfig = useMemo(() => {
    const cfg = {};
    for (const g of GATE_DEFS) {
      cfg[g.key] = { enabled: false, threshold: g.thresholdDefault };
    }
    return cfg;
  }, []);
  const ungatedResults = useMemo(() => runReplay(resolvedWindows, ungatedConfig), [resolvedWindows, ungatedConfig]);
  const ungatedStats = useMemo(() => computeStats(ungatedResults), [ungatedResults]);

  // Equity curves
  const yourCurve = useMemo(() => computeEquityCurve(userResults), [userResults]);
  const prodCurve = useMemo(() => computeEquityCurve(prodResults), [prodResults]);
  const ungatedCurve = useMemo(() => computeEquityCurve(ungatedResults), [ungatedResults]);

  const updateGate = useCallback((key, cfg) => {
    setGateConfig(prev => ({ ...prev, [key]: cfg }));
  }, []);

  const resetToDefaults = useCallback(() => {
    const cfg = {};
    for (const g of GATE_DEFS) {
      cfg[g.key] = { enabled: true, threshold: g.thresholdDefault };
    }
    setGateConfig(cfg);
  }, []);

  const disableAllGates = useCallback(() => {
    const cfg = {};
    for (const g of GATE_DEFS) {
      cfg[g.key] = { enabled: false, threshold: g.thresholdDefault };
    }
    setGateConfig(cfg);
  }, []);

  return (
    <div style={S.page}>
      {/* Header */}
      <div style={S.header}>
        <div style={S.title}>Strategy Lab</div>
        <span style={S.badge}>CLIENT-SIDE SIMULATOR</span>
        {!loading && (
          <span style={{ fontSize: 9, color: T.textMuted, marginLeft: 'auto' }}>
            {resolvedWindows.length} resolved windows loaded
          </span>
        )}
      </div>

      {/* Strategy Config Panel */}
      <StrategyConfigPanel api={api} />

      {/* Tabs */}
      <div style={S.tabs}>
        <button style={S.tab(activeTab === 'replay')} onClick={() => setActiveTab('replay')}>
          Historical Replay
        </button>
        <button style={S.tab(activeTab === 'impact')} onClick={() => setActiveTab('impact')}>
          Gate Impact Analysis
        </button>
        <button style={S.tab(activeTab === 'shadow')} onClick={() => setActiveTab('shadow')}>
          Live Shadow Comparison
        </button>
      </div>

      {/* Error */}
      {error && <div style={S.errorBox}>{error}</div>}

      {/* Loading */}
      {loading && <div style={S.loadingBox}>Loading outcomes data...</div>}

      {/* Tab A: Historical Replay */}
      {!loading && activeTab === 'replay' && (
        <div>
          {/* Controls */}
          <div style={S.card}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <div style={S.cardTitle}>Gate Configuration</div>
              <div style={{ display: 'flex', gap: 8 }}>
                {/* Time range selector */}
                <select
                  value={limit}
                  onChange={(e) => setLimit(parseInt(e.target.value))}
                  style={{
                    background: T.headerBg,
                    color: T.text,
                    border: `1px solid ${T.border}`,
                    borderRadius: 4,
                    padding: '4px 8px',
                    fontSize: 10,
                    fontFamily: T.mono,
                    cursor: 'pointer',
                  }}
                >
                  {LIMIT_OPTIONS.map(o => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
                <button
                  onClick={resetToDefaults}
                  style={{
                    background: T.headerBg,
                    color: T.amber,
                    border: `1px solid ${T.border}`,
                    borderRadius: 4,
                    padding: '4px 10px',
                    fontSize: 9,
                    fontFamily: T.mono,
                    cursor: 'pointer',
                    fontWeight: 600,
                  }}
                >
                  RESET DEFAULTS
                </button>
                <button
                  onClick={disableAllGates}
                  style={{
                    background: T.headerBg,
                    color: T.textMuted,
                    border: `1px solid ${T.border}`,
                    borderRadius: 4,
                    padding: '4px 10px',
                    fontSize: 9,
                    fontFamily: T.mono,
                    cursor: 'pointer',
                    fontWeight: 600,
                  }}
                >
                  DISABLE ALL
                </button>
              </div>
            </div>

            {GATE_DEFS.map((g) => (
              <GateControl
                key={g.key}
                gateDef={g}
                config={gateConfig[g.key]}
                onChange={(cfg) => updateGate(g.key, cfg)}
              />
            ))}
          </div>

          {/* Summary */}
          <SummaryCards
            stats={userStats}
            productionStats={prodStats}
            ungatedStats={ungatedStats}
          />

          {/* Equity curve */}
          <EquityCurveChart
            yourCurve={yourCurve}
            prodCurve={prodCurve}
            ungatedCurve={ungatedCurve}
          />

          {/* Per-gate kill chart */}
          <GateKillChart results={userResults} gateConfig={gateConfig} />
        </div>
      )}

      {/* Tab B: Gate Impact Analysis */}
      {!loading && activeTab === 'impact' && (
        <GateImpactAnalysis windows={resolvedWindows} />
      )}

      {/* Tab C: Live Shadow Comparison */}
      {activeTab === 'shadow' && (
        <ShadowComparison api={api} />
      )}

      {/* Window Analysis Modal */}
      <WindowAnalysisModal
        windowTs={analysisWindow}
        onClose={() => setAnalysisWindow(null)}
      />
    </div>
  );
}
