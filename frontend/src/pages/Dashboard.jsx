/**
 * Dashboard.jsx — Novakash Trading Dashboard
 *
 * 7 real-time canvas charts wired to the hub API:
 *  1. VPIN Real-time Chart
 *  2. Cascade State Machine (SVG)
 *  3. Arb Spread Monitor
 *  4. Equity Curve with Drawdown Underlay
 *  5. Win Rate by VPIN Bucket
 *  6. Hourly Performance Heatmap
 *  7. Daily P&L Bars
 *
 * All charts fall back to seeded demo data when the API returns empty results.
 */

import { useEffect, useRef, useState, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';

// ─── Google Fonts: IBM Plex Mono ──────────────────────────────────────────────
if (!document.getElementById('ibm-plex-mono-font')) {
  const link = document.createElement('link');
  link.id = 'ibm-plex-mono-font';
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap';
  document.head.appendChild(link);
}

// ─── Theme ────────────────────────────────────────────────────────────────────
const T = {
  bg: '#07070c',
  chartBg: '#08080e',
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  purple: '#a855f7',
  cyan: '#06b6d4',
  profit: '#4ade80',
  loss: '#f87171',
  warning: '#f59e0b',
  label: 'rgba(255,255,255,0.3)',
  label2: 'rgba(255,255,255,0.5)',
  gridLine: 'rgba(255,255,255,0.04)',
  font: "'IBM Plex Mono', monospace",
};

// ─── Seeded PRNG ──────────────────────────────────────────────────────────────
function seededRng(seed) {
  let s = seed;
  return () => {
    s = (s * 1664525 + 1013904223) & 0xffffffff;
    return (s >>> 0) / 0xffffffff;
  };
}

// ─── Canvas helpers ───────────────────────────────────────────────────────────
function setupCanvas(canvas) {
  if (!canvas) return { ctx: null, w: 0, h: 0, dpr: 1 };
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = rect.width || canvas.offsetWidth || 600;
  const h = rect.height || canvas.offsetHeight || 240;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  return { ctx, w, h, dpr };
}

function drawGrid(ctx, w, h, cols = 6, rows = 4) {
  ctx.save();
  ctx.strokeStyle = T.gridLine;
  ctx.lineWidth = 1;
  for (let i = 1; i < cols; i++) {
    const x = (w / cols) * i;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
  }
  for (let i = 1; i < rows; i++) {
    const y = (h / rows) * i;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }
  ctx.restore();
}

// ─── Demo Data Generators ─────────────────────────────────────────────────────
function genVpinDemo() {
  const rng = seededRng(42);
  const data = [];
  let vpin = 0.35;
  let btcPrice = 65000;
  for (let t = 0; t < 200; t++) {
    vpin = Math.min(0.95, Math.max(0.1, vpin + (rng() - 0.48) * 0.04));
    btcPrice += (rng() - 0.5) * 400;
    data.push({ t, vpin, btcPrice });
  }
  return data;
}

function genArbDemo() {
  const rng = seededRng(77);
  return Array.from({ length: 120 }, () => 0.92 + rng() * 0.14);
}

function genEquityDemo() {
  const rng = seededRng(13);
  let balance = 1000;
  return Array.from({ length: 60 }, (_, i) => {
    balance += (rng() - 0.42) * 45;
    return { day: `Day ${i + 1}`, balance: Math.max(600, balance) };
  });
}

function genDailyPnlDemo() {
  const rng = seededRng(55);
  return Array.from({ length: 60 }, () => (rng() - 0.42) * 80);
}

function genTradesDemo() {
  const rng = seededRng(99);
  const outcomes = ['WIN', 'LOSS', 'WIN', 'WIN', 'LOSS'];
  return Array.from({ length: 200 }, (_, i) => ({
    id: i,
    outcome: outcomes[Math.floor(rng() * outcomes.length)],
    pnl_usd: (rng() - 0.4) * 60,
    vpin: 0.28 + rng() * 0.55,
    hour: Math.floor(rng() * 24),
    dayOfWeek: Math.floor(rng() * 7),
    strategy: 'vpin_cascade',
    stake_usd: 20 + rng() * 80,
  }));
}

// ─── vpinColour helper ────────────────────────────────────────────────────────
function vpinColour(v) {
  if (v < 0.40) return T.profit;
  if (v < 0.55) return T.warning;
  if (v < 0.70) return '#fb923c';
  return T.loss;
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 1: VPIN Real-time Chart
// ═══════════════════════════════════════════════════════════════════════════════
function VpinChart({ data }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const draw = () => {
      const { ctx, w, h } = setupCanvas(canvas);
      if (!ctx) return;

      // Background
      ctx.fillStyle = T.chartBg;
      ctx.fillRect(0, 0, w, h);
      drawGrid(ctx, w, h, 8, 5);

      const PAD = { top: 32, right: 96, bottom: 32, left: 40 };
      const cw = w - PAD.left - PAD.right;
      const ch = h - PAD.top - PAD.bottom;

      const minV = 0.0, maxV = 1.0;
      const toX = (i) => PAD.left + (i / Math.max(data.length - 1, 1)) * cw;
      const toY = (v) => PAD.top + (1 - (v - minV) / (maxV - minV)) * ch;

      // Threshold zone fills
      const zones = [
        { from: 0.0, to: 0.40, color: 'rgba(74,222,128,0.04)' },
        { from: 0.40, to: 0.55, color: 'rgba(245,158,11,0.05)' },
        { from: 0.55, to: 0.70, color: 'rgba(251,146,60,0.06)' },
        { from: 0.70, to: 1.0, color: 'rgba(248,113,113,0.07)' },
      ];
      zones.forEach(({ from, to, color }) => {
        ctx.fillStyle = color;
        ctx.fillRect(PAD.left, toY(to), cw, toY(from) - toY(to));
      });

      // Threshold lines
      [[0.55, 'INFORMED', T.warning], [0.70, 'CASCADE', T.loss]].forEach(([v, label, color]) => {
        const y = toY(v);
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 6]);
        ctx.globalAlpha = 0.5;
        ctx.beginPath();
        ctx.moveTo(PAD.left, y);
        ctx.lineTo(PAD.left + cw, y);
        ctx.stroke();
        ctx.restore();
        ctx.fillStyle = color;
        ctx.font = `10px ${T.font}`;
        ctx.globalAlpha = 0.6;
        ctx.fillText(label, PAD.left + cw + 4, y + 4);
        ctx.globalAlpha = 1;
      });

      if (data.length < 2) return;

      // Draw line segments with colour per value
      ctx.save();
      ctx.lineWidth = 1.5;
      for (let i = 1; i < data.length; i++) {
        const x0 = toX(i - 1), y0 = toY(data[i - 1].vpin);
        const x1 = toX(i), y1 = toY(data[i].vpin);
        ctx.strokeStyle = vpinColour(data[i].vpin);
        ctx.globalAlpha = 0.85;
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.lineTo(x1, y1);
        ctx.stroke();
      }
      ctx.restore();

      // Glow dots on high values (> 0.70)
      data.forEach((d, i) => {
        if (d.vpin > 0.70) {
          const x = toX(i), y = toY(d.vpin);
          const grd = ctx.createRadialGradient(x, y, 0, x, y, 8);
          grd.addColorStop(0, 'rgba(248,113,113,0.6)');
          grd.addColorStop(1, 'rgba(248,113,113,0)');
          ctx.fillStyle = grd;
          ctx.beginPath();
          ctx.arc(x, y, 8, 0, Math.PI * 2);
          ctx.fill();
        }
      });

      // Y-axis labels
      ctx.font = `10px ${T.font}`;
      ctx.fillStyle = T.label;
      [0.0, 0.25, 0.50, 0.75, 1.0].forEach(v => {
        ctx.fillText(v.toFixed(2), 2, toY(v) + 4);
      });

      // Current value callout top-right
      const last = data[data.length - 1];
      if (last) {
        const cv = last.vpin;
        const col = vpinColour(cv);
        ctx.save();
        ctx.fillStyle = 'rgba(0,0,0,0.6)';
        ctx.strokeStyle = col;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.roundRect(w - 88, 6, 82, 22, 4);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = col;
        ctx.font = `600 12px ${T.font}`;
        ctx.fillText(`VPIN ${cv.toFixed(3)}`, w - 83, 22);
        ctx.restore();
      }
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, [data]);

  return (
    <div style={styles.chartWrap}>
      <div style={styles.chartTitle}>VPIN — Volume-Synchronised Informed Trading Probability</div>
      <canvas ref={canvasRef} style={styles.canvas} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 2: Cascade State Machine (SVG)
// ═══════════════════════════════════════════════════════════════════════════════
const CASCADE_STATES = ['IDLE', 'DETECTED', 'EXHAUSTING', 'BET SIGNAL', 'COOLDOWN'];
const CASCADE_ARROWS = [
  ['IDLE', 'DETECTED', 'OI spike'],
  ['DETECTED', 'EXHAUSTING', 'OI fade'],
  ['EXHAUSTING', 'BET SIGNAL', 'threshold'],
  ['BET SIGNAL', 'COOLDOWN', 'bet placed'],
  ['COOLDOWN', 'IDLE', 'timeout'],
];

function CascadeChart({ cascadeData }) {
  const { state = 'IDLE', direction = '—', oi_delta = 0 } = cascadeData || {};
  const activeIdx = CASCADE_STATES.indexOf(state.toUpperCase());

  const nodeW = 100, nodeH = 36, hGap = 24;
  const totalW = CASCADE_STATES.length * nodeW + (CASCADE_STATES.length - 1) * hGap;
  const svgH = 100;

  return (
    <div style={styles.chartWrap}>
      <div style={styles.chartTitle}>Cascade State Machine</div>
      <div style={{ background: T.chartBg, borderRadius: 6, padding: '16px 8px 8px', position: 'relative' }}>
        <svg width="100%" viewBox={`0 0 ${totalW + 20} ${svgH}`} style={{ overflow: 'visible' }}>
          {/* Arrows between nodes */}
          {CASCADE_ARROWS.map(([from, to, label], i) => {
            const x1 = 10 + i * (nodeW + hGap) + nodeW;
            const x2 = 10 + (i + 1) * (nodeW + hGap);
            const y = svgH / 2;
            return (
              <g key={i}>
                <line x1={x1} y1={y} x2={x2} y2={y} stroke="rgba(255,255,255,0.12)" strokeWidth="1" markerEnd="url(#arrow)" />
                <text x={(x1 + x2) / 2} y={y - 8} textAnchor="middle" fill="rgba(255,255,255,0.25)" fontSize="8" fontFamily={T.font}>{label}</text>
              </g>
            );
          })}

          {/* Arrowhead marker */}
          <defs>
            <marker id="arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6 Z" fill="rgba(255,255,255,0.2)" />
            </marker>
          </defs>

          {/* State nodes */}
          {CASCADE_STATES.map((s, i) => {
            const x = 10 + i * (nodeW + hGap);
            const y = svgH / 2 - nodeH / 2;
            const isActive = i === activeIdx;
            const fill = isActive ? (s === 'BET SIGNAL' ? T.profit : T.purple) : 'rgba(255,255,255,0.04)';
            const stroke = isActive ? (s === 'BET SIGNAL' ? T.profit : T.purple) : 'rgba(255,255,255,0.1)';
            const textColor = isActive ? '#fff' : 'rgba(255,255,255,0.3)';

            return (
              <g key={s}>
                {isActive && (
                  <rect x={x - 4} y={y - 4} width={nodeW + 8} height={nodeH + 8} rx="8"
                    fill="none" stroke={fill} strokeWidth="1" opacity="0.4">
                    <animate attributeName="opacity" values="0.4;0.1;0.4" dur="2s" repeatCount="indefinite" />
                  </rect>
                )}
                <rect x={x} y={y} width={nodeW} height={nodeH} rx="4" fill={fill} fillOpacity={isActive ? 0.18 : 1} stroke={stroke} strokeWidth="1" />
                <text x={x + nodeW / 2} y={y + nodeH / 2 + 4} textAnchor="middle"
                  fill={textColor} fontSize="10" fontWeight={isActive ? '600' : '400'} fontFamily={T.font}>
                  {s}
                </text>
              </g>
            );
          })}
        </svg>

        {/* Stat cards */}
        <div style={{ display: 'flex', gap: 12, marginTop: 16, padding: '0 4px' }}>
          {[
            { label: 'State', value: state, color: activeIdx === 3 ? T.profit : T.purple },
            { label: 'Direction', value: direction || '—', color: T.cyan },
            { label: 'OI Δ', value: typeof oi_delta === 'number' ? (oi_delta > 0 ? '+' : '') + oi_delta.toFixed(2) + 'M' : '—', color: oi_delta > 0 ? T.profit : T.loss },
          ].map(({ label, value, color }) => (
            <div key={label} style={{ flex: 1, background: T.card, border: `1px solid ${T.border}`, borderRadius: 6, padding: '8px 12px' }}>
              <div style={{ fontSize: 10, color: T.label, fontFamily: T.font, marginBottom: 4 }}>{label}</div>
              <div style={{ fontSize: 14, color, fontFamily: T.font, fontWeight: 600 }}>{value}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 3: Arb Spread Monitor
// ═══════════════════════════════════════════════════════════════════════════════
function ArbChart({ data }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const draw = () => {
      const { ctx, w, h } = setupCanvas(canvas);
      if (!ctx) return;

      ctx.fillStyle = T.chartBg;
      ctx.fillRect(0, 0, w, h);
      drawGrid(ctx, w, h, 8, 4);

      const PAD = { top: 24, right: 16, bottom: 28, left: 44 };
      const cw = w - PAD.left - PAD.right;
      const ch = h - PAD.top - PAD.bottom;

      if (!data.length) return;

      const minV = Math.min(...data, 0.85);
      const maxV = Math.max(...data, 1.10);
      const toX = (i) => PAD.left + (i / Math.max(data.length - 1, 1)) * cw;
      const toY = (v) => PAD.top + (1 - (v - minV) / (maxV - minV)) * ch;
      const parityY = toY(1.0);

      // Green profit zone
      ctx.save();
      ctx.fillStyle = 'rgba(74,222,128,0.06)';
      ctx.fillRect(PAD.left, parityY, cw, PAD.top + ch - parityY);
      ctx.restore();

      // Parity line
      ctx.save();
      ctx.strokeStyle = 'rgba(255,255,255,0.2)';
      ctx.lineWidth = 1;
      ctx.setLineDash([6, 6]);
      ctx.beginPath();
      ctx.moveTo(PAD.left, parityY);
      ctx.lineTo(PAD.left + cw, parityY);
      ctx.stroke();
      ctx.restore();

      ctx.font = `10px ${T.font}`;
      ctx.fillStyle = 'rgba(255,255,255,0.2)';
      ctx.fillText('$1.00 parity', PAD.left + 4, parityY - 4);

      // Filled area under line
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(toX(0), PAD.top + ch);
      data.forEach((v, i) => ctx.lineTo(toX(i), toY(v)));
      ctx.lineTo(toX(data.length - 1), PAD.top + ch);
      ctx.closePath();
      ctx.fillStyle = 'rgba(6,182,212,0.08)';
      ctx.fill();
      ctx.restore();

      // Line
      ctx.save();
      ctx.strokeStyle = T.cyan;
      ctx.lineWidth = 1.5;
      ctx.lineJoin = 'round';
      ctx.beginPath();
      data.forEach((v, i) => i === 0 ? ctx.moveTo(toX(i), toY(v)) : ctx.lineTo(toX(i), toY(v)));
      ctx.stroke();
      ctx.restore();

      // Arb opportunity dots (below parity)
      let arbCount = 0;
      data.forEach((v, i) => {
        if (v < 1.0) {
          arbCount++;
          const x = toX(i), y = toY(v);
          const grd = ctx.createRadialGradient(x, y, 0, x, y, 6);
          grd.addColorStop(0, 'rgba(74,222,128,0.8)');
          grd.addColorStop(1, 'rgba(74,222,128,0)');
          ctx.fillStyle = grd;
          ctx.beginPath();
          ctx.arc(x, y, 6, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = T.profit;
          ctx.beginPath();
          ctx.arc(x, y, 2.5, 0, Math.PI * 2);
          ctx.fill();
        }
      });

      // Arb count badge
      ctx.save();
      ctx.fillStyle = 'rgba(0,0,0,0.6)';
      ctx.strokeStyle = T.profit;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(w - 114, 6, 108, 20, 4);
      ctx.fill(); ctx.stroke();
      ctx.fillStyle = T.profit;
      ctx.font = `11px ${T.font}`;
      ctx.fillText(`${arbCount} arbs detected`, w - 109, 20);
      ctx.restore();

      // Y-axis
      ctx.font = `10px ${T.font}`;
      ctx.fillStyle = T.label;
      [minV, (minV + maxV) / 2, maxV].forEach(v => {
        ctx.fillText(`$${v.toFixed(2)}`, 2, toY(v) + 4);
      });
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, [data]);

  return (
    <div style={styles.chartWrap}>
      <div style={styles.chartTitle}>Arb Spread Monitor — YES+NO Combined Price</div>
      <canvas ref={canvasRef} style={styles.canvas} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 4: Equity Curve with Drawdown
// ═══════════════════════════════════════════════════════════════════════════════
function EquityChart({ data }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const draw = () => {
      const { ctx, w, h } = setupCanvas(canvas);
      if (!ctx) return;

      ctx.fillStyle = T.chartBg;
      ctx.fillRect(0, 0, w, h);
      drawGrid(ctx, w, h, 8, 4);

      if (!data.length) return;

      const PAD = { top: 28, right: 100, bottom: 28, left: 60 };
      const cw = w - PAD.left - PAD.right;
      const ch = h - PAD.top - PAD.bottom;

      const balances = data.map(d => d.balance);
      const startBalance = balances[0] || 1000;
      const minV = Math.min(...balances, startBalance * 0.8);
      const maxV = Math.max(...balances, startBalance * 1.2);
      const toX = (i) => PAD.left + (i / Math.max(data.length - 1, 1)) * cw;
      const toY = (v) => PAD.top + (1 - (v - minV) / (maxV - minV)) * ch;

      // Compute drawdown
      let peak = balances[0];
      const drawdowns = balances.map(b => {
        peak = Math.max(peak, b);
        return peak > 0 ? (b - peak) / peak : 0;
      });

      const ddMin = Math.min(...drawdowns, -0.15);
      const ddH = 50;
      const ddTop = PAD.top + ch + 8;
      const toYdd = (dd) => ddTop + (1 - (dd - ddMin) / (0 - ddMin)) * ddH;

      // Drawdown fill
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(toX(0), ddTop);
      drawdowns.forEach((dd, i) => ctx.lineTo(toX(i), toYdd(dd)));
      ctx.lineTo(toX(drawdowns.length - 1), ddTop);
      ctx.closePath();
      ctx.fillStyle = 'rgba(248,113,113,0.2)';
      ctx.fill();
      ctx.strokeStyle = T.loss;
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.restore();

      ctx.font = `9px ${T.font}`;
      ctx.fillStyle = T.label;
      ctx.fillText('Drawdown', PAD.left, ddTop - 2);

      // Breakeven line
      const beY = toY(startBalance);
      ctx.save();
      ctx.strokeStyle = 'rgba(255,255,255,0.15)';
      ctx.lineWidth = 1;
      ctx.setLineDash([6, 6]);
      ctx.beginPath();
      ctx.moveTo(PAD.left, beY);
      ctx.lineTo(PAD.left + cw, beY);
      ctx.stroke();
      ctx.restore();

      // Equity line gradient fill
      const currentBalance = balances[balances.length - 1];
      const isProfit = currentBalance >= startBalance;

      ctx.save();
      ctx.beginPath();
      ctx.moveTo(toX(0), toY(startBalance));
      balances.forEach((b, i) => ctx.lineTo(toX(i), toY(b)));
      ctx.lineTo(toX(balances.length - 1), toY(startBalance));
      ctx.closePath();
      ctx.fillStyle = isProfit ? 'rgba(74,222,128,0.08)' : 'rgba(248,113,113,0.08)';
      ctx.fill();
      ctx.restore();

      // Equity line
      ctx.save();
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      ctx.strokeStyle = isProfit ? T.profit : T.loss;
      ctx.beginPath();
      balances.forEach((b, i) => i === 0 ? ctx.moveTo(toX(i), toY(b)) : ctx.lineTo(toX(i), toY(b)));
      ctx.stroke();
      ctx.restore();

      // Y-axis
      ctx.font = `10px ${T.font}`;
      ctx.fillStyle = T.label;
      [minV, startBalance, maxV].forEach(v => {
        ctx.fillText(`$${v.toFixed(0)}`, 2, toY(v) + 4);
      });

      // Callout
      const pctReturn = ((currentBalance - startBalance) / startBalance * 100).toFixed(1);
      const callColor = isProfit ? T.profit : T.loss;
      ctx.save();
      ctx.fillStyle = 'rgba(0,0,0,0.6)';
      ctx.strokeStyle = callColor;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(w - 94, 6, 88, 38, 4);
      ctx.fill(); ctx.stroke();
      ctx.fillStyle = callColor;
      ctx.font = `600 13px ${T.font}`;
      ctx.fillText(`$${currentBalance.toFixed(0)}`, w - 88, 24);
      ctx.font = `11px ${T.font}`;
      ctx.fillStyle = callColor;
      ctx.fillText(`${pctReturn > 0 ? '+' : ''}${pctReturn}%`, w - 88, 38);
      ctx.restore();
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, [data]);

  return (
    <div style={styles.chartWrap}>
      <div style={styles.chartTitle}>Equity Curve</div>
      <canvas ref={canvasRef} style={{ ...styles.canvas, height: 280 }} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 5: Win Rate by VPIN Bucket
// ═══════════════════════════════════════════════════════════════════════════════
function WinRateBucketChart({ trades }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    // Bucket trades by VPIN at entry, step 0.05, range 0.30–0.80
    const buckets = {};
    for (let v = 0.30; v < 0.82; v = Math.round((v + 0.05) * 100) / 100) {
      buckets[v.toFixed(2)] = { wins: 0, total: 0 };
    }
    trades.forEach(t => {
      if (t.vpin == null) return;
      const bucket = (Math.floor(t.vpin / 0.05) * 0.05).toFixed(2);
      if (buckets[bucket]) {
        buckets[bucket].total++;
        if (t.outcome === 'WIN') buckets[bucket].wins++;
      }
    });

    const bucketKeys = Object.keys(buckets).sort();
    const bucketData = bucketKeys.map(k => ({
      label: k,
      wr: buckets[k].total > 0 ? buckets[k].wins / buckets[k].total : null,
      n: buckets[k].total,
    }));

    const draw = () => {
      const { ctx, w, h } = setupCanvas(canvas);
      if (!ctx) return;

      ctx.fillStyle = T.chartBg;
      ctx.fillRect(0, 0, w, h);
      drawGrid(ctx, w, h, bucketData.length, 4);

      const PAD = { top: 24, right: 16, bottom: 36, left: 44 };
      const cw = w - PAD.left - PAD.right;
      const ch = h - PAD.top - PAD.bottom;

      const barW = cw / bucketData.length;
      const barPad = 4;

      // 50% line
      const y50 = PAD.top + (1 - 0.5) * ch;
      ctx.save();
      ctx.strokeStyle = 'rgba(255,255,255,0.2)';
      ctx.lineWidth = 1;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(PAD.left, y50);
      ctx.lineTo(PAD.left + cw, y50);
      ctx.stroke();
      ctx.restore();

      ctx.font = `9px ${T.font}`;
      ctx.fillStyle = 'rgba(255,255,255,0.2)';
      ctx.fillText('50%', 2, y50 + 4);

      bucketData.forEach((b, i) => {
        const x = PAD.left + i * barW + barPad;
        const bw = barW - barPad * 2;
        if (b.wr === null) return;

        const barH = b.wr * ch;
        const by = PAD.top + ch - barH;
        const color = b.wr > 0.54 ? T.profit : b.wr > 0.50 ? T.warning : T.loss;

        ctx.fillStyle = color;
        ctx.globalAlpha = 0.7;
        ctx.fillRect(x, by, bw, barH);
        ctx.globalAlpha = 1;

        // % label on bar
        ctx.font = `9px ${T.font}`;
        ctx.fillStyle = color;
        ctx.fillText(`${(b.wr * 100).toFixed(0)}%`, x + bw / 2 - 10, by - 4);

        // n= label
        ctx.fillStyle = T.label;
        ctx.fillText(`n=${b.n}`, x + bw / 2 - 10, PAD.top + ch + 14);

        // X label
        ctx.fillStyle = T.label;
        ctx.fillText(b.label, x + bw / 2 - 10, PAD.top + ch + 26);
      });

      // Y-axis
      ctx.font = `10px ${T.font}`;
      ctx.fillStyle = T.label;
      [0, 0.25, 0.5, 0.75, 1.0].forEach(v => {
        ctx.fillText(`${(v * 100).toFixed(0)}%`, 2, PAD.top + (1 - v) * ch + 4);
      });
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, [trades]);

  return (
    <div style={styles.chartWrap}>
      <div style={styles.chartTitle}>Win Rate by VPIN Bucket</div>
      <canvas ref={canvasRef} style={{ ...styles.canvas, height: 240 }} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 6: Hourly Performance Heatmap
// ═══════════════════════════════════════════════════════════════════════════════
const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function HeatmapChart({ trades }) {
  const canvasRef = useRef(null);
  const [tooltip, setTooltip] = useState(null);
  const cellsRef = useRef([]);

  const buildGrid = useCallback(() => {
    const grid = {};
    for (let d = 0; d < 7; d++) {
      for (let h = 0; h < 24; h++) {
        grid[`${d}-${h}`] = { wins: 0, total: 0 };
      }
    }
    trades.forEach(t => {
      const key = `${t.dayOfWeek}-${t.hour}`;
      if (grid[key]) {
        grid[key].total++;
        if (t.outcome === 'WIN') grid[key].wins++;
      }
    });
    return grid;
  }, [trades]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const grid = buildGrid();

    const draw = () => {
      const { ctx, w, h } = setupCanvas(canvas);
      if (!ctx) return;

      ctx.fillStyle = T.chartBg;
      ctx.fillRect(0, 0, w, h);

      const PAD = { top: 16, right: 16, bottom: 40, left: 36 };
      const cw = w - PAD.left - PAD.right;
      const ch = h - PAD.top - PAD.bottom;
      const cellW = cw / 24;
      const cellH = ch / 7;
      const cells = [];

      for (let d = 0; d < 7; d++) {
        for (let hh = 0; hh < 24; hh++) {
          const cell = grid[`${d}-${hh}`];
          const wr = cell.total > 0 ? cell.wins / cell.total : null;
          const x = PAD.left + hh * cellW;
          const y = PAD.top + d * cellH;
          const gap = 1;

          let fill = 'rgba(255,255,255,0.03)';
          if (wr !== null) {
            if (wr > 0.55) fill = `rgba(74,222,128,${0.1 + wr * 0.35})`;
            else if (wr < 0.45) fill = `rgba(248,113,113,${0.1 + (1 - wr) * 0.3})`;
            else fill = `rgba(168,85,247,${0.1 + wr * 0.2})`;
          }

          ctx.fillStyle = fill;
          ctx.fillRect(x + gap, y + gap, cellW - gap * 2, cellH - gap * 2);

          cells.push({ x: x + gap, y: y + gap, w: cellW - gap * 2, h: cellH - gap * 2, day: d, hour: hh, wr, n: cell.total });
        }
      }

      cellsRef.current = cells;

      // X-axis: hours
      ctx.font = `9px ${T.font}`;
      ctx.fillStyle = T.label;
      [0, 6, 12, 18, 23].forEach(hh => {
        ctx.fillText(`${hh}h`, PAD.left + hh * cellW + 2, PAD.top + ch + 14);
      });

      // Y-axis: days
      DAY_LABELS.forEach((dl, d) => {
        ctx.fillText(dl, 2, PAD.top + d * cellH + cellH / 2 + 3);
      });

      // Legend
      const legX = PAD.left;
      const legY = PAD.top + ch + 22;
      [
        { color: 'rgba(74,222,128,0.5)', label: '>55% WR' },
        { color: 'rgba(248,113,113,0.4)', label: '<45% WR' },
        { color: 'rgba(168,85,247,0.3)', label: '45-55%' },
        { color: 'rgba(255,255,255,0.05)', label: 'No data' },
      ].forEach(({ color, label }, i) => {
        const lx = legX + i * 90;
        ctx.fillStyle = color;
        ctx.fillRect(lx, legY, 10, 10);
        ctx.fillStyle = T.label;
        ctx.fillText(label, lx + 14, legY + 9);
      });
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, [trades, buildGrid]);

  const handleMouseMove = useCallback((e) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const hit = cellsRef.current.find(c => mx >= c.x && mx <= c.x + c.w && my >= c.y && my <= c.y + c.h);
    if (hit && hit.n > 0) {
      setTooltip({
        x: e.clientX - rect.left + 8,
        y: e.clientY - rect.top - 30,
        day: DAY_LABELS[hit.day],
        hour: hit.hour,
        wr: hit.wr !== null ? (hit.wr * 100).toFixed(1) : '—',
        n: hit.n,
      });
    } else {
      setTooltip(null);
    }
  }, []);

  return (
    <div style={styles.chartWrap}>
      <div style={styles.chartTitle}>Hourly Performance Heatmap — 24h × 7 days</div>
      <div style={{ position: 'relative' }}>
        <canvas
          ref={canvasRef}
          style={{ ...styles.canvas, height: 220 }}
          onMouseMove={handleMouseMove}
          onMouseLeave={() => setTooltip(null)}
        />
        {tooltip && (
          <div style={{
            position: 'absolute',
            left: tooltip.x,
            top: tooltip.y,
            background: 'rgba(0,0,0,0.85)',
            border: `1px solid ${T.border}`,
            borderRadius: 4,
            padding: '6px 10px',
            pointerEvents: 'none',
            fontFamily: T.font,
            fontSize: 11,
            color: T.label2,
            zIndex: 10,
            whiteSpace: 'nowrap',
          }}>
            {tooltip.day} {tooltip.hour}:00 — WR: {tooltip.wr}% (n={tooltip.n})
          </div>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 7: Daily P&L Bars
// ═══════════════════════════════════════════════════════════════════════════════
function DailyPnlChart({ data }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const draw = () => {
      const { ctx, w, h } = setupCanvas(canvas);
      if (!ctx) return;

      ctx.fillStyle = T.chartBg;
      ctx.fillRect(0, 0, w, h);
      drawGrid(ctx, w, h, 10, 4);

      if (!data.length) return;

      const PAD = { top: 24, right: 16, bottom: 28, left: 52 };
      const cw = w - PAD.left - PAD.right;
      const ch = h - PAD.top - PAD.bottom;

      const maxAbs = Math.max(...data.map(Math.abs), 1);
      const barW = cw / data.length;
      const barPad = Math.max(1, barW * 0.1);
      const zeroY = PAD.top + ch / 2;

      // Zero line
      ctx.save();
      ctx.strokeStyle = 'rgba(255,255,255,0.15)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(PAD.left, zeroY);
      ctx.lineTo(PAD.left + cw, zeroY);
      ctx.stroke();
      ctx.restore();

      // Bars
      data.forEach((v, i) => {
        const x = PAD.left + i * barW + barPad;
        const bw = barW - barPad * 2;
        const barH = (Math.abs(v) / maxAbs) * (ch / 2);
        const isUp = v >= 0;
        const by = isUp ? zeroY - barH : zeroY;

        ctx.fillStyle = isUp ? T.profit : T.loss;
        ctx.globalAlpha = 0.75;
        ctx.fillRect(x, by, bw, barH);
        ctx.globalAlpha = 1;
      });

      // Y-axis
      ctx.font = `10px ${T.font}`;
      ctx.fillStyle = T.label;
      [maxAbs, 0, -maxAbs].forEach(v => {
        const y = zeroY - (v / maxAbs) * (ch / 2);
        ctx.fillText(`$${v > 0 ? '+' : ''}${v.toFixed(0)}`, 2, y + 4);
      });

      // X count
      ctx.fillStyle = T.label;
      ctx.fillText(`${data.length}d`, PAD.left + cw - 24, PAD.top - 6);
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, [data]);

  return (
    <div style={styles.chartWrap}>
      <div style={styles.chartTitle}>Daily P&amp;L — Last {data.length} Days</div>
      <canvas ref={canvasRef} style={styles.canvas} />
    </div>
  );
}

// ─── Shared Styles ────────────────────────────────────────────────────────────
const styles = {
  page: {
    background: T.bg,
    minHeight: '100vh',
    fontFamily: T.font,
    color: '#fff',
    padding: '0 0 40px',
  },
  header: {
    background: 'rgba(255,255,255,0.02)',
    borderBottom: `1px solid ${T.border}`,
    padding: '12px 24px',
    display: 'flex',
    alignItems: 'center',
    gap: 24,
    flexWrap: 'wrap',
  },
  headerTitle: {
    fontSize: 13,
    color: T.purple,
    fontWeight: 600,
    letterSpacing: '0.08em',
    marginRight: 8,
  },
  statPill: {
    background: T.card,
    border: `1px solid ${T.border}`,
    borderRadius: 6,
    padding: '5px 12px',
    display: 'flex',
    alignItems: 'center',
    gap: 8,
  },
  statLabel: {
    fontSize: 10,
    color: T.label,
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
  },
  statValue: {
    fontSize: 13,
    color: '#fff',
    fontWeight: 600,
  },
  body: {
    padding: '20px 24px',
    display: 'flex',
    flexDirection: 'column',
    gap: 20,
  },
  sectionTitle: {
    fontSize: 11,
    color: T.purple,
    letterSpacing: '0.12em',
    marginBottom: 12,
    opacity: 0.7,
  },
  grid2: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: 16,
  },
  chartWrap: {
    background: T.card,
    border: `1px solid ${T.border}`,
    borderRadius: 8,
    padding: 16,
    overflow: 'hidden',
  },
  chartTitle: {
    fontSize: 10,
    color: T.label,
    letterSpacing: '0.06em',
    marginBottom: 10,
    textTransform: 'uppercase',
    fontFamily: T.font,
  },
  canvas: {
    width: '100%',
    height: 240,
    display: 'block',
    borderRadius: 4,
  },
};

// ─── Loading state ────────────────────────────────────────────────────────────
function Loader() {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '4px 0' }}>
      <div style={{ width: 6, height: 6, borderRadius: '50%', background: T.purple, animation: 'pulse 1.2s infinite' }} />
      <span style={{ fontSize: 10, color: T.label }}>fetching…</span>
    </div>
  );
}

function StatusDot({ status }) {
  const color = status === 'LIVE' ? T.profit : status === 'STALE' ? T.warning : T.loss;
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
      <span style={{
        width: 7, height: 7, borderRadius: '50%', background: color, display: 'inline-block',
        boxShadow: `0 0 6px ${color}`,
        animation: status === 'LIVE' ? 'pulse 2s infinite' : 'none',
      }} />
      <span style={{ color, fontSize: 12, fontWeight: 600 }}>{status}</span>
    </span>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN DASHBOARD
// ═══════════════════════════════════════════════════════════════════════════════
export default function Dashboard() {
  const api = useApi();

  const [stats, setStats] = useState(null);
  const [vpinData, setVpinData] = useState(null);
  const [cascadeData, setCascadeData] = useState(null);
  const [arbData, setArbData] = useState(null);
  const [equityData, setEquityData] = useState(null);
  const [dailyPnlData, setDailyPnlData] = useState(null);
  const [tradesData, setTradesData] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetchAll = useCallback(async () => {
    try {
      const endpoints = [
        '/api/dashboard/stats',
        '/api/dashboard/vpin-history',
        '/api/dashboard/cascade-state',
        '/api/dashboard/arb-spreads',
        '/api/dashboard/equity',
        '/api/dashboard/daily-pnl',
        '/api/dashboard/trades',
      ];

      const [statsRes, vpinRes, cascadeRes, arbRes, equityRes, pnlRes, tradesRes] =
        await Promise.allSettled(endpoints.map(url => api('GET', url)));

      const get = (res, fallback) => res.status === 'fulfilled' ? (res.value?.data ?? fallback) : fallback;

      const rawStats = get(statsRes, {});
      const rawVpin = get(vpinRes, []);
      const rawCascade = get(cascadeRes, {});
      const rawArb = get(arbRes, []);
      const rawEquity = get(equityRes, []);
      const rawPnl = get(pnlRes, []);
      const rawTrades = get(tradesRes, []);

      setStats(rawStats);
      setVpinData(rawVpin.length ? rawVpin : genVpinDemo());
      setCascadeData(rawCascade.state ? rawCascade : { state: 'IDLE', direction: '—', oi_delta: 0 });
      setArbData(rawArb.length ? rawArb : genArbDemo());
      setEquityData(rawEquity.length ? rawEquity : genEquityDemo());
      setDailyPnlData(rawPnl.length ? rawPnl : genDailyPnlDemo());
      setTradesData(rawTrades.length ? rawTrades : genTradesDemo());
    } catch (err) {
      console.error('Dashboard fetch error:', err);
      // Full fallback
      setVpinData(genVpinDemo());
      setCascadeData({ state: 'IDLE', direction: '—', oi_delta: 0 });
      setArbData(genArbDemo());
      setEquityData(genEquityDemo());
      setDailyPnlData(genDailyPnlDemo());
      setTradesData(genTradesDemo());
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    fetchAll();
    const interval = setInterval(fetchAll, 30000);
    return () => clearInterval(interval);
  }, [fetchAll]);

  const balance = stats?.balance;
  const todayPnl = stats?.today_pnl ?? 0;
  const winRate = stats?.win_rate ?? 0;
  const engineStatus = stats?.engine_status ?? 'OFFLINE';
  const totalTrades = stats?.total_trades ?? 0;
  const walletBalance = stats?.wallet_balance_usdc;
  const paperMode = stats?.paper_mode ?? true;

  return (
    <div style={styles.page}>
      {/* Inject pulse keyframe */}
      <style>{`
        @keyframes pulse { 0%, 100% { opacity: 1 } 50% { opacity: 0.3 } }
        @media (max-width: 768px) { .dash-grid2 { grid-template-columns: 1fr !important; } }
      `}</style>

      {/* Header bar */}
      <div style={styles.header}>
        <span style={styles.headerTitle}>◈ NOVAKASH</span>

        <div style={styles.statPill}>
          <span style={styles.statLabel}>Balance</span>
          <span style={styles.statValue}>
            {balance != null ? `$${parseFloat(balance).toFixed(2)}` : '—'}
          </span>
        </div>

        <div style={styles.statPill}>
          <span style={styles.statLabel}>Poly Wallet</span>
          <span style={{
            ...styles.statValue,
            color: walletBalance != null && walletBalance > 0 ? T.profit : T.label,
          }}>
            {walletBalance != null ? `$${parseFloat(walletBalance).toFixed(2)}` : paperMode ? '📄 Paper' : '—'}
          </span>
        </div>

        <div style={styles.statPill}>
          <span style={styles.statLabel}>Today P&L</span>
          <span style={{
            ...styles.statValue,
            color: todayPnl >= 0 ? T.profit : T.loss,
          }}>
            {todayPnl >= 0 ? '+' : ''}${todayPnl.toFixed(2)}
          </span>
        </div>

        <div style={styles.statPill}>
          <span style={styles.statLabel}>Win Rate</span>
          <span style={{
            ...styles.statValue,
            color: winRate > 0.54 ? T.profit : winRate > 0.5 ? T.warning : T.loss,
          }}>
            {(winRate * 100).toFixed(1)}%
          </span>
        </div>

        <div style={styles.statPill}>
          <span style={styles.statLabel}>Trades</span>
          <span style={styles.statValue}>{totalTrades}</span>
        </div>

        <div style={styles.statPill}>
          <span style={styles.statLabel}>Engine</span>
          <StatusDot status={engineStatus} />
        </div>

        {loading && <Loader />}
      </div>

      {/* Body */}
      <div style={styles.body}>

        {/* § REAL-TIME SIGNALS */}
        <div>
          <div style={styles.sectionTitle}>§ REAL-TIME SIGNALS</div>
          <div className="dash-grid2" style={styles.grid2}>
            {vpinData && <VpinChart data={vpinData} />}
            {cascadeData && <CascadeChart cascadeData={cascadeData} />}
          </div>
        </div>

        {/* Arb + Equity */}
        <div className="dash-grid2" style={styles.grid2}>
          {arbData && <ArbChart data={arbData} />}
          {equityData && <EquityChart data={equityData} />}
        </div>

        {/* § STRATEGY ANALYSIS */}
        <div>
          <div style={styles.sectionTitle}>§ STRATEGY ANALYSIS</div>
          <div className="dash-grid2" style={styles.grid2}>
            {tradesData && <WinRateBucketChart trades={tradesData} />}
            {tradesData && <HeatmapChart trades={tradesData} />}
          </div>
        </div>

        {/* Daily P&L — full width */}
        {dailyPnlData && <DailyPnlChart data={dailyPnlData} />}

      </div>
    </div>
  );
}
