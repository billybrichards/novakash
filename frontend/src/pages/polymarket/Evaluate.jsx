import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, GATE_NAMES, fmt, utcHHMM, pct } from './components/theme.js';
import { getStrategyMeta } from '../../constants/strategies.js';

/**
 * Polymarket Evaluate — "How Am I Doing?"
 *
 * Performance analysis dashboard with 4 sections:
 *   A. Strategy Summary Cards (from /v58/strategy-comparison)
 *   B. Strategy Comparison Table — per-window, multi-strategy columns
 *   C. Accuracy by Signal Component
 *   D. P&L Charts (inline SVG — no chart library)
 *
 * Polls endpoints every 30s.
 */

// ─── Gate pipeline names (V10.6 order) ──────────────────────────────────────
const GATE_PIPELINE = [
  { key: 'eval_offset_bounds', label: 'EvalOffset' },
  { key: 'source_agreement', label: 'SrcAgree' },
  { key: 'delta_magnitude', label: 'Delta' },
  { key: 'taker_flow', label: 'Taker' },
  { key: 'cg_confirmation', label: 'CGConfirm' },
  { key: 'dune_confidence', label: 'DUNE' },
  { key: 'spread_gate', label: 'Spread' },
  { key: 'dynamic_cap', label: 'DynCap' },
];

// ─── Date range filters ─────────────────────────────────────────────────────
const DATE_RANGES = [
  { key: '24h', label: '24h', hours: 24 },
  { key: '7d', label: '7d', hours: 168 },
  { key: '30d', label: '30d', hours: 720 },
  { key: 'all', label: 'All', hours: Infinity },
];

// ─── Strategy display metadata — sourced from shared constants ───────────────
function strategyMeta(key, index) {
  const meta = getStrategyMeta(key, index);
  return { label: meta.label, color: meta.color };
}

// ─── Inject keyframes once ──────────────────────────────────────────────────
if (typeof document !== 'undefined' && !document.getElementById('pm-eval-styles')) {
  const style = document.createElement('style');
  style.id = 'pm-eval-styles';
  style.textContent = `
    @keyframes eval-fade-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
  `;
  document.head.appendChild(style);
}

// ─── Shared inline style fragments ──────────────────────────────────────────
const S = {
  card: {
    background: T.card, border: `1px solid ${T.cardBorder}`,
    borderRadius: 4, padding: '12px 16px',
  },
  sectionTitle: {
    fontSize: 11, fontWeight: 600, letterSpacing: '0.08em',
    color: T.cyan, textTransform: 'uppercase', marginBottom: 10,
    fontFamily: T.mono,
  },
  pill: (active) => ({
    padding: '3px 10px', borderRadius: 3, fontSize: 10,
    fontFamily: T.mono, cursor: 'pointer', border: 'none',
    background: active ? T.cyan : 'rgba(51,65,85,0.5)',
    color: active ? '#000' : T.textMuted,
    fontWeight: active ? 700 : 400,
    transition: 'all 0.15s',
  }),
  pillColored: (active, color) => ({
    padding: '3px 10px', borderRadius: 3, fontSize: 10,
    fontFamily: T.mono, cursor: 'pointer',
    border: `1px solid ${active ? color : 'transparent'}`,
    background: active ? `${color}22` : 'rgba(51,65,85,0.3)',
    color: active ? color : T.textMuted,
    fontWeight: active ? 700 : 400,
    transition: 'all 0.15s',
  }),
  th: {
    padding: '6px 8px', textAlign: 'left', fontSize: 9,
    fontWeight: 600, letterSpacing: '0.06em', color: T.textMuted,
    borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap',
    fontFamily: T.mono, textTransform: 'uppercase',
  },
  td: {
    padding: '5px 8px', fontSize: 10, fontFamily: T.mono,
    borderBottom: `1px solid rgba(51,65,85,0.3)`, whiteSpace: 'nowrap',
    color: T.text,
  },
};


// ─── Helpers ────────────────────────────────────────────────────────────────

function fmtUsd(v) {
  if (v == null || isNaN(v)) return '\u2014';
  const n = Number(v);
  const sign = n >= 0 ? '+' : '';
  return `${sign}$${n.toFixed(2)}`;
}

function fmtPnl(v) {
  if (v == null || isNaN(v)) return '\u2014';
  const n = Number(v);
  const sign = n >= 0 ? '+' : '';
  return `${sign}${n.toFixed(4)}`;
}

function pnlColor(v) {
  if (v == null || isNaN(v)) return T.textDim;
  return Number(v) >= 0 ? T.green : T.red;
}

