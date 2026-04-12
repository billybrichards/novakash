import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, GATE_NAMES, fmt, utcHHMM, pct } from './components/theme.js';
import WindowAnalysisModal from './components/WindowAnalysisModal.jsx';

/**
 * Polymarket Evaluate — "How Am I Doing?"
 *
 * Performance analysis dashboard with 4 sections:
 *   A. Performance Summary Cards
 *   B. Signal vs Outcome Table (filterable)
 *   C. Accuracy by Signal Component
 *   D. P&L Charts (inline SVG — no chart library)
 *
 * Polls 5 endpoints every 30s.
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
//  SECTION A: Performance Summary Cards
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
//  SECTION B: Signal vs Outcome Table
// ═══════════════════════════════════════════════════════════════════════════

function OutcomeFilters({ dateRange, setDateRange, direction, setDirection, outcome, setOutcome, gateFilter, setGateFilter }) {
  return (
    <div style={{ display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap', marginBottom: 10 }}>
      {/* Date range */}
      <div style={{ display: 'flex', gap: 3 }}>
        {DATE_RANGES.map(r => (
          <button key={r.key} style={S.pill(dateRange === r.key)} onClick={() => setDateRange(r.key)}>
            {r.label}
          </button>
        ))}
      </div>

      {/* Direction */}
      <div style={{ display: 'flex', gap: 3, alignItems: 'center' }}>
        <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, marginRight: 4 }}>DIR:</span>
        {['all', 'UP', 'DOWN'].map(d => (
          <button key={d} style={S.pill(direction === d)} onClick={() => setDirection(d)}>
            {d === 'all' ? 'All' : d}
          </button>
        ))}
      </div>

      {/* Outcome */}
      <div style={{ display: 'flex', gap: 3, alignItems: 'center' }}>
        <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, marginRight: 4 }}>RESULT:</span>
        {['all', 'WIN', 'LOSS', 'SKIP'].map(o => (
          <button key={o} style={S.pill(outcome === o)} onClick={() => setOutcome(o)}>
            {o === 'all' ? 'All' : o}
          </button>
        ))}
      </div>

      {/* Gate filter */}
      <div style={{ display: 'flex', gap: 3, alignItems: 'center' }}>
        <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, marginRight: 4 }}>GATE:</span>
        <select
          value={gateFilter}
          onChange={e => setGateFilter(e.target.value)}
          style={{
            background: 'rgba(51,65,85,0.5)', color: T.text, border: `1px solid ${T.border}`,
            borderRadius: 3, padding: '2px 6px', fontSize: 10, fontFamily: T.mono,
          }}
        >
          <option value="all">All</option>
          {GATE_PIPELINE.map(g => (
            <option key={g.key} value={g.key}>{g.label}</option>
          ))}
        </select>
      </div>
    </div>
  );
}


