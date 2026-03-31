/**
 * PaperDashboard.jsx — Paper Trading View
 *
 * Same 7 canvas charts as main Dashboard, but:
 * - Shows paper-mode trades only
 * - Uses seeded demo data when no real trades exist
 * - Adjustable thresholds to generate mock trades
 */

import { useEffect, useRef, useState, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';

// ─── Theme (same as Dashboard) ────────────────────────────────────────────────
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

// ─── Google Fonts: IBM Plex Mono ──────────────────────────────────────────────
if (!document.getElementById('ibm-plex-mono-font')) {
  const link = document.createElement('link');
  link.id = 'ibm-plex-mono-font';
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap';
  document.head.appendChild(link);
}

// ─── Seeded PRNG for reproducible demo data ───────────────────────────────────
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

// ─── Demo Data Generators (paper mode mock data) ──────────────────────────────
function genPaperTrades(seed = 42) {
  const rng = seededRng(seed);
  const strategies = ['sub_dollar_arb', 'vpin_cascade'];
  const outcomes = ['WIN', 'LOSS', 'WIN', 'WIN', 'LOSS', 'WIN', 'LOSS', 'WIN'];
  
  return Array.from({ length: 50 }, (_, i) => {
    const strategy = strategies[Math.floor(rng() * strategies.length)];
    const isArb = strategy === 'sub_dollar_arb';
    // Arb has higher win rate (~80%), cascade ~55%
    const winRate = isArb ? 0.80 : 0.55;
    const outcome = rng() < winRate ? 'WIN' : 'LOSS';
    const stake = isArb ? 20 + rng() * 30 : 15 + rng() * 35;
    const pnl = outcome === 'WIN' 
      ? stake * (0.03 + rng() * 0.05)  // Arb: 3-8% profit
      : (rng() - 0.45) * stake;  // Cascade: variable PnL
    
    return {
      id: i,
      strategy,
      outcome,
      pnl_usd: parseFloat(pnl.toFixed(2)),
      stake_usd: parseFloat(stake.toFixed(2)),
      vpin: 0.25 + rng() * 0.60,
      hour: Math.floor(rng() * 24),
      dayOfWeek: Math.floor(rng() * 7),
      market_slug: `BTC-${Math.floor(rng() * 1000)}`,
      entry_price: 0.40 + rng() * 0.50,
      created_at: new Date(Date.now() - (50 - i) * 3600000).toISOString(),
    };
  });
}

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
  // Simulate sub-$1 arb opportunities (0.92-0.98 range = profitable)
  return Array.from({ length: 120 }, () => 0.92 + rng() * 0.06);
}

function genEquityDemo() {
  const rng = seededRng(13);
  let balance = 1000;
  return Array.from({ length: 60 }, (_, i) => {
    // Paper mode: slightly positive drift
    balance += (rng() - 0.40) * 45;
    return { day: `Day ${i + 1}`, balance: Math.max(600, balance) };
  });
}