function dirColor(dir) {
  if (dir === 'UP') return T.green;
  if (dir === 'DOWN') return T.red;
  return T.textDim;
}

function outcomeChip(correct) {
  if (correct === true) return { label: 'WIN', color: T.green, bg: 'rgba(16,185,129,0.12)' };
  if (correct === false) return { label: 'LOSS', color: T.red, bg: 'rgba(239,68,68,0.12)' };
  return { label: '\u2014', color: T.textDim, bg: 'transparent' };
}

function windowTime(ts) {
  if (!ts) return '\u2014';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mi = String(d.getUTCMinutes()).padStart(2, '0');
  return `${mm}-${dd} ${hh}:${mi}Z`;
}

function parseWindowTs(ts) {
  if (!ts) return 0;
  return new Date(typeof ts === 'number' ? ts * 1000 : ts).getTime();
}

/** Bucket a p_up value into confidence ranges */
function confidenceBucket(pUp) {
  if (pUp == null || isNaN(pUp)) return null;
  const v = Number(pUp);
  // Normalise: use distance from 0.5 as confidence
  const conf = Math.abs(v - 0.5) + 0.5;
  if (conf >= 0.8) return '0.8+';
  if (conf >= 0.7) return '0.7-0.8';
  if (conf >= 0.6) return '0.6-0.7';
  return '0.5-0.6';
}

/** Bucket VPIN value */
function vpinBucket(vpin) {
  if (vpin == null || isNaN(vpin)) return null;
  const v = Number(vpin);
  if (v >= 0.7) return '0.7+';
  if (v >= 0.55) return '0.55-0.7';
  if (v >= 0.4) return '0.4-0.55';
  return '<0.4';
}


// ═══════════════════════════════════════════════════════════════════════════
//  SECTION A: Strategy Summary Cards
// ═══════════════════════════════════════════════════════════════════════════

function SummaryCards({ accuracy, stats }) {
  const cards = [
    {
      label: 'Cumulative P&L',
      value: fmtUsd(accuracy?.cumulative_pnl),
      color: pnlColor(accuracy?.cumulative_pnl),
      sub: `Ungated: ${fmtUsd(accuracy?.ungated_pnl)}`,
    },
    {
      label: 'Gated Win Rate',
      value: accuracy?.v58_accuracy != null ? `${accuracy.v58_accuracy}%` : '\u2014',
      color: (accuracy?.v58_accuracy || 0) >= 55 ? T.green : T.amber,
      sub: `${accuracy?.v58_trades_count || 0} trades`,
    },
    {
      label: 'Ungated Accuracy',
      value: accuracy?.ungated_accuracy != null ? `${accuracy.ungated_accuracy}%` : '\u2014',
      color: (accuracy?.ungated_accuracy || 0) >= 55 ? T.green : T.amber,
      sub: `${accuracy?.ungated_wins || 0}W / ${accuracy?.ungated_losses || 0}L`,
    },
    {
      label: 'Gate Value',
      value: fmtUsd(accuracy?.gate_value),
      color: pnlColor(accuracy?.gate_value),
      sub: (accuracy?.gate_value || 0) > 0 ? 'Gates helped' : (accuracy?.gate_value || 0) < 0 ? 'Gates cost' : '\u2014',
    },
    {
      label: 'Current Streak',
      value: accuracy?.current_streak != null ? `${accuracy.current_streak}W` : '\u2014',
      color: (accuracy?.current_streak || 0) >= 3 ? T.green : T.text,
      sub: 'consecutive wins',
    },
  ];

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8 }}>
      {cards.map((c, i) => (
        <div key={i} style={{
          ...S.card,
          animation: 'eval-fade-in 0.3s ease-out both',
          animationDelay: `${i * 50}ms`,
        }}>
          <div style={{ fontSize: 9, color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em', marginBottom: 6, textTransform: 'uppercase' }}>
            {c.label}
          </div>
          <div style={{ fontSize: 22, fontWeight: 700, fontFamily: T.mono, color: c.color, lineHeight: 1 }}>
            {c.value}
          </div>
          <div style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, marginTop: 4 }}>
            {c.sub}
          </div>
        </div>
      ))}
    </div>
  );
}


// ═══════════════════════════════════════════════════════════════════════════
//  SECTION B: Strategy Comparison Table
// ═══════════════════════════════════════════════════════════════════════════

