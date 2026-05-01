// /desk Phase 2 — TimesFM quantile fan with anchored start/current price.
//
// Reads quantiles_full from /v4/snapshot.timescales.5m — 128 timesteps,
// 2 Hz from the TimesFM service → spans ~64 seconds of forward projection.
//
// Visual contract for the operator (rewritten 2026-05-01 from feedback that
// the original fan was ambiguous about "where does the bet break-even"):
//
//   • horizontal "TARGET @ OPEN $X" line at Δ% = 0 — this is the price
//     Polymarket resolves against. Drawing it solid + labelled means
//     UP-vs-DOWN is now a single eye-glance check.
//   • current-price marker at (xFor(now), yFor((px−target)/target)) with
//     a callout `$77,972 (+0.03%)` so the dollar value and the Δ% are
//     readable simultaneously.
//   • dual y-axis: Δ% on the left (existing), USD on the right derived
//     from `target * (1 + Δ)` so the eye reads dollars too.
//   • when TimesFM is OFFLINE we still render the anchor + current-price
//     marker on an empty chart instead of returning null — the surface is
//     more useful than nothing during outages.

import React, { useMemo } from 'react';
import { T } from '../../../theme/tokens.js';

const WINDOW_SECONDS = 300;
const CHART_W = 600;
const CHART_H = 220;
const PAD = { l: 56, r: 64, t: 14, b: 22 };

// TimesFM publishes at 2 Hz → 128 steps ≈ 64 s of forward projection.
function inferDt(n) {
  if (n >= 120) return 0.5;
  if (n >= 60) return 1.0;
  return 1.0;
}

