// /desk — window-relative Chainlink price chart.
//
// Renders inline SVG per the FE onboarding rule "no Recharts for small
// charts" (§3.2 of docs/agent-handovers/frontend-onboarding.md). X-axis
// is seconds-in-window (0–300), y-axis is Δ% vs window-open price. Ghost
// lines for the previous 5 windows render at low opacity behind the
// current line for shape comparison.
//
// Data source: GET /api/ticks/chainlink?asset=BTC&limit=300 — roughly
// one point per ~5s of real time × N windows. We bin by 5-minute
// floor(ts) and only keep the last 6 windows (current + 5 ghosts).

import React, { useEffect, useMemo, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';
import { T } from '../../../theme/tokens.js';

const WINDOW_SECONDS = 300;
const CHART_W = 600;
const CHART_H = 180;
const PAD = { l: 36, r: 8, t: 8, b: 20 };

export default function PriceChart({ asset = 'BTC', windowEpoch, pollMs = 4_000 }) {
  const api = useApi();
  const [ticks, setTicks] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.get(`/api/ticks/chainlink?asset=${encodeURIComponent(asset)}&limit=400`);
        const body = r?.data ?? r;
        const raw = Array.isArray(body?.rows) ? body.rows : Array.isArray(body) ? body : [];
        if (!cancelled) {
          setTicks(raw);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e.message || 'ticks fetch failed');
      }
    };
    load();
    const h = setInterval(load, pollMs);
    return () => { cancelled = true; clearInterval(h); };
  }, [api, asset, pollMs]);

  const { current, ghosts, yRange } = useMemo(() => {
    return groupByWindow(ticks, windowEpoch);
  }, [ticks, windowEpoch]);

  if (error && !ticks.length) {
    return <Panel title="Window-relative Chainlink">
      <div style={{ color: T.loss, fontSize: 11 }}>{error}</div>
    </Panel>;
  }
  if (!ticks.length) {
    return <Panel title="Window-relative Chainlink">
      <div style={{ color: T.label, fontSize: 11 }}>Loading ticks…</div>
    </Panel>;
  }

  const xFor = s => PAD.l + (s / WINDOW_SECONDS) * (CHART_W - PAD.l - PAD.r);
  const yFor = d => {
    const { min, max } = yRange;
    const span = Math.max(1e-6, max - min);
    const frac = (d - min) / span;
    return PAD.t + (1 - frac) * (CHART_H - PAD.t - PAD.b);
  };

  const toPath = pts => pts
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${xFor(p.s).toFixed(1)},${yFor(p.d).toFixed(1)}`)
    .join(' ');

  return (
    <Panel title="Window-relative Chainlink" subtitle="x: seconds-in-window · y: Δ% vs window-open · ghosts = last 5 windows">
      <svg width={CHART_W} height={CHART_H} style={{ display: 'block', maxWidth: '100%' }} viewBox={`0 0 ${CHART_W} ${CHART_H}`}>
        <rect x={0} y={0} width={CHART_W} height={CHART_H} fill="transparent" />
        {/* zero line */}
        <line
          x1={PAD.l} x2={CHART_W - PAD.r}
          y1={yFor(0)} y2={yFor(0)}
          stroke={T.grid} strokeDasharray="2 3"
        />
        {/* axis labels — just endpoints for the y-axis */}
        <text x={4} y={PAD.t + 8} fill={T.label} fontSize="9" fontFamily={T.font}>
          {fmtPct(yRange.max)}
        </text>
        <text x={4} y={CHART_H - PAD.b} fill={T.label} fontSize="9" fontFamily={T.font}>
          {fmtPct(yRange.min)}
        </text>
        {/* x axis tick marks at 60s intervals */}
        {[0, 60, 120, 180, 240, 300].map(s => (
          <g key={s}>
            <line x1={xFor(s)} x2={xFor(s)} y1={CHART_H - PAD.b} y2={CHART_H - PAD.b + 3} stroke={T.grid} />
            <text x={xFor(s)} y={CHART_H - 4} fontSize="9" fill={T.label} fontFamily={T.font} textAnchor="middle">
              {s}
            </text>
          </g>
        ))}

        {/* ghosts */}
        {ghosts.map((g, i) => (
          <path
            key={g.epoch}
            d={toPath(g.pts)}
            fill="none"
            stroke={T.label}
            strokeOpacity={0.18}
            strokeWidth={1}
          />
        ))}
        {/* current */}
        {current.pts.length > 1 ? (
          <path
            d={toPath(current.pts)}
            fill="none"
            stroke={T.cyan}
            strokeWidth={1.5}
          />
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
  if (!Number.isFinite(d)) return '';
  const sign = d >= 0 ? '+' : '';
  return `${sign}${(d * 100).toFixed(2)}%`;
}

// Group rows into buckets keyed by window floor. Returns:
//   current = { epoch, pts: [{s, d}] }
//   ghosts  = array of up to 5 earlier windows
//   yRange  = {min, max} of delta across everything drawn
function groupByWindow(ticks, currentEpoch) {
  const byEpoch = new Map();
  for (const r of ticks) {
    const ts = Number(r.ts_unix ?? r.ts ?? r.timestamp ?? 0);
    // Normalise ms → s if huge.
    const tsS = ts > 1e12 ? Math.floor(ts / 1000) : Math.floor(ts);
    if (!Number.isFinite(tsS) || tsS <= 0) continue;
    const epoch = Math.floor(tsS / WINDOW_SECONDS) * WINDOW_SECONDS;
    const price = Number(r.price);
    if (!Number.isFinite(price)) continue;
    const arr = byEpoch.get(epoch) || [];
    arr.push({ tsS, price });
    byEpoch.set(epoch, arr);
  }

  // Sort + convert to relative (s, d) tuples per epoch.
  const asPts = (epoch, arr) => {
    const sorted = arr.slice().sort((a, b) => a.tsS - b.tsS);
    const p0 = sorted[0]?.price;
    if (!Number.isFinite(p0) || p0 === 0) return [];
    return sorted.map(x => ({
      s: Math.max(0, Math.min(WINDOW_SECONDS, x.tsS - epoch)),
      d: (x.price - p0) / p0,
    }));
  };

  const cur = currentEpoch ?? [...byEpoch.keys()].sort((a, b) => b - a)[0] ?? 0;
  const current = { epoch: cur, pts: asPts(cur, byEpoch.get(cur) || []) };

  const ghosts = [...byEpoch.keys()]
    .filter(e => e < cur)
    .sort((a, b) => b - a)
    .slice(0, 5)
    .map(epoch => ({ epoch, pts: asPts(epoch, byEpoch.get(epoch)) }));

  let min = 0, max = 0;
  for (const { pts } of [current, ...ghosts]) {
    for (const p of pts) {
      if (p.d < min) min = p.d;
      if (p.d > max) max = p.d;
    }
  }
  // Guarantee non-degenerate range.
  if (max - min < 0.0005) { max = 0.001; min = -0.001; }

  return { current, ghosts, yRange: { min, max } };
}
