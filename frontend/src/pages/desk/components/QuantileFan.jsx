// /desk Phase 2 — TimesFM quantile fan (p10/25/50/75/90) projected from now to close.
//
// Reads quantiles_full from /v4/snapshot.timescales.5m — 128 timesteps,
// 2 Hz from the TimesFM service → spans ~64 seconds of forward projection.
// X-axis is seconds-in-window (0–300), same framing as PriceChart so users
// read them side-by-side. The fan is anchored at `seconds_in_window =
// 300 - seconds_to_close` and fades out at the 128-timestep horizon.
//
// Y-axis is Δ% vs window-open Chainlink. We accept `targetPrice` from the
// parent and reuse PriceChart's Δ math for consistency.
//
// If TimesFM is OFFLINE (status !== 'ok', or quantiles_full missing) we
// render nothing — the fan is an S-tier nice-to-have, not a load-bearing
// surface. The PriceChart still shows the Chainlink line below.

import React, { useMemo } from 'react';
import { T } from '../../../theme/tokens.js';

const WINDOW_SECONDS = 300;
const CHART_W = 600;
const CHART_H = 180;
const PAD = { l: 36, r: 8, t: 8, b: 20 };

// TimesFM publishes at 2 Hz → 128 steps ≈ 64 s of forward projection.
// Older configs used 1 Hz; inspect the step count and infer.
function inferDt(n) {
  // 64 timesteps over 60s ≈ 1 Hz; 128 timesteps over 60s ≈ 2 Hz.
  if (n >= 120) return 0.5;
  if (n >= 60) return 1.0;
  return 1.0;
}

export default function QuantileFan({ fiveMin, targetPrice }) {
  const data = useMemo(() => computeFanPaths(fiveMin, targetPrice), [fiveMin, targetPrice]);

  if (!data) return null;

  const { paths, yRange, startS } = data;
  const xFor = s => PAD.l + (s / WINDOW_SECONDS) * (CHART_W - PAD.l - PAD.r);
  const yFor = d => {
    const span = Math.max(1e-6, yRange.max - yRange.min);
    const frac = (d - yRange.min) / span;
    return PAD.t + (1 - frac) * (CHART_H - PAD.t - PAD.b);
  };

  const toPath = pts => pts
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${xFor(p.s).toFixed(1)},${yFor(p.d).toFixed(1)}`)
    .join(' ');

  // Band = p10↔p90 as filled area, p25↔p75 inside. We build filled polygons
  // by concatenating the upper path with the reverse of the lower path.
  const bandPath = (upper, lower) => {
    if (!upper.length || !lower.length) return '';
    const upperSeg = upper
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${xFor(p.s).toFixed(1)},${yFor(p.d).toFixed(1)}`)
      .join(' ');
    const lowerSeg = lower
      .slice()
      .reverse()
      .map(p => `L${xFor(p.s).toFixed(1)},${yFor(p.d).toFixed(1)}`)
      .join(' ');
    return `${upperSeg} ${lowerSeg} Z`;
  };

  return (
    <Panel
      title="TimesFM Quantile Fan"
      subtitle="p10–p90 band · p25–p75 band · p50 line · projected from T-now"
    >
      <svg
        width={CHART_W}
        height={CHART_H}
        style={{ display: 'block', maxWidth: '100%' }}
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
      >
        <rect x={0} y={0} width={CHART_W} height={CHART_H} fill="transparent" />
        {/* zero line */}
        <line
          x1={PAD.l}
          x2={CHART_W - PAD.r}
          y1={yFor(0)}
          y2={yFor(0)}
          stroke={T.grid}
          strokeDasharray="2 3"
        />
        {/* y-axis endpoints */}
        <text x={4} y={PAD.t + 8} fill={T.label} fontSize="9" fontFamily={T.font}>
          {fmtPct(yRange.max)}
        </text>
        <text x={4} y={CHART_H - PAD.b} fill={T.label} fontSize="9" fontFamily={T.font}>
          {fmtPct(yRange.min)}
        </text>
        {/* x-axis tick marks */}
        {[0, 60, 120, 180, 240, 300].map(s => (
          <g key={s}>
            <line
              x1={xFor(s)}
              x2={xFor(s)}
              y1={CHART_H - PAD.b}
              y2={CHART_H - PAD.b + 3}
              stroke={T.grid}
            />
            <text
              x={xFor(s)}
              y={CHART_H - 4}
              fontSize="9"
              fill={T.label}
              fontFamily={T.font}
              textAnchor="middle"
            >
              {s}
            </text>
          </g>
        ))}
        {/* now marker */}
        <line
          x1={xFor(startS)}
          x2={xFor(startS)}
          y1={PAD.t}
          y2={CHART_H - PAD.b}
          stroke={T.cyan}
          strokeDasharray="3 3"
          opacity={0.3}
        />
        <text
          x={xFor(startS) + 3}
          y={PAD.t + 10}
          fill={T.cyan}
          fontSize="9"
          fontFamily={T.font}
          opacity={0.6}
        >
          now
        </text>

        {/* p10–p90 band (outer, light) */}
        <path d={bandPath(paths.p90, paths.p10)} fill={T.purple} fillOpacity={0.08} />
        {/* p25–p75 band (inner, darker) */}
        <path d={bandPath(paths.p75, paths.p25)} fill={T.purple} fillOpacity={0.18} />
        {/* p50 line */}
        <path d={toPath(paths.p50)} fill="none" stroke={T.purple} strokeWidth={1.5} />
      </svg>
    </Panel>
  );
}