export default function QuantileFan({ fiveMin, targetPrice, currentPrice }) {
  const data = useMemo(
    () => computeFanPaths(fiveMin, targetPrice, currentPrice),
    [fiveMin, targetPrice, currentPrice],
  );

  // Right-axis ($) labels are derived from the Δ%-axis range. Computed
  // unconditionally to keep hook order stable across the early-return.
  const yMax = data?.yRange?.max ?? 0;
  const yMin = data?.yRange?.min ?? 0;
  const dollarTicks = useMemo(() => {
    if (!Number.isFinite(targetPrice) || targetPrice <= 0) return [];
    return [yMax, (yMax + yMin) / 2, yMin].map(d => ({
      d,
      usd: targetPrice * (1 + d),
    }));
  }, [yMax, yMin, targetPrice]);

  if (!data) return null;

  const { paths, yRange, startS, hasFan, status, currentDelta } = data;

  const xFor = s => PAD.l + (s / WINDOW_SECONDS) * (CHART_W - PAD.l - PAD.r);
  const yFor = d => {
    const span = Math.max(1e-6, yRange.max - yRange.min);
    const frac = (d - yRange.min) / span;
    return PAD.t + (1 - frac) * (CHART_H - PAD.t - PAD.b);
  };

  const toPath = pts => pts
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${xFor(p.s).toFixed(1)},${yFor(p.d).toFixed(1)}`)
    .join(' ');

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

  const subtitle = hasFan
    ? 'p10–p90 band · p25–p75 band · p50 line · target line + live price'
    : `TimesFM ${status || 'offline'} — showing anchor + live price only`;

  const currentY = currentDelta != null ? yFor(currentDelta) : null;

  return (
    <Panel title="TimesFM Quantile Fan · BTC 5m" subtitle={subtitle}>
      <svg
        width={CHART_W}
        height={CHART_H}
        style={{ display: 'block', maxWidth: '100%' }}
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
      >
        <rect x={0} y={0} width={CHART_W} height={CHART_H} fill="transparent" />

        {/* x-axis ticks 0..300 */}
        {[0, 60, 120, 180, 240, 300].map(s => (
          <g key={s}>
            <line
              x1={xFor(s)} x2={xFor(s)}
              y1={CHART_H - PAD.b} y2={CHART_H - PAD.b + 3}
              stroke={T.grid}
            />
            <text
              x={xFor(s)} y={CHART_H - 6}
              fontSize="9" fill={T.label} fontFamily={T.font}
              textAnchor="middle"
            >
              {s}
            </text>
          </g>
        ))}

        {/* Δ% y-axis labels (left) */}
        <text x={4} y={PAD.t + 8} fill={T.label} fontSize="9" fontFamily={T.font}>
          {fmtPct(yRange.max)}
        </text>
        <text x={4} y={CHART_H - PAD.b - 2} fill={T.label} fontSize="9" fontFamily={T.font}>
          {fmtPct(yRange.min)}
        </text>
        <text
          x={4} y={(yFor(yRange.max) + yFor(yRange.min)) / 2 + 3}
          fill={T.label2} fontSize="9" fontFamily={T.font}
        >
          Δ%
        </text>

        {/* USD y-axis labels (right) */}
        {dollarTicks.map((t, i) => (
          <text
            key={i}
            x={CHART_W - 4}
            y={yFor(t.d) + 3}
            fill={T.label}
            fontSize="9"
            fontFamily={T.font}
            textAnchor="end"
          >
            ${fmtUsd(t.usd)}
          </text>
        ))}

        {/* TARGET @ OPEN — solid, labelled */}
        <line
          x1={PAD.l} x2={CHART_W - PAD.r}
          y1={yFor(0)} y2={yFor(0)}
          stroke={T.cyan} strokeOpacity={0.85}
          strokeWidth={1.25}
        />
        <text
          x={PAD.l + 4} y={yFor(0) - 4}
          fill={T.cyan} fontSize="10" fontFamily={T.font}
          opacity={0.95}
        >
          ▼ TARGET @ OPEN {Number.isFinite(targetPrice) ? `$${fmtUsd(targetPrice)}` : ''}
        </text>

        {/* "now" vertical marker */}
        <line
          x1={xFor(startS)} x2={xFor(startS)}
          y1={PAD.t} y2={CHART_H - PAD.b}
          stroke={T.label2} strokeDasharray="3 3" opacity={0.4}
        />
        <text
          x={xFor(startS) + 3} y={PAD.t + 9}
          fill={T.label2} fontSize="9" fontFamily={T.font}
          opacity={0.75}
        >
          now · T-{Math.max(0, Math.round(WINDOW_SECONDS - startS))}s
        </text>

        {/* p10–p90 band */}
        {hasFan && (
          <path d={bandPath(paths.p90, paths.p10)} fill={T.purple} fillOpacity={0.08} />
        )}
        {/* p25–p75 band */}
        {hasFan && (
          <path d={bandPath(paths.p75, paths.p25)} fill={T.purple} fillOpacity={0.18} />
        )}
        {/* p50 line */}
        {hasFan && (
          <path d={toPath(paths.p50)} fill="none" stroke={T.purple} strokeWidth={1.5} />
        )}

        {/* current-price marker — dot + callout */}
        {currentY != null && Number.isFinite(currentPrice) ? (
          <g>
            <line
              x1={PAD.l} x2={CHART_W - PAD.r}
              y1={currentY} y2={currentY}
              stroke={currentDelta >= 0 ? T.profit : T.loss}
              strokeOpacity={0.35}
              strokeDasharray="1 3"
            />
            <circle
              cx={xFor(startS)} cy={currentY}
              r={4}
              fill={currentDelta >= 0 ? T.profit : T.loss}
              stroke="#0b0b0e"
              strokeWidth={1.5}
            />
            <text
              x={xFor(startS) + 8}
              y={currentY - 6}
              fill={currentDelta >= 0 ? T.profit : T.loss}
              fontSize="11"
              fontFamily={T.font}
              fontWeight={600}
            >
              ${fmtUsd(currentPrice)} ({fmtPct(currentDelta)})
            </text>
          </g>
        ) : null}
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
  if (!Number.isFinite(d)) return '—';
  const sign = d >= 0 ? '+' : '';
  return `${sign}${(d * 100).toFixed(3)}%`;
}

function fmtUsd(v) {
  if (!Number.isFinite(v)) return '—';
  return v.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

// Compute paths for each quantile (Δ% vs targetPrice) plus the y-range that
// bounds both the fan and the live price marker. Returns an object always
// safe to render — `hasFan=false` means no TimesFM but the anchor + price
// dot will still draw.
export function computeFanPaths(fiveMin, targetPrice, currentPrice) {
  if (!Number.isFinite(targetPrice) || targetPrice <= 0) return null;

  // What seconds-in-window are we at? Source the snapshot first; fall back
  // to assuming we're at t=0 so the chart still draws something.
  const secondsToClose = Number(fiveMin?.seconds_to_close);
  const startS = Number.isFinite(secondsToClose) && secondsToClose >= 0
    ? Math.max(0, Math.min(WINDOW_SECONDS, WINDOW_SECONDS - secondsToClose))
    : 0;

  const status = fiveMin?.status;
  const qf = fiveMin?.quantiles_full;
  const haveQf = qf && Array.isArray(qf.p50) && qf.p50.length > 0
    && status === 'ok';

  const paths = { p10: [], p25: [], p50: [], p75: [], p90: [] };
  let hasFan = false;

  if (haveQf) {
    const n = qf.p50.length;
    const dt = inferDt(n);
    const quantKeys = ['p10', 'p25', 'p50', 'p75', 'p90'];
    let corrupt = false;
    for (const k of quantKeys) {
      const arr = qf[k];
      if (!Array.isArray(arr) || arr.length !== n) { corrupt = true; break; }
      for (let i = 0; i < n; i++) {
        const s = startS + i * dt;
        if (s > WINDOW_SECONDS) break;
        const price = Number(arr[i]);
        if (!Number.isFinite(price)) continue;
        paths[k].push({ s, d: (price - targetPrice) / targetPrice });
      }
    }
    hasFan = !corrupt && paths.p50.length > 0;
  }

  // Y range = union(fan extents, current-price delta, target [0]).
  const currentDelta = Number.isFinite(currentPrice)
    ? (currentPrice - targetPrice) / targetPrice
    : null;

  let min = 0, max = 0;
  if (hasFan) {
    for (const k of Object.keys(paths)) {
      for (const p of paths[k]) {
        if (p.d < min) min = p.d;
        if (p.d > max) max = p.d;
      }
    }
  }
  if (currentDelta != null) {
    if (currentDelta < min) min = currentDelta;
    if (currentDelta > max) max = currentDelta;
  }

  // Pad and guarantee non-degenerate range so the price dot has headroom.
  const pad = Math.max(0.0008, (max - min) * 0.15);
  min -= pad;
  max += pad;
  if (max - min < 0.0015) { max = 0.001; min = -0.001; }

  return {
    paths,
    yRange: { min, max },
    startS,
    hasFan,
    status,
    currentDelta,
  };
}