function StrategySelector({ knownStrategies, selectedStrategies, setSelectedStrategies, comparisonStats }) {
  const statsMap = useMemo(() => {
    const m = {};
    if (comparisonStats) for (const s of comparisonStats) m[s.strategy_id || s.strategy || s.name] = s;
    return m;
  }, [comparisonStats]);

  const toggle = (key) => {
    setSelectedStrategies(prev => {
      if (prev.includes(key)) return prev.length === 1 ? prev : prev.filter(k => k !== key);
      return prev.length >= 3 ? [...prev.slice(1), key] : [...prev, key];
    });
  };

  return (
    <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 10, flexWrap: 'wrap' }}>
      <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>STRATEGIES:</span>
      {knownStrategies.map((key, i) => {
        const meta = strategyMeta(key, i);
        const active = selectedStrategies.includes(key);
        const wr = statsMap[key]?.accuracy;
        const label = wr != null ? `${meta.label} ${Number(wr).toFixed(0)}%` : meta.label;
        return (
          <button key={key} style={S.pillColored(active, meta.color)} onClick={() => toggle(key)}
            title={active ? 'Hide' : selectedStrategies.length >= 3 ? 'Replaces oldest' : 'Show'}>
            {label}
          </button>
        );
      })}
      <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>(max 3 columns)</span>
    </div>
  );
}

function StratOutcomeChip({ outcome }) {
  if (outcome === 'WIN') return <span style={{ padding: '1px 5px', borderRadius: 2, fontSize: 8, fontFamily: T.mono, background: 'rgba(16,185,129,0.15)', color: T.green, fontWeight: 700 }}>WIN</span>;
  if (outcome === 'LOSS') return <span style={{ padding: '1px 5px', borderRadius: 2, fontSize: 8, fontFamily: T.mono, background: 'rgba(239,68,68,0.12)', color: T.red, fontWeight: 700 }}>LOSS</span>;
  if (outcome === 'SKIP') return <span style={{ color: T.textDim, fontSize: 9, fontFamily: T.mono }}>SKIP</span>;
  return <span style={{ color: T.textDim, fontSize: 9 }}>{'\u2014'}</span>;
}

