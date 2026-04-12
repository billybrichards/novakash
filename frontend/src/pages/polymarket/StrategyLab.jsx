import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, fmt, pct } from './components/theme.js';

/**
 * Strategy Lab — Historical replay and gate impact analysis.
 *
 * Tab A: Historical Replay — toggle gates + adjust thresholds, see what-if W/L.
 * Tab B: Gate Impact Analysis — per-gate counterfactual table.
 *
 * Data source: /api/v58/outcomes?limit=N
 * All replay logic runs client-side in JavaScript.
 */

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

      {/* Tabs */}
      <div style={S.tabs}>
        <button style={S.tab(activeTab === 'replay')} onClick={() => setActiveTab('replay')}>
          Historical Replay
        </button>
        <button style={S.tab(activeTab === 'impact')} onClick={() => setActiveTab('impact')}>
          Gate Impact Analysis
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
    </div>
  );
}
