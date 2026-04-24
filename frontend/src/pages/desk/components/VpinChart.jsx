// /desk Phase 2 — VPIN line with threshold bands.
//
// VPIN lives in a few places depending on engine state; we check them in
// priority order and take the first finite value:
//   1. fiveMin.sub_signals.vpin              (preferred, per spec)
//   2. fiveMin.vpin                          (legacy flat)
//   3. fiveMin.polymarket_live_recommended_outcome.extras.vpin
//   4. topLevel.macro.inputs.vpin_current
//
// Thresholds (per engine/config/constants.py):
//   0.55 → VPIN_INFORMED_THRESHOLD (amber — informed flow warning)
//   0.70 → VPIN_CASCADE_THRESHOLD  (red — cascade trigger)
//
// Data is collected client-side — the hub doesn't expose a VPIN history
// endpoint yet. We snapshot the current value every `pollMs` tick and
// keep a rolling 5-minute window of samples.

import React, { useEffect, useState } from 'react';
import { T } from '../../../theme/tokens.js';

const HISTORY_SECONDS = 300;
const CHART_W = 260;
const CHART_H = 120;
const PAD = { l: 24, r: 6, t: 6, b: 16 };

const INFORMED = 0.55;
const CASCADE = 0.70;

export function extractVpin(snap) {
  if (!snap) return null;
  const tryNum = v => {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const fm = snap.timescales?.['5m'] ?? snap.fiveMin ?? null;
  const candidates = [
    fm?.sub_signals?.vpin,
    fm?.vpin,
    fm?.polymarket_live_recommended_outcome?.extras?.vpin,
    snap?.macro?.inputs?.vpin_current,
  ];
  for (const c of candidates) {
    const n = tryNum(c);
    if (n != null) return n;
  }
  return null;
}

export default function VpinChart({ snapshot }) {
  // Local rolling history keyed by ts-second.
  const [history, setHistory] = useState([]); // [{ts: epoch_s, v: number}]

  useEffect(() => {
    const v = extractVpin(snapshot);
    if (v == null) return;
    const now = Math.floor(Date.now() / 1000);
    setHistory(prev => {
      // Dedup same-second samples (the 2s snapshot poller can fire twice inside one second).
      if (prev.length && prev[prev.length - 1].ts === now) return prev;
      const next = [...prev, { ts: now, v }];
      // Trim to rolling HISTORY_SECONDS.
      const cutoff = now - HISTORY_SECONDS;
      return next.filter(p => p.ts >= cutoff);
    });
  }, [snapshot]);

  const current = extractVpin(snapshot);
  const hasData = history.length >= 2;
  const yMin = 0;
  const yMax = 1;

  const xFor = ts => {
    const now = Math.floor(Date.now() / 1000);
    const ageS = now - ts;
    const frac = 1 - Math.min(1, ageS / HISTORY_SECONDS);
    return PAD.l + frac * (CHART_W - PAD.l - PAD.r);
  };
  const yFor = v => {
    const frac = (v - yMin) / (yMax - yMin);
    return PAD.t + (1 - frac) * (CHART_H - PAD.t - PAD.b);
  };

  const linePath = history
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${xFor(p.ts).toFixed(1)},${yFor(p.v).toFixed(1)}`)
    .join(' ');

  const currentColor = current == null
    ? T.label
    : current >= CASCADE ? T.loss
    : current >= INFORMED ? T.warn
    : T.profit;

  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
        marginBottom: 4,
      }}>
        <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em' }}>VPIN</div>
        <div style={{ fontSize: 14, color: currentColor, fontFamily: T.font, fontWeight: 500 }}>
          {current == null ? '—' : current.toFixed(3)}
        </div>
      </div>
      <div style={{ fontSize: 9, color: T.label2, marginBottom: 4 }}>
        5m rolling · informed ≥0.55 · cascade ≥0.70
      </div>

      {hasData ? (
        <svg
          width={CHART_W}
          height={CHART_H}
          style={{ display: 'block', maxWidth: '100%' }}
          viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        >
          {/* Threshold band: informed (amber, 0.55–0.70) */}
          <rect
            x={PAD.l}
            y={yFor(CASCADE)}
            width={CHART_W - PAD.l - PAD.r}
            height={yFor(INFORMED) - yFor(CASCADE)}
            fill={T.warn}
            fillOpacity={0.08}
          />
          {/* Threshold band: cascade (red, ≥ 0.70) */}
          <rect
            x={PAD.l}
            y={yFor(yMax)}
            width={CHART_W - PAD.l - PAD.r}
            height={yFor(CASCADE) - yFor(yMax)}
            fill={T.loss}
            fillOpacity={0.08}
          />
          {/* Threshold lines */}
          <line
            x1={PAD.l} x2={CHART_W - PAD.r}
            y1={yFor(INFORMED)} y2={yFor(INFORMED)}
            stroke={T.warn} strokeDasharray="2 3" opacity={0.7}
          />
          <line
            x1={PAD.l} x2={CHART_W - PAD.r}
            y1={yFor(CASCADE)} y2={yFor(CASCADE)}
            stroke={T.loss} strokeDasharray="2 3" opacity={0.7}
          />
          {/* y-axis labels */}
          <text x={2} y={yFor(0) + 3} fontSize="9" fill={T.label} fontFamily={T.font}>0</text>
          <text x={2} y={yFor(0.5) + 3} fontSize="9" fill={T.label} fontFamily={T.font}>.5</text>
          <text x={2} y={yFor(1) + 6} fontSize="9" fill={T.label} fontFamily={T.font}>1</text>
          {/* VPIN line */}
          <path d={linePath} fill="none" stroke={T.cyan} strokeWidth={1.5} />
        </svg>
      ) : (
        <div style={{
          color: T.label, fontSize: 10, padding: '20px 0', textAlign: 'center',
        }}>
          {current == null
            ? 'VPIN not exposed in snapshot yet'
            : 'Collecting history…'}
        </div>
      )}
    </div>
  );
}