function StrategyComparisonTable({ strategyWindows, selectedStrategies, knownStrategies }) {
  if (!strategyWindows || !strategyWindows.length) {
    return (
      <div style={{ ...S.card, textAlign: 'center', padding: 30, color: T.textDim, fontSize: 11, fontFamily: T.mono }}>
        No window data — waiting for /v58/strategy-windows
      </div>
    );
  }

  const activeCols = knownStrategies.filter(k => selectedStrategies.includes(k));

  return (
    <div style={{ ...S.card, padding: 0, overflow: 'auto', maxHeight: 520 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ background: T.headerBg, position: 'sticky', top: 0, zIndex: 2 }}>
            <th style={S.th}>Time</th>
            <th style={{ ...S.th, textAlign: 'center' }}>Actual</th>
            <th style={{ ...S.th, fontSize: 8 }}>VPIN</th>
            <th style={{ ...S.th, fontSize: 8 }}>Regime</th>
            {activeCols.map((key, i) => {
              const meta = strategyMeta(key, knownStrategies.indexOf(key));
              return (
                <th key={key} colSpan={2} style={{ ...S.th, textAlign: 'center', color: meta.color, borderLeft: `2px solid ${meta.color}33` }}>
                  {meta.label}
                </th>
              );
            })}
          </tr>
          <tr style={{ background: T.headerBg, position: 'sticky', top: 25, zIndex: 1 }}>
            {['', '', '', ''].map((_, i) => <th key={i} style={{ ...S.th, borderBottom: `2px solid ${T.border}` }} />)}
            {activeCols.map(key => {
              const meta = strategyMeta(key, knownStrategies.indexOf(key));
              return (
                <React.Fragment key={key}>
                  <th style={{ ...S.th, borderBottom: `2px solid ${T.border}`, borderLeft: `2px solid ${meta.color}33`, textAlign: 'center', color: T.textMuted }}>Action</th>
                  <th style={{ ...S.th, borderBottom: `2px solid ${T.border}`, textAlign: 'center', color: T.textMuted }}>Result</th>
                </React.Fragment>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {strategyWindows.map((w, i) => {
            const allSkip = activeCols.every(key => { const sd = w.strategies?.[key]; return !sd || sd.action === 'SKIP'; });
            return (
              <tr key={w.window_ts || i} style={{ background: i % 2 === 0 ? 'transparent' : 'rgba(15,23,42,0.3)', opacity: allSkip ? 0.45 : 1 }}>
                <td style={{ ...S.td, color: allSkip ? T.textDim : T.text }}>{windowTime(w.window_ts)}</td>
                <td style={{ ...S.td, textAlign: 'center', fontWeight: 700, color: dirColor(w.actual_direction) }}>{w.actual_direction || '\u2014'}</td>
                <td style={{ ...S.td, color: (w.vpin || 0) >= 0.55 ? T.amber : T.textDim, fontSize: 9 }}>{w.vpin != null ? fmt(w.vpin, 3) : '\u2014'}</td>
                <td style={{ ...S.td, fontSize: 9, color: T.textMuted }}>{(w.regime || '\u2014').slice(0, 12)}</td>
                {activeCols.map(key => {
                  const meta = strategyMeta(key, knownStrategies.indexOf(key));
                  const sd = w.strategies?.[key];
                  if (!sd) return (
                    <React.Fragment key={key}>
                      <td style={{ ...S.td, borderLeft: `2px solid ${meta.color}22`, textAlign: 'center', color: T.textDim }}>{'\u2014'}</td>
                      <td style={{ ...S.td, textAlign: 'center' }}><StratOutcomeChip outcome={null} /></td>
                    </React.Fragment>
                  );
                  let actionLabel = 'SKIP', actionColor = T.textDim;
                  if (sd.action === 'TRADE') {
                    actionLabel = sd.direction === 'UP' ? 'TRADE\u2191' : sd.direction === 'DOWN' ? 'TRADE\u2193' : 'TRADE';
                    actionColor = sd.direction === 'UP' ? T.green : T.red;
                  }
                  const modeColor = sd.mode === 'LIVE' ? T.green : T.purple;
                  return (
                    <React.Fragment key={key}>
                      <td style={{ ...S.td, borderLeft: `2px solid ${meta.color}22`, textAlign: 'center' }} title={sd.skip_reason || ''}>
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 3 }}>
                          <span style={{ color: actionColor, fontWeight: sd.action === 'TRADE' ? 700 : 400, fontSize: 10 }}>{actionLabel}</span>
                          {sd.mode && <span style={{ padding: '0 3px', borderRadius: 2, fontSize: 7, fontFamily: T.mono, fontWeight: 700, background: sd.mode === 'LIVE' ? 'rgba(16,185,129,0.15)' : 'rgba(168,85,247,0.12)', color: modeColor }}>{sd.mode}</span>}
                        </div>
                      </td>
                      <td style={{ ...S.td, textAlign: 'center' }}><StratOutcomeChip outcome={sd.outcome} /></td>
                    </React.Fragment>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}


// ═══════════════════════════════════════════════════════════════════════════
//  SECTION C: Accuracy by Signal Component
// ═══════════════════════════════════════════════════════════════════════════

function AccuracyBar({ label, wins, total, color }) {
  const pctVal = total > 0 ? (wins / total * 100) : 0;
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 2 }}>
        <span style={{ fontSize: 10, fontFamily: T.mono, color: T.text }}>{label}</span>
        <span style={{ fontSize: 10, fontFamily: T.mono, color: pctVal >= 55 ? T.green : T.amber }}>
          {pctVal.toFixed(1)}% ({wins}/{total})
        </span>
      </div>
      <div style={{ height: 6, background: 'rgba(51,65,85,0.4)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{
          height: '100%', borderRadius: 3, width: `${Math.min(pctVal, 100)}%`,
          background: color || (pctVal >= 55 ? T.green : T.amber),
          transition: 'width 0.4s ease-out',
        }} />
      </div>
    </div>
  );
}


function AccuracyCards({ outcomes }) {
  // Source agreement accuracy
  const agreeWindows = outcomes.filter(o => o.tfm_v57c_agree === true && o.actual_direction);
  const disagreeWindows = outcomes.filter(o => o.tfm_v57c_agree === false && o.actual_direction);
  const agreeWins = agreeWindows.filter(o => o.v57c_correct === true).length;
  const disagreeWins = disagreeWindows.filter(o => o.v57c_correct === true).length;

  // Sequoia accuracy by confidence bucket
  const confBuckets = {};
  outcomes.forEach(o => {
    if (o.actual_direction == null) return;
    const bucket = confidenceBucket(o.confidence);
    if (!bucket) return;
    if (!confBuckets[bucket]) confBuckets[bucket] = { wins: 0, total: 0 };
    confBuckets[bucket].total++;
    if (o.v57c_correct === true) confBuckets[bucket].wins++;
  });

  // VPIN accuracy by bucket
  const vpinBuckets = {};
  outcomes.forEach(o => {
    if (o.actual_direction == null) return;
    const bucket = vpinBucket(o.vpin);
    if (!bucket) return;
    if (!vpinBuckets[bucket]) vpinBuckets[bucket] = { wins: 0, total: 0 };
    vpinBuckets[bucket].total++;
    if (o.v57c_correct === true) vpinBuckets[bucket].wins++;
  });

  // Regime accuracy
  const regimeBuckets = {};
  outcomes.forEach(o => {
    if (o.actual_direction == null) return;
    const regime = o.regime || o.v71_regime || 'UNKNOWN';
    if (!regimeBuckets[regime]) regimeBuckets[regime] = { wins: 0, total: 0 };
    regimeBuckets[regime].total++;
    if (o.v57c_correct === true) regimeBuckets[regime].wins++;
  });

  const confOrder = ['0.8+', '0.7-0.8', '0.6-0.7', '0.5-0.6'];
  const vpinOrder = ['<0.4', '0.4-0.55', '0.55-0.7', '0.7+'];

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8 }}>
      {/* Source Agreement */}
      <div style={S.card}>
        <div style={{ ...S.sectionTitle, fontSize: 10, marginBottom: 12 }}>Source Agreement</div>
        <AccuracyBar label="Agree" wins={agreeWins} total={agreeWindows.length} color={T.green} />
        <AccuracyBar label="Disagree" wins={disagreeWins} total={disagreeWindows.length} color={T.red} />
      </div>

      {/* Sequoia Confidence */}
      <div style={S.card}>
        <div style={{ ...S.sectionTitle, fontSize: 10, marginBottom: 12 }}>Sequoia by Confidence</div>
        {confOrder.map(b => confBuckets[b] ? (
          <AccuracyBar key={b} label={b} wins={confBuckets[b].wins} total={confBuckets[b].total} color={T.purple} />
        ) : null)}
        {confOrder.every(b => !confBuckets[b]) && (
          <div style={{ color: T.textDim, fontSize: 10, fontFamily: T.mono }}>No data</div>
        )}
      </div>

      {/* VPIN */}
      <div style={S.card}>
        <div style={{ ...S.sectionTitle, fontSize: 10, marginBottom: 12 }}>VPIN by Bucket</div>
        {vpinOrder.map(b => vpinBuckets[b] ? (
          <AccuracyBar key={b} label={b} wins={vpinBuckets[b].wins} total={vpinBuckets[b].total} color={T.cyan} />
        ) : null)}
        {vpinOrder.every(b => !vpinBuckets[b]) && (
          <div style={{ color: T.textDim, fontSize: 10, fontFamily: T.mono }}>No data</div>
        )}
      </div>

      {/* Regime */}
      <div style={S.card}>
        <div style={{ ...S.sectionTitle, fontSize: 10, marginBottom: 12 }}>Regime Accuracy</div>
        {Object.entries(regimeBuckets)
          .sort((a, b) => b[1].total - a[1].total)
          .map(([regime, d]) => (
            <AccuracyBar key={regime} label={regime} wins={d.wins} total={d.total} color={T.amber} />
          ))
        }
        {Object.keys(regimeBuckets).length === 0 && (
          <div style={{ color: T.textDim, fontSize: 10, fontFamily: T.mono }}>No data</div>
        )}
      </div>
    </div>
  );
}


// ═══════════════════════════════════════════════════════════════════════════
//  SECTION D: P&L Charts (Inline SVG)
// ═══════════════════════════════════════════════════════════════════════════

function EquityCurve({ pnlTimeline }) {
  if (!pnlTimeline || pnlTimeline.length < 2) {
    return (
      <div style={{ ...S.card, textAlign: 'center', padding: 30, color: T.textDim, fontSize: 11, fontFamily: T.mono }}>
        Not enough data for equity curve
      </div>
    );
  }

  const W = 600, H = 200, PAD = { t: 20, r: 20, b: 30, l: 55 };
  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;

  const gatedVals = pnlTimeline.map(p => p.gated_cumulative ?? 0);
  const ungatedVals = pnlTimeline.map(p => p.ungated_cumulative ?? 0);
  const allVals = [...gatedVals, ...ungatedVals];
  const minY = Math.min(0, ...allVals);
  const maxY = Math.max(0.01, ...allVals);
  const rangeY = maxY - minY || 0.01;

  const xScale = (i) => PAD.l + (i / (pnlTimeline.length - 1)) * plotW;
  const yScale = (v) => PAD.t + plotH - ((v - minY) / rangeY) * plotH;

  const makePath = (vals) => {
    return vals.map((v, i) => {
      const x = xScale(i);
      const y = yScale(v);
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
  };

  const gatedPath = makePath(gatedVals);
  const ungatedPath = makePath(ungatedVals);

  // Zero line
  const zeroY = yScale(0);

  // Y-axis labels
  const yTicks = 5;
  const yLabels = [];
  for (let i = 0; i <= yTicks; i++) {
    const val = minY + (rangeY * i / yTicks);
    yLabels.push({ val, y: yScale(val) });
  }

  // X-axis labels (show ~5 timestamps)
  const xLabels = [];
  const step = Math.max(1, Math.floor(pnlTimeline.length / 5));
  for (let i = 0; i < pnlTimeline.length; i += step) {
    const ts = pnlTimeline[i].window_ts;
    xLabels.push({ x: xScale(i), label: utcHHMM(ts) });
  }

  return (
    <div style={S.card}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <div style={{ ...S.sectionTitle, marginBottom: 0 }}>Equity Curve</div>
        <div style={{ display: 'flex', gap: 12 }}>
          <span style={{ fontSize: 9, fontFamily: T.mono, color: T.cyan }}>
            <span style={{ display: 'inline-block', width: 12, height: 2, background: T.cyan, marginRight: 4, verticalAlign: 'middle' }} />
            Gated
          </span>
          <span style={{ fontSize: 9, fontFamily: T.mono, color: T.purple }}>
            <span style={{ display: 'inline-block', width: 12, height: 2, background: T.purple, marginRight: 4, verticalAlign: 'middle', opacity: 0.5 }} />
            Ungated
          </span>
        </div>
      </div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: 'block' }}>
        {/* Grid lines */}
        {yLabels.map((yl, i) => (
          <g key={i}>
            <line x1={PAD.l} x2={W - PAD.r} y1={yl.y} y2={yl.y}
              stroke="rgba(51,65,85,0.3)" strokeWidth={0.5} />
            <text x={PAD.l - 6} y={yl.y + 3} textAnchor="end"
              fill={T.textDim} fontSize={8} fontFamily={T.mono}>
              {yl.val >= 0 ? '+' : ''}{yl.val.toFixed(2)}
            </text>
          </g>
        ))}

        {/* Zero line */}
        <line x1={PAD.l} x2={W - PAD.r} y1={zeroY} y2={zeroY}
          stroke="rgba(100,116,139,0.5)" strokeWidth={1} strokeDasharray="4,3" />

        {/* X-axis labels */}
        {xLabels.map((xl, i) => (
          <text key={i} x={xl.x} y={H - 6} textAnchor="middle"
            fill={T.textDim} fontSize={8} fontFamily={T.mono}>
            {xl.label}
          </text>
        ))}

        {/* Ungated line (behind) */}
        <path d={ungatedPath} fill="none" stroke={T.purple} strokeWidth={1.5} opacity={0.4} />

        {/* Gated line (front) */}
        <path d={gatedPath} fill="none" stroke={T.cyan} strokeWidth={2} />

        {/* End points */}
        <circle cx={xScale(gatedVals.length - 1)} cy={yScale(gatedVals[gatedVals.length - 1])}
          r={3} fill={T.cyan} />
        <circle cx={xScale(ungatedVals.length - 1)} cy={yScale(ungatedVals[ungatedVals.length - 1])}
          r={3} fill={T.purple} opacity={0.5} />
      </svg>
    </div>
  );
}


function DailyPnlBars({ dailyPnl }) {
  if (!dailyPnl || !dailyPnl.length) {
    return (
      <div style={{ ...S.card, textAlign: 'center', padding: 30, color: T.textDim, fontSize: 11, fontFamily: T.mono }}>
        No daily P&L data
      </div>
    );
  }

  const W = 600, H = 160, PAD = { t: 15, r: 20, b: 30, l: 55 };
  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;

  const last30 = dailyPnl.slice(-30);
  const vals = last30.map(d => d.pnl_usd ?? d.pnl ?? 0);
  const maxAbs = Math.max(0.01, ...vals.map(Math.abs));
  const barW = Math.max(2, (plotW / last30.length) - 2);

  const xScale = (i) => PAD.l + (i / last30.length) * plotW + 1;
  const zeroY = PAD.t + plotH / 2;

  // Y labels
  const yLabels = [
    { val: maxAbs, y: PAD.t },
    { val: 0, y: zeroY },
    { val: -maxAbs, y: PAD.t + plotH },
  ];

  return (
    <div style={S.card}>
      <div style={{ ...S.sectionTitle, marginBottom: 8 }}>Daily P&L (Last 30 Days)</div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: 'block' }}>
        {/* Grid */}
        {yLabels.map((yl, i) => (
          <g key={i}>
            <line x1={PAD.l} x2={W - PAD.r} y1={yl.y} y2={yl.y}
              stroke="rgba(51,65,85,0.3)" strokeWidth={0.5} />
            <text x={PAD.l - 6} y={yl.y + 3} textAnchor="end"
              fill={T.textDim} fontSize={8} fontFamily={T.mono}>
              {yl.val >= 0 ? '+' : ''}{yl.val.toFixed(2)}
            </text>
          </g>
        ))}

        {/* Zero line */}
        <line x1={PAD.l} x2={W - PAD.r} y1={zeroY} y2={zeroY}
          stroke="rgba(100,116,139,0.5)" strokeWidth={1} />

        {/* Bars */}
        {last30.map((d, i) => {
          const v = d.pnl_usd ?? d.pnl ?? 0;
          const barH = Math.abs(v / maxAbs) * (plotH / 2);
          const x = xScale(i);
          const y = v >= 0 ? zeroY - barH : zeroY;
          return (
            <rect key={i} x={x} y={y} width={barW} height={Math.max(1, barH)}
              fill={v >= 0 ? T.green : T.red} opacity={0.7} rx={1} />
          );
        })}

        {/* X-axis labels (every ~5) */}
        {last30.filter((_, i) => i % Math.max(1, Math.floor(last30.length / 6)) === 0).map((d, i, arr) => {
          const idx = last30.indexOf(d);
          const dateStr = d.date ? d.date.slice(5) : '';
          return (
            <text key={i} x={xScale(idx) + barW / 2} y={H - 6} textAnchor="middle"
              fill={T.textDim} fontSize={7} fontFamily={T.mono}>
              {dateStr}
            </text>
          );
        })}
      </svg>
    </div>
  );
}


// ═══════════════════════════════════════════════════════════════════════════
//  MAIN COMPONENT
// ═══════════════════════════════════════════════════════════════════════════

export default function Evaluate() {
  const api = useApi();

  // Legacy data (Section C + D)
  const [accuracy, setAccuracy] = useState(null);
  const [outcomes, setOutcomes] = useState([]);
  const [dailyPnl, setDailyPnl] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Strategy comparison data (Section A + B)
  const [comparisonStats, setComparisonStats] = useState(null);
  const [strategyWindows, setStrategyWindows] = useState([]);
  const [knownStrategies, setKnownStrategies] = useState(['v10_gate', 'v4_fusion', 'v4_down_only', 'v4_up_asian']);
  const [selectedStrategies, setSelectedStrategies] = useState(['v4_down_only', 'v4_up_asian', 'v4_fusion']);

  useEffect(() => {
    const prev = document.title;
    document.title = 'Evaluate \u2014 Polymarket \u2014 Novakash';
    return () => { document.title = prev; };
  }, []);

  const fetchData = useCallback(async () => {
    try {
      const results = await Promise.allSettled([
        api('GET', '/v58/accuracy?limit=100'),
        api('GET', '/v58/outcomes?limit=100'),
        api('GET', '/pnl/daily'),
        api('GET', '/v58/strategy-comparison?days=7'),
        api('GET', '/v58/strategy-windows?limit=100&asset=BTC'),
      ]);
      const [accRes, outRes, pnlRes, compRes, winRes] = results;

      if (accRes.status === 'fulfilled') setAccuracy(accRes.value?.data || accRes.value);
      if (outRes.status === 'fulfilled') {
        const d = outRes.value?.data || outRes.value;
        setOutcomes(d?.outcomes ?? (Array.isArray(d) ? d : []));
      }
      if (pnlRes.status === 'fulfilled') {
        const d = pnlRes.value?.data || pnlRes.value;
        setDailyPnl(d?.data ?? (Array.isArray(d) ? d : []));
      }
      if (compRes.status === 'fulfilled') {
        const d = compRes.value?.data || compRes.value;
        setComparisonStats(Array.isArray(d) ? d : (d?.strategies ?? null));
      }
      if (winRes.status === 'fulfilled') {
        const d = winRes.value?.data || winRes.value;
        setStrategyWindows(d?.windows ?? (Array.isArray(d) ? d : []));
        if (d?.known_strategies?.length) {
          setKnownStrategies(prev => {
            const merged = [...prev];
            for (const k of d.known_strategies) if (!merged.includes(k)) merged.push(k);
            return merged;
          });
        }
      }
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch');
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => { fetchData(); }, [fetchData]);
  useEffect(() => { const t = setInterval(fetchData, 30000); return () => clearInterval(t); }, [fetchData]);

  const pnlTimeline = accuracy?.pnl_timeline || [];
  const windowCount = strategyWindows.length || outcomes.length;

  return (
    <div style={{ minHeight: '100vh', background: T.bg, color: T.text, padding: 12, fontFamily: T.mono, overflowY: 'auto' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <h1 style={{ fontSize: 16, fontWeight: 700, color: T.text, margin: 0, fontFamily: T.mono }}>Evaluate</h1>
          <span style={{ fontSize: 10, color: T.textMuted, fontFamily: T.mono }}>Polymarket Performance Analysis</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {loading && <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>loading...</span>}
          <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>{windowCount} windows</span>
        </div>
      </div>

      {error && (
        <div style={{ background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.3)', padding: '5px 10px', borderRadius: 3, marginBottom: 8, fontSize: 10, fontFamily: T.mono, color: '#fca5a5' }}>
          API Error: {error}
        </div>
      )}

      {/* Section A: Strategy Summary Cards */}
      <div style={{ marginBottom: 12 }}>
        <div style={S.sectionTitle}>Strategy Performance (7 days)</div>
        {comparisonStats && comparisonStats.length > 0 ? (
          <div style={{ display: 'grid', gridTemplateColumns: `repeat(${Math.min(comparisonStats.length, 4)}, 1fr)`, gap: 8 }}>
            {comparisonStats.map((s, i) => {
              const key = s.strategy_id || s.strategy || s.name || `s${i}`;
              const meta = strategyMeta(key, i);
              const wr = s.accuracy != null ? Number(s.accuracy) : null;
              const wrColor = wr == null ? T.textDim : wr >= 60 ? T.green : wr >= 50 ? T.amber : T.red;
              const mode = s.mode || 'GHOST';
              return (
                <div key={key} style={{ ...S.card, borderColor: `${meta.color}44` }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                    <span style={{ fontSize: 9, color: meta.color, fontFamily: T.mono, fontWeight: 700, textTransform: 'uppercase' }}>{meta.label}</span>
                    <span style={{ padding: '1px 5px', borderRadius: 2, fontSize: 8, fontFamily: T.mono, fontWeight: 700, background: mode === 'LIVE' ? 'rgba(16,185,129,0.15)' : 'rgba(168,85,247,0.15)', color: mode === 'LIVE' ? T.green : T.purple }}>{mode}</span>
                  </div>
                  <div style={{ fontSize: 26, fontWeight: 700, fontFamily: T.mono, color: wrColor, lineHeight: 1 }}>{wr != null ? `${wr.toFixed(1)}%` : '\u2014'}</div>
                  <div style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, marginTop: 5, display: 'flex', gap: 8 }}>
                    <span style={{ color: T.green }}>{s.wins ?? 0}W</span>
                    <span style={{ color: T.red }}>{s.losses ?? 0}L</span>
                    {s.skips != null && <span>{s.skips}S</span>}
                  </div>
                  {s.cum_pnl != null && <div style={{ fontSize: 10, fontFamily: T.mono, color: s.cum_pnl >= 0 ? T.green : T.red, marginTop: 4, fontWeight: 600 }}>{fmtUsd(s.cum_pnl)}</div>}
                </div>
              );
            })}
          </div>
        ) : (
          <div style={{ ...S.card, color: T.textDim, fontSize: 10, fontFamily: T.mono }}>Loading strategy comparison...</div>
        )}
      </div>

      {/* Section B: Strategy Comparison Table */}
      <div style={{ marginBottom: 12 }}>
        <div style={S.sectionTitle}>Strategy Comparison — Per Window</div>
        <StrategySelector
          knownStrategies={knownStrategies}
          selectedStrategies={selectedStrategies}
          setSelectedStrategies={setSelectedStrategies}
          comparisonStats={comparisonStats}
        />
        <StrategyComparisonTable
          strategyWindows={strategyWindows}
          selectedStrategies={selectedStrategies}
          knownStrategies={knownStrategies}
        />
      </div>

      {/* Section C: Accuracy by Signal Component */}
      <div style={{ marginBottom: 12 }}>
        <div style={S.sectionTitle}>Accuracy by Signal Component</div>
        <AccuracyCards outcomes={outcomes} />
      </div>

      {/* Section D: P&L Charts */}
      <div style={{ marginBottom: 16 }}>
        <div style={S.sectionTitle}>P&L Charts</div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <EquityCurve pnlTimeline={pnlTimeline} />
          <DailyPnlBars dailyPnl={dailyPnl} />
        </div>
      </div>
    </div>
  );
}