function genDailyPnlDemo() {
  const rng = seededRng(55);
  return Array.from({ length: 60 }, () => (rng() - 0.38) * 80);
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
    const { ctx, w, h } = setupCanvas(canvas);
    if (!ctx) return;

    drawGrid(ctx, w, h);

    const points = data || genVpinDemo();
    const maxVpin = 1.0;
    const minVpin = 0.0;

    // Draw VPIN line
    ctx.save();
    ctx.strokeStyle = T.cyan;
    ctx.lineWidth = 2;
    ctx.beginPath();

    points.forEach((p, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h - ((p.vpin - minVpin) / (maxVpin - minVpin)) * h;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });

    ctx.stroke();

    // Draw threshold lines
    ctx.strokeStyle = 'rgba(245,158,11,0.5)';
    ctx.setLineDash([5, 5]);
    ctx.lineWidth = 1;
    
    // VPIN cascade threshold (0.70)
    const cascadeY = h - 0.70 * h;
    ctx.beginPath();
    ctx.moveTo(0, cascadeY);
    ctx.lineTo(w, cascadeY);
    ctx.stroke();

    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(245,158,11,0.8)';
    ctx.font = '10px IBM Plex Mono';
    ctx.fillText('CASCADE → 0.70', w - 80, cascadeY - 5);

    ctx.restore();
  }, [data]);

  return (
    <div style={{ background: T.card, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ color: T.cyan, fontFamily: T.font, fontSize: 12, fontWeight: 600 }}>
          📡 VPIN
        </span>
        <span style={{ color: T.label, fontFamily: T.font, fontSize: 10 }}>
          Real-time informed flow
        </span>
      </div>
      <canvas ref={canvasRef} style={{ width: '100%', height: 180, display: 'block' }} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 2: Equity Curve with Drawdown
// ═══════════════════════════════════════════════════════════════════════════════
function EquityCurve({ trades }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const { ctx, w, h } = setupCanvas(canvas);
    if (!ctx) return;

    drawGrid(ctx, w, h, 8, 5);

    // Calculate equity curve from trades
    const dailyPnL = {};
    (trades || genPaperTrades()).forEach(t => {
      const day = t.created_at ? t.created_at.slice(0, 10) : 'Day 1';
      dailyPnL[day] = (dailyPnL[day] || 0) + t.pnl_usd;
    });

    let balance = 1000;
    let peak = 1000;
    const equityData = Object.keys(dailyPnL).map(day => {
      balance += dailyPnL[day];
      peak = Math.max(peak, balance);
      return { day, balance, drawdown: (peak - balance) / peak };
    });

    if (equityData.length === 0) {
      equityData.push(...genEquityDemo());
    }

    const maxBalance = Math.max(...equityData.map(d => d.balance));
    const minBalance = Math.min(...equityData.map(d => d.balance));
    const range = maxBalance - minBalance || 1;

    // Draw drawdown underlay
    ctx.save();
    ctx.fillStyle = 'rgba(248,113,113,0.15)';
    let prevX = 0;
    let prevDDY = h;

    equityData.forEach((d, i) => {
      const x = (i / (equityData.length - 1)) * w;
      const ddY = h - (d.drawdown / 0.5) * h; // 50% max drawdown scale
      
      if (i === 0) {
        ctx.beginPath();
        ctx.moveTo(x, h);
        ctx.lineTo(x, ddY);
      } else {
        ctx.lineTo(x, ddY);
      }
      prevX = x;
      prevDDY = ddY;
    });

    ctx.lineTo(prevX, h);
    ctx.closePath();
    ctx.fill();
    ctx.restore();

    // Draw equity line
    ctx.save();
    ctx.strokeStyle = T.profit;
    ctx.lineWidth = 2;
    ctx.beginPath();

    equityData.forEach((d, i) => {
      const x = (i / (equityData.length - 1)) * w;
      const y = h - ((d.balance - minBalance) / range) * h;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });

    ctx.stroke();

    // Current balance label
    const last = equityData[equityData.length - 1];
    ctx.fillStyle = T.profit;
    ctx.font = '11px IBM Plex Mono';
    ctx.fillText(`$${last.balance.toFixed(0)}`, w - 60, 20);

    ctx.restore();
  }, [trades]);

  return (
    <div style={{ background: T.card, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ color: T.profit, fontFamily: T.font, fontSize: 12, fontWeight: 600 }}>
          📈 Equity
        </span>
        <span style={{ color: T.label, fontFamily: T.font, fontSize: 10 }}>
          Paper bankroll trajectory
        </span>
      </div>
      <canvas ref={canvasRef} style={{ width: '100%', height: 180, display: 'block' }} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 3: Daily P&L Bars
// ═══════════════════════════════════════════════════════════════════════════════
function DailyPnlBars({ trades }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const { ctx, w, h } = setupCanvas(canvas);
    if (!ctx) return;

    drawGrid(ctx, w, h, 10, 5);

    // Aggregate daily PnL
    const dailyPnL = {};
    (trades || genPaperTrades()).forEach(t => {
      const day = t.created_at ? t.created_at.slice(0, 10) : 'Day 1';
      dailyPnL[day] = (dailyPnL[day] || 0) + t.pnl_usd;
    });

    const days = Object.keys(dailyPnL).slice(-30); // Last 30 days
    const values = days.map(d => dailyPnL[d]);
    const maxPnL = Math.max(...values.map(Math.abs)) || 100;

    const barWidth = (w - 40) / 30;

    ctx.save();
    days.forEach((day, i) => {
      const x = 20 + i * barWidth;
      const value = values[i];
      const barHeight = (Math.abs(value) / maxPnL) * (h - 30);
      const y = value >= 0 ? h - barHeight - 20 : h - 20;

      ctx.fillStyle = value >= 0 ? T.profit : T.loss;
      ctx.fillRect(x + 2, y, barWidth - 4, barHeight);
    });

    // Zero line
    ctx.strokeStyle = 'rgba(255,255,255,0.2)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(20, h - 20);
    ctx.lineTo(w - 20, h - 20);
    ctx.stroke();

    ctx.restore();
  }, [trades]);

  return (
    <div style={{ background: T.card, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ color: T.cyan, fontFamily: T.font, fontSize: 12, fontWeight: 600 }}>
          📊 Daily P&L
        </span>
        <span style={{ color: T.label, fontFamily: T.font, fontSize: 10 }}>
          Last 30 days (paper)
        </span>
      </div>
      <canvas ref={canvasRef} style={{ width: '100%', height: 150, display: 'block' }} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 4: Arb Spread Monitor
// ═══════════════════════════════════════════════════════════════════════════════
function ArbMonitor({ data }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const { ctx, w, h } = setupCanvas(canvas);
    if (!ctx) return;

    drawGrid(ctx, w, h, 8, 5);

    const spreadData = data || genArbDemo();

    // Draw 0.95 threshold line (breakeven after fees)
    ctx.save();
    ctx.strokeStyle = 'rgba(245,158,11,0.6)';
    ctx.setLineDash([5, 5]);
    ctx.lineWidth = 2;
    const thresholdY = h - 0.95 * h;
    ctx.beginPath();
    ctx.moveTo(0, thresholdY);
    ctx.lineTo(w, thresholdY);
    ctx.stroke();

    ctx.fillStyle = 'rgba(245,158,11,0.8)';
    ctx.font = '10px IBM Plex Mono';
    ctx.fillText('BREAKEVEN → 0.95', w - 70, thresholdY - 5);

    // Draw spread line
    ctx.setLineDash([]);
    ctx.strokeStyle = T.cyan;
    ctx.lineWidth = 2;
    ctx.beginPath();

    spreadData.forEach((spread, i) => {
      const x = (i / (spreadData.length - 1)) * w;
      const y = h - spread * h;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });

    ctx.stroke();

    // Mark profitable zones (below 0.95)
    ctx.fillStyle = 'rgba(6,182,212,0.1)';
    ctx.fillRect(0, thresholdY, w, h - thresholdY);

    ctx.restore();
  }, [data]);

  return (
    <div style={{ background: T.card, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ color: T.cyan, fontFamily: T.font, fontSize: 12, fontWeight: 600 }}>
          ⚡ Arb Spread
        </span>
        <span style={{ color: T.label, fontFamily: T.font, fontSize: 10 }}>
          Sub-$1 opportunities
        </span>
      </div>
      <canvas ref={canvasRef} style={{ width: '100%', height: 180, display: 'block' }} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 5: Win Rate by VPIN Bucket
// ═══════════════════════════════════════════════════════════════════════════════
function WinRateByVpin({ trades }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const { ctx, w, h } = setupCanvas(canvas);
    if (!ctx) return;

    drawGrid(ctx, w, h, 6, 5);

    const tradeData = trades || genPaperTrades();

    // Bucket by VPIN
    const buckets = {
      '0.0-0.3': { wins: 0, total: 0 },
      '0.3-0.5': { wins: 0, total: 0 },
      '0.5-0.7': { wins: 0, total: 0 },
      '0.7-0.9': { wins: 0, total: 0 },
      '0.9-1.0': { wins: 0, total: 0 },
    };

    tradeData.forEach(t => {
      let bucket;
      if (t.vpin < 0.3) bucket = '0.0-0.3';
      else if (t.vpin < 0.5) bucket = '0.3-0.5';
      else if (t.vpin < 0.7) bucket = '0.5-0.7';
      else if (t.vpin < 0.9) bucket = '0.7-0.9';
      else bucket = '0.9-1.0';

      buckets[bucket].total++;
      if (t.outcome === 'WIN') buckets[bucket].wins++;
    });

    const barWidth = (w - 40) / 5;

    ctx.save();
    Object.keys(buckets).forEach((key, i) => {
      const bucket = buckets[key];
      const winRate = bucket.total > 0 ? bucket.wins / bucket.total : 0;
      const x = 20 + i * barWidth;
      const barHeight = winRate * (h - 30);

      // Bar
      ctx.fillStyle = T.profit;
      ctx.fillRect(x + 4, h - barHeight - 20, barWidth - 8, barHeight);

      // Label
      ctx.fillStyle = T.label;
      ctx.font = '9px IBM Plex Mono';
      ctx.fillText(key, x + 8, h - 5);

      // Win rate %
      ctx.fillStyle = '#fff';
      ctx.font = '10px IBM Plex Mono';
      ctx.fillText(`${(winRate * 100).toFixed(0)}%`, x + 10, h - barHeight - 25);
    });

    // Y-axis label
    ctx.fillStyle = T.label;
    ctx.font = '10px IBM Plex Mono';
    ctx.save();
    ctx.translate(10, h / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText('Win Rate', 0, 0);
    ctx.restore();

    ctx.restore();
  }, [trades]);

  return (
    <div style={{ background: T.card, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ color: T.warning, fontFamily: T.font, fontSize: 12, fontWeight: 600 }}>
          📊 Win Rate by VPIN
        </span>
        <span style={{ color: T.label, fontFamily: T.font, fontSize: 10 }}>
          Strategy performance
        </span>
      </div>
      <canvas ref={canvasRef} style={{ width: '100%', height: 180, display: 'block' }} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 6: Cascade State Machine (SVG)
// ═══════════════════════════════════════════════════════════════════════════════
function CascadeState({ vpin }) {
  const [state, setState] = useState('IDLE');

  useEffect(() => {
    // Simple FSM based on current VPIN
    const v = vpin || 0.4;
    if (v < 0.55) setState('IDLE');
    else if (v < 0.70) setState('INFORMED');
    else if (v < 0.80) setState('CASCADE_DETECTED');
    else if (v < 0.90) setState('EXHAUSTING');
    else setState('BET_SIGNAL');
  }, [vpin]);

  const stateColors = {
    IDLE: 'rgba(255,255,255,0.1)',
    INFORMED: 'rgba(245,158,11,0.2)',
    CASCADE_DETECTED: 'rgba(248,113,113,0.2)',
    EXHAUSTING: 'rgba(251,146,60,0.2)',
    BET_SIGNAL: 'rgba(168,85,247,0.2)',
  };

  const stateLabels = {
    IDLE: '🔵 IDLE',
    INFORMED: '🟠 INFORMED',
    CASCADE_DETECTED: '🔴 CASCADE',
    EXHAUSTING: '🟡 EXHAUSTING',
    BET_SIGNAL: '🟣 BET',
  };

  return (
    <div style={{ 
      background: T.card, 
      border: `1px solid ${T.border}`, 
      borderRadius: 8, 
      padding: 12,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ color: T.purple, fontFamily: T.font, fontSize: 12, fontWeight: 600 }}>
          🌊 Cascade
        </span>
        <span style={{ color: T.label, fontFamily: T.font, fontSize: 10 }}>
          FSM state
        </span>
      </div>
      <div style={{
        background: stateColors[state],
        border: `1px solid ${stateColors[state].replace('0.2', '0.4')}`,
        borderRadius: 6,
        padding: '15px 20px',
        textAlign: 'center',
      }}>
        <div style={{ 
          color: '#fff', 
          fontFamily: T.font, 
          fontSize: 18, 
          fontWeight: 700,
          letterSpacing: '0.05em',
        }}>
          {stateLabels[state]}
        </div>
        <div style={{ 
          color: T.label2, 
          fontFamily: T.font, 
          fontSize: 11, 
          marginTop: 5,
        }}>
          VPIN: {(vpin || 0).toFixed(3)}
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART 7: Trade History Table
// ═══════════════════════════════════════════════════════════════════════════════
function TradeHistory({ trades }) {
  const tradeData = trades || genPaperTrades();

  return (
    <div style={{ background: T.card, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
        <span style={{ color: T.cyan, fontFamily: T.font, fontSize: 12, fontWeight: 600 }}>
          📋 Recent Trades
        </span>
        <span style={{ color: T.label, fontFamily: T.font, fontSize: 10 }}>
          {tradeData.length} paper trades
        </span>
      </div>
      <div style={{ maxHeight: 200, overflowY: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${T.border}` }}>
              <th style={{ textAlign: 'left', padding: '6px 4px', color: T.label }}>Strat</th>
              <th style={{ textAlign: 'left', padding: '6px 4px', color: T.label }}>Outcome</th>
              <th style={{ textAlign: 'right', padding: '6px 4px', color: T.label }}>PnL</th>
              <th style={{ textAlign: 'right', padding: '6px 4px', color: T.label }}>VPIN</th>
            </tr>
          </thead>
          <tbody>
            {tradeData.slice(0, 15).map(t => (
              <tr key={t.id} style={{ borderBottom: `1px solid ${T.border}` }}>
                <td style={{ padding: '6px 4px', color: t.strategy === 'sub_dollar_arb' ? T.cyan : T.purple, fontFamily: T.font, fontSize: 10 }}>
                  {t.strategy === 'sub_dollar_arb' ? '⚡ Arb' : '🌊 Cascade'}
                </td>
                <td style={{ padding: '6px 4px' }}>
                  <span style={{
                    color: t.outcome === 'WIN' ? T.profit : T.loss,
                    fontFamily: T.font,
                    fontSize: 10,
                    fontWeight: 600,
                  }}>
                    {t.outcome}
                  </span>
                </td>
                <td style={{ 
                  padding: '6px 4px', 
                  textAlign: 'right', 
                  color: t.pnl_usd >= 0 ? T.profit : T.loss,
                  fontFamily: T.font,
                  fontSize: 10,
                }}>
                  {t.pnl_usd >= 0 ? '+' : ''}${t.pnl_usd.toFixed(2)}
                </td>
                <td style={{ 
                  padding: '6px 4px', 
                  textAlign: 'right', 
                  color: vpinColour(t.vpin),
                  fontFamily: T.font,
                  fontSize: 10,
                }}>
                  {t.vpin.toFixed(3)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// Main Paper Dashboard Page
// ═══════════════════════════════════════════════════════════════════════════════
export default function PaperDashboard() {
  const { data: tradesData } = useApi('/api/trades?mode=paper&limit=100');
  const [vpin, setVpin] = useState(0.45);
  const [arbSpreads, setArbSpreads] = useState([]);

  // Simulate real-time updates
  useEffect(() => {
    const vpinInterval = setInterval(() => {
      setVpin(v => {
        const delta = (Math.random() - 0.5) * 0.08;
        return Math.min(0.95, Math.max(0.1, v + delta));
      });
    }, 2000);

    const arbInterval = setInterval(() => {
      setArbSpreads(prev => {
        const newSpread = 0.92 + Math.random() * 0.06;
        const updated = [...prev, newSpread].slice(-120);
        return updated;
      });
    }, 3000);

    return () => {
      clearInterval(vpinInterval);
      clearInterval(arbInterval);
    };
  }, []);

  const trades = tradesData?.data || genPaperTrades();

  return (
    <div style={{ 
      background: T.bg,
      minHeight: '100vh',
      padding: '20px 16px',
      fontFamily: T.font,
    }}>
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <div style={{ marginBottom: 24 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
          <span style={{ 
            display: 'inline-flex', 
            alignItems: 'center', 
            gap: 4,
            padding: '3px 8px',
            borderRadius: 4,
            border: '1px solid rgba(168,85,247,0.3)',
            background: 'rgba(168,85,247,0.1)',
            color: '#a855f7',
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: '0.06em',
            fontFamily: T.font,
          }}>
            📄 PAPER
          </span>
          <h1 style={{
            color: 'rgba(255,255,255,0.9)',
            fontFamily: T.font,
            fontSize: 20, 
            fontWeight: 700, 
            letterSpacing: '-0.01em',
            margin: 0,
          }}>
            Paper Trading Dashboard
          </h1>
        </div>
        <div style={{ color: T.label, fontSize: 12 }}>
          Simulated trades · Adjust thresholds in Trading Config to see more activity
        </div>
      </div>

      {/* ── Summary Stats ───────────────────────────────────────────────────── */}
      <div style={{ 
        display: 'grid', 
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', 
        gap: 12, 
        marginBottom: 20,
      }}>
        {(() => {
          const totalPnL = trades.reduce((sum, t) => sum + t.pnl_usd, 0);
          const wins = trades.filter(t => t.outcome === 'WIN').length;
          const winRate = trades.length > 0 ? (wins / trades.length) * 100 : 0;
          const arbTrades = trades.filter(t => t.strategy === 'sub_dollar_arb').length;
          
          return (
            <>
              <div style={{ 
                background: T.card, 
                border: `1px solid ${T.border}`, 
                borderRadius: 8, 
                padding: 14,
              }}>
                <div style={{ color: T.label, fontSize: 10, marginBottom: 4 }}>Total P&L</div>
                <div style={{ 
                  color: totalPnL >= 0 ? T.profit : T.loss, 
                  fontSize: 20, 
                  fontWeight: 700,
                  fontFamily: T.font,
                }}>
                  {totalPnL >= 0 ? '+' : ''}${totalPnL.toFixed(2)}
                </div>
              </div>
              <div style={{ 
                background: T.card, 
                border: `1px solid ${T.border}`, 
                borderRadius: 8, 
                padding: 14,
              }}>
                <div style={{ color: T.label, fontSize: 10, marginBottom: 4 }}>Win Rate</div>
                <div style={{ 
                  color: T.profit, 
                  fontSize: 20, 
                  fontWeight: 700,
                  fontFamily: T.font,
                }}>
                  {winRate.toFixed(1)}%
                </div>
              </div>
              <div style={{ 
                background: T.card, 
                border: `1px solid ${T.border}`, 
                borderRadius: 8, 
                padding: 14,
              }}>
                <div style={{ color: T.label, fontSize: 10, marginBottom: 4 }}>Total Trades</div>
                <div style={{ 
                  color: '#fff', 
                  fontSize: 20, 
                  fontWeight: 700,
                  fontFamily: T.font,
                }}>
                  {trades.length}
                </div>
              </div>
              <div style={{ 
                background: T.card, 
                border: `1px solid ${T.border}`, 
                borderRadius: 8, 
                padding: 14,
              }}>
                <div style={{ color: T.label, fontSize: 10, marginBottom: 4 }}>Arb Trades</div>
                <div style={{ 
                  color: T.cyan, 
                  fontSize: 20, 
                  fontWeight: 700,
                  fontFamily: T.font,
                }}>
                  {arbTrades}
                </div>
              </div>
            </>
          );
        })()}
      </div>

      {/* ── Charts Grid ─────────────────────────────────────────────────────── */}
      <div style={{ 
        display: 'grid', 
        gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', 
        gap: 16,
        marginBottom: 20,
      }}>
        <VpinChart data={genVpinDemo()} />
        <CascadeState vpin={vpin} />
      </div>

      <div style={{ 
        display: 'grid', 
        gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', 
        gap: 16,
        marginBottom: 20,
      }}>
        <EquityCurve trades={trades} />
        <ArbMonitor data={arbSpreads.length > 0 ? arbSpreads : genArbDemo()} />
      </div>

      <div style={{ 
        display: 'grid', 
        gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', 
        gap: 16,
        marginBottom: 20,
      }}>
        <DailyPnlBars trades={trades} />
        <WinRateByVpin trades={trades} />
      </div>

      <TradeHistory trades={trades} />
    </div>
  );
}