function OutcomeTable({ outcomes, gateMap }) {
  if (!outcomes.length) {
    return (
      <div style={{ ...S.card, textAlign: 'center', padding: 30, color: T.textDim, fontSize: 11, fontFamily: T.mono }}>
        No resolved windows match filters
      </div>
    );
  }

  return (
    <div style={{ ...S.card, padding: 0, overflow: 'auto', maxHeight: 480 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ background: T.headerBg, position: 'sticky', top: 0, zIndex: 1 }}>
            <th style={S.th}>Window</th>
            <th style={S.th}>Signal</th>
            <th style={S.th}>Actual</th>
            <th style={S.th}>Result</th>
            <th style={S.th}>Sequoia p_up</th>
            <th style={S.th}>VPIN</th>
            <th style={S.th}>Src Agree</th>
            <th style={S.th}>Regime</th>
            <th style={S.th}>Gate</th>
            <th style={S.th}>Block Reason</th>
            <th style={S.th}>Would-Have P&L</th>
          </tr>
        </thead>
        <tbody>
          {outcomes.map((o, i) => {
            const gate = gateMap[o.window_ts_epoch] || null;
            const gateDecision = gate?.decision || (o.v58_would_trade ? 'TRADE' : 'SKIP');
            const gateFailed = gate?.gate_failed || o.v58_skip_reason || o.skip_reason || null;

            const correct = o.v58_would_trade ? o.v58_correct : null;
            const chip = outcomeChip(correct);

            // For skipped windows, show ungated outcome
            const ungatedChip = outcomeChip(o.ungated_correct);

            // Source agreement: check if Chainlink+Tiingo agree via delta sources
            const srcAgree = o.tfm_v57c_agree;

            return (
              <tr key={o.window_ts || i} style={{
                background: i % 2 === 0 ? 'transparent' : 'rgba(15,23,42,0.3)',
                cursor: 'pointer',
              }} onClick={() => {
                const epoch = o.window_ts_epoch || (() => {
                  if (!o.window_ts) return 0;
                  const d = new Date(o.window_ts);
                  return isNaN(d) ? 0 : Math.floor(d.getTime() / 1000);
                })();
                if (epoch) setAnalysisWindow(epoch);
              }} title="Click to analyze window">
                <td style={S.td}>{windowTime(o.window_ts)}</td>
                <td style={{ ...S.td, color: dirColor(o.direction), fontWeight: 600 }}>
                  {o.direction || '\u2014'}
                </td>
                <td style={{ ...S.td, color: dirColor(o.actual_direction), fontWeight: 600 }}>
                  {o.actual_direction || '\u2014'}
                </td>
                <td style={S.td}>
                  {o.v58_would_trade ? (
                    <span style={{
                      padding: '1px 6px', borderRadius: 2, fontSize: 9,
                      background: chip.bg, color: chip.color, fontWeight: 600,
                    }}>
                      {chip.label}
                    </span>
                  ) : (
                    <span style={{ color: T.textDim, fontSize: 9 }}>
                      SKIP {ungatedChip.label !== '\u2014' ? `(${ungatedChip.label})` : ''}
                    </span>
                  )}
                </td>
                <td style={{ ...S.td, color: T.purple }}>
                  {o.confidence != null ? fmt(o.confidence, 3) : '\u2014'}
                </td>
                <td style={{ ...S.td, color: (o.vpin || 0) >= 0.55 ? T.amber : T.text }}>
                  {o.vpin != null ? fmt(o.vpin, 3) : '\u2014'}
                </td>
                <td style={S.td}>
                  <span style={{
                    color: srcAgree === true ? T.green : srcAgree === false ? T.red : T.textDim,
                    fontSize: 9, fontWeight: 600,
                  }}>
                    {srcAgree === true ? 'AGREE' : srcAgree === false ? 'DISAGREE' : '\u2014'}
                  </span>
                </td>
                <td style={{ ...S.td, fontSize: 9, color: T.textMuted }}>
                  {o.regime || o.v71_regime || '\u2014'}
                </td>
                <td style={S.td}>
                  <span style={{
                    padding: '1px 6px', borderRadius: 2, fontSize: 9, fontWeight: 600,
                    background: gateDecision === 'TRADE' ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.08)',
                    color: gateDecision === 'TRADE' ? T.green : T.red,
                  }}>
                    {gateDecision}
                  </span>
                </td>
                <td style={{ ...S.td, fontSize: 9, color: T.textMuted, maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis' }}
                  title={gateFailed || ''}>
                  {gateDecision === 'SKIP' ? (gateFailed || '\u2014') : '\u2014'}
                </td>
                <td style={{ ...S.td, color: pnlColor(o.ungated_pnl), fontWeight: 600 }}>
                  {!o.v58_would_trade && o.ungated_pnl != null ? fmtPnl(o.ungated_pnl) : '\u2014'}
                </td>
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

  // Data state
  const [accuracy, setAccuracy] = useState(null);
  const [outcomes, setOutcomes] = useState([]);
  const [stats, setStats] = useState(null);
  const [hqData, setHqData] = useState(null);
  const [dailyPnl, setDailyPnl] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Filter state
  const [dateRange, setDateRange] = useState('30d');
  const [direction, setDirection] = useState('all');
  const [outcome, setOutcome] = useState('all');
  const [gateFilter, setGateFilter] = useState('all');

  // Window analysis modal
  const [analysisWindow, setAnalysisWindow] = useState(null);

  // Browser tab title
  useEffect(() => {
    const prev = document.title;
    document.title = 'Evaluate \u2014 Polymarket \u2014 Novakash';
    return () => { document.title = prev; };
  }, []);

  // Fetch all endpoints
  const fetchData = useCallback(async () => {
    try {
      const results = await Promise.allSettled([
        api('GET', '/v58/stats?days=30'),
        api('GET', '/v58/accuracy?limit=100'),
        api('GET', '/v58/outcomes?limit=100'),
        api('GET', '/v58/execution-hq?asset=btc&timeframe=5m'),
        api('GET', '/pnl/daily'),
      ]);

      const [statsRes, accRes, outRes, hqRes, pnlRes] = results;

      if (statsRes.status === 'fulfilled') {
        setStats(statsRes.value?.data || statsRes.value);
      }
      if (accRes.status === 'fulfilled') {
        setAccuracy(accRes.value?.data || accRes.value);
      }
      if (outRes.status === 'fulfilled') {
        const outData = outRes.value?.data || outRes.value;
        setOutcomes(outData?.outcomes ?? (Array.isArray(outData) ? outData : []));
      }
      if (hqRes.status === 'fulfilled') {
        setHqData(hqRes.value?.data || hqRes.value);
      }
      if (pnlRes.status === 'fulfilled') {
        const pnlData = pnlRes.value?.data || pnlRes.value;
        setDailyPnl(pnlData?.data ?? (Array.isArray(pnlData) ? pnlData : []));
      }

      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch data');
    } finally {
      setLoading(false);
    }
  }, [api]);

  // Initial fetch
  useEffect(() => { fetchData(); }, [fetchData]);

  // Poll every 30s
  useEffect(() => {
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, [fetchData]);

  // Build gate heartbeat lookup: window_ts_epoch -> gate data
  const gateMap = useMemo(() => {
    const map = {};
    if (hqData?.gate_heartbeat) {
      for (const g of hqData.gate_heartbeat) {
        if (g.window_ts) {
          map[g.window_ts] = g;
        }
      }
    }
    return map;
  }, [hqData]);

  // Filter outcomes
  const filteredOutcomes = useMemo(() => {
    const now = Date.now();
    const rangeHours = DATE_RANGES.find(r => r.key === dateRange)?.hours ?? Infinity;

    return outcomes
      .map(o => ({
        ...o,
        window_ts_epoch: (() => {
          // Parse window_ts to epoch seconds for gate_heartbeat matching
          if (!o.window_ts) return 0;
          const d = new Date(o.window_ts);
          return Math.floor(d.getTime() / 1000);
        })(),
      }))
      .filter(o => {
        // Date range
        if (rangeHours < Infinity) {
          const age = now - parseWindowTs(o.window_ts);
          if (age > rangeHours * 3600 * 1000) return false;
        }
        // Direction
        if (direction !== 'all' && o.direction !== direction) return false;

        // Outcome
        if (outcome === 'WIN' && o.v58_correct !== true) return false;
        if (outcome === 'LOSS' && o.v58_correct !== false) return false;
        if (outcome === 'SKIP' && o.v58_would_trade !== false) return false;

        // Gate filter
        if (gateFilter !== 'all') {
          const gate = gateMap[o.window_ts_epoch];
          if (!gate) return true; // No gate data, show anyway
          if (gate.gate_failed !== gateFilter) return false;
        }

        return true;
      });
  }, [outcomes, dateRange, direction, outcome, gateFilter, gateMap]);

  // P&L timeline from accuracy data
  const pnlTimeline = accuracy?.pnl_timeline || [];

  return (
    <div style={{
      minHeight: '100vh', background: T.bg, color: T.text,
      padding: 12, fontFamily: T.mono,
      overflowY: 'auto',
    }}>
      {/* Header */}
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginBottom: 12,
      }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <h1 style={{ fontSize: 16, fontWeight: 700, color: T.white, margin: 0, fontFamily: T.mono }}>
            Evaluate
          </h1>
          <span style={{ fontSize: 10, color: T.textMuted, fontFamily: T.mono }}>
            Polymarket Performance Analysis
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {loading && (
            <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, animation: 'pulse 1.5s ease-in-out infinite' }}>
              loading...
            </span>
          )}
          <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>
            {outcomes.length} windows
          </span>
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div style={{
          background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.3)',
          padding: '5px 10px', borderRadius: 3, marginBottom: 8, fontSize: 10,
          fontFamily: T.mono, color: '#fca5a5',
        }}>
          API Error: {error}
        </div>
      )}

      {/* Section A: Summary Cards */}
      <div style={{ marginBottom: 12 }}>
        <SummaryCards accuracy={accuracy} stats={stats} />
      </div>

      {/* Section B: Signal vs Outcome */}
      <div style={{ marginBottom: 12 }}>
        <div style={S.sectionTitle}>Signal vs Outcome Analysis</div>
        <OutcomeFilters
          dateRange={dateRange} setDateRange={setDateRange}
          direction={direction} setDirection={setDirection}
          outcome={outcome} setOutcome={setOutcome}
          gateFilter={gateFilter} setGateFilter={setGateFilter}
        />
        <OutcomeTable outcomes={filteredOutcomes} gateMap={gateMap} />
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

      {/* Window Analysis Modal */}
      <WindowAnalysisModal
        windowTs={analysisWindow}
        onClose={() => setAnalysisWindow(null)}
      />
    </div>
  );
}