function Panel({ title, subtitle, children }) {
  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em', marginBottom: 6 }}>
        {title}
      </div>
      {subtitle ? <div style={{ fontSize: 9, color: T.label2, marginBottom: 6 }}>{subtitle}</div> : null}
      {children}
    </div>
  );
}

function fmtPct(d) {
  if (!Number.isFinite(d)) return '';
  const sign = d >= 0 ? '+' : '';
  return `${sign}${(d * 100).toFixed(3)}%`;
}

// Compute paths for each quantile, converted to Δ% vs targetPrice.
// Returns null if inputs insufficient.
export function computeFanPaths(fiveMin, targetPrice) {
  if (!fiveMin || fiveMin.status !== 'ok') return null;
  const qf = fiveMin.quantiles_full;
  if (!qf || !qf.p50 || !Array.isArray(qf.p50) || !qf.p50.length) return null;
  if (!Number.isFinite(targetPrice) || targetPrice <= 0) return null;

  const secondsToClose = Number(fiveMin.seconds_to_close);
  if (!Number.isFinite(secondsToClose) || secondsToClose < 0) return null;
  const startS = WINDOW_SECONDS - secondsToClose; // seconds-in-window at "now"

  const n = qf.p50.length;
  const dt = inferDt(n);

  // Build (s, d) sequences per quantile.
  const quantKeys = ['p10', 'p25', 'p50', 'p75', 'p90'];
  const paths = {};
  for (const k of quantKeys) {
    const arr = qf[k];
    if (!Array.isArray(arr) || arr.length !== n) {
      return null; // corrupt shape — bail silently rather than render garbage
    }
    const pts = [];
    for (let i = 0; i < n; i++) {
      const s = startS + i * dt;
      if (s > WINDOW_SECONDS) break;
      const price = Number(arr[i]);
      if (!Number.isFinite(price)) continue;
      pts.push({ s, d: (price - targetPrice) / targetPrice });
    }
    paths[k] = pts;
  }

  // Compute y-range across all points.
  let min = 0, max = 0;
  for (const k of quantKeys) {
    for (const p of paths[k] || []) {
      if (p.d < min) min = p.d;
      if (p.d > max) max = p.d;
    }
  }
  if (max - min < 0.0005) { max = 0.001; min = -0.001; }

  return { paths, yRange: { min, max }, startS };
}
