import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';

// ─── Design Tokens ────────────────────────────────────────────────────────────
const C = {
  bg: '#07070c',
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  purple: '#a855f7',
  cyan: '#06b6d4',
  green: '#4ade80',
  red: '#f87171',
  amber: '#f59e0b',
  text: 'rgba(255,255,255,0.85)',
  muted: 'rgba(255,255,255,0.4)',
  faint: 'rgba(255,255,255,0.15)',
};

const MONO = "'IBM Plex Mono', monospace";

// ─── HiDPI Canvas Setup ───────────────────────────────────────────────────────
function setupCanvas(canvas) {
  if (!canvas) return null;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  return { ctx, w: rect.width, h: rect.height };
}

// ─── TOC Sections ─────────────────────────────────────────────────────────────
const TOC_SECTIONS = [
  { id: 'overview',       label: 'Overview',                symbol: '§' },
  { id: 'arb',            label: 'Strategy 1: Sub-$1 Arb',  symbol: '§' },
  { id: 'vpin-cascade',   label: 'Strategy 2: VPIN Cascade', symbol: '§' },
  { id: 'vpin-explained', label: 'VPIN Explained',           symbol: '§' },
  { id: 'cascade-fsm',    label: 'Cascade State Machine',    symbol: '§' },
  { id: 'risk',           label: 'Risk Management',          symbol: '§' },
  { id: 'thresholds',     label: 'Thresholds & Config',      symbol: '§' },
  { id: 'venues',         label: 'Markets & Venues',         symbol: '§' },
  { id: 'fees',           label: 'Fee Structure',            symbol: '§' },
  { id: 'paper-vs-live',  label: 'Paper vs Live',           symbol: '§' },
];

// ─── Config Variables Table ───────────────────────────────────────────────────
const CONFIG_VARS = [
  { name: 'starting_bankroll',        default: 25,     range: [1, 10000],   unit: '$',  desc: 'Initial capital for sizing', impact: 'Base for all position sizing math' },
  { name: 'bet_fraction',             default: 0.05,   range: [0.01, 0.2],  unit: '%',  desc: 'Fraction of bankroll per trade', impact: 'Higher → bigger bets, more variance' },
  { name: 'max_drawdown_pct',         default: 0.10,   range: [0.05, 0.5],  unit: '%',  desc: 'Kill switch trigger threshold', impact: 'Lower → tighter kill switch' },
  { name: 'daily_loss_limit_pct',     default: 0.05,   range: [0.01, 0.2],  unit: '%',  desc: 'Max loss per day before pause', impact: 'Lower → pauses trading sooner' },
  { name: 'consecutive_loss_limit',   default: 3,      range: [1, 10],      unit: '#',  desc: 'Losses before cooldown', impact: 'Lower → more frequent cooldowns' },
  { name: 'cooldown_minutes',         default: 15,     range: [1, 120],     unit: 'min', desc: 'Cooldown after streak losses', impact: 'Higher → longer pause after losses' },
  { name: 'vpin_informed_threshold',  default: 0.55,   range: [0.4, 0.9],   unit: '',   desc: 'VPIN level for "informed flow"', impact: 'Lower → more sensitive, more signals' },
  { name: 'vpin_cascade_threshold',   default: 0.70,   range: [0.5, 0.95],  unit: '',   desc: 'VPIN level for cascade detection', impact: 'Lower → triggers on smaller events' },
  { name: 'vpin_window',              default: 50,     range: [10, 200],    unit: '#',  desc: 'Buckets for VPIN rolling mean', impact: 'Lower → noisier, faster VPIN' },
  { name: 'bucket_size_usd',          default: 50000,  range: [10000, 500000], unit: '$', desc: 'USD volume per VPIN bucket', impact: 'Smaller → more buckets, noisier signal' },
  { name: 'arb_min_spread',           default: 0.015,  range: [0.005, 0.05], unit: '%', desc: 'Min YES+NO gap to trigger arb', impact: 'Lower → more arbs but smaller edge' },
  { name: 'arb_fee_multiplier',       default: 0.072,  range: [0.02, 0.1],  unit: '',   desc: 'Fee factor for arb P&L calc', impact: 'Higher → more conservative filter' },
  { name: 'opinion_fee_multiplier',   default: 0.04,   range: [0.01, 0.08], unit: '',   desc: 'Opinion Exchange fee factor', impact: 'Affects net profit calculation' },
  { name: 'cascade_decel_threshold',  default: 0.30,   range: [0.1, 0.7],   unit: '',   desc: 'Liq rate drop % = "exhausted"', impact: 'Lower → wait longer for full exhaust' },
  { name: 'cascade_min_liq_usd',      default: 1000000, range: [100000, 10000000], unit: '$', desc: 'Min cascade size to care about', impact: 'Higher → only big events trigger' },
  { name: 'bet_duration_hours',       default: 24,     range: [1, 168],     unit: 'hr', desc: 'How long to hold cascade bet', impact: 'Higher → rides longer bounces' },
  { name: 'max_open_positions',       default: 3,      range: [1, 10],      unit: '#',  desc: 'Max simultaneous open trades', impact: 'Higher → more diversification' },
  { name: 'scan_interval_ms',         default: 500,    range: [100, 5000],  unit: 'ms', desc: 'How often to scan for arbs', impact: 'Lower → catches more arbs but CPU cost' },
  { name: 'polymarket_fee_mult',      default: 0.072,  range: [0.02, 0.1],  unit: '',   desc: 'Polymarket-specific fee multiplier', impact: 'Must match actual exchange fees' },
];

// ─── Utility ──────────────────────────────────────────────────────────────────
function SectionAnchor({ id }) {
  return <div id={id} style={{ scrollMarginTop: 80 }} />;
}

function SectionHeader({ children, color = C.purple }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        fontFamily: MONO, fontSize: 11, color: C.muted,
        letterSpacing: '0.12em', marginBottom: 8, textTransform: 'uppercase',
      }}>
        <div style={{ width: 24, height: 1, background: color, opacity: 0.6 }} />
        SECTION
      </div>
      <h2 style={{
        margin: 0, fontFamily: MONO, fontSize: 22,
        fontWeight: 700, color: C.text, letterSpacing: '-0.02em',
      }}>
        {children}
      </h2>
      <div style={{ height: 2, width: 40, background: color, marginTop: 10, borderRadius: 1,
        boxShadow: `0 0 8px ${color}66` }} />
    </div>
  );
}

function Card({ children, style = {}, glow }) {
  return (
    <div style={{
      background: C.card,
      border: `1px solid ${C.border}`,
      borderRadius: 12,
      padding: '20px 24px',
      position: 'relative',
      ...(glow ? { boxShadow: `0 0 24px ${glow}18` } : {}),
      ...style,
    }}>
      {children}
    </div>
  );
}

function MathBlock({ children }) {
  return (
    <pre style={{
      background: 'rgba(0,0,0,0.4)',
      border: `1px solid ${C.border}`,
      borderRadius: 8,
      padding: '16px 20px',
      fontFamily: MONO,
      fontSize: 12,
      color: C.cyan,
      overflowX: 'auto',
      lineHeight: 1.8,
      margin: '16px 0',
    }}>
      {children}
    </pre>
  );
}

function Badge({ children, color = C.purple }) {
  return (
    <span style={{
      display: 'inline-block',
      padding: '3px 9px',
      borderRadius: 4,
      border: `1px solid ${color}44`,
      background: `${color}18`,
      color,
      fontFamily: MONO,
      fontSize: 10,
      fontWeight: 700,
      letterSpacing: '0.08em',
    }}>
      {children}
    </span>
  );
}

function InsightBox({ children, color = C.amber }) {
  return (
    <div style={{
      background: `${color}0e`,
      border: `1px solid ${color}33`,
      borderLeft: `3px solid ${color}`,
      borderRadius: 8,
      padding: '14px 18px',
      fontFamily: MONO,
      fontSize: 12,
      color: 'rgba(255,255,255,0.7)',
      lineHeight: 1.7,
      margin: '16px 0',
    }}>
      <span style={{ color, fontWeight: 700, marginRight: 8 }}>⚡</span>
      {children}
    </div>
  );
}

// ─── Canvas: Arb Diagram ──────────────────────────────────────────────────────
function ArbDiagram({ yes, no, label }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;

    ctx.clearRect(0, 0, w, h);

    const sum = yes + no;
    const isArb = sum < 1.0;
    const spread = (1.0 - sum).toFixed(3);

    // YES box
    const bw = 110, bh = 56, gap = 20;
    const startX = w / 2 - bw - gap / 2;
    const y = h / 2 - bh / 2;

    // YES box
    ctx.strokeStyle = C.cyan + '99';
    ctx.fillStyle = `${C.cyan}18`;
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.roundRect(startX, y, bw, bh, 8); ctx.fill(); ctx.stroke();
    ctx.fillStyle = C.cyan;
    ctx.font = `700 11px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.fillText('YES', startX + bw / 2, y + 20);
    ctx.font = `700 18px ${MONO}`;
    ctx.fillStyle = C.text;
    ctx.fillText(`$${yes.toFixed(2)}`, startX + bw / 2, y + 42);

    // NO box
    const noX = startX + bw + gap;
    ctx.strokeStyle = C.purple + '99';
    ctx.fillStyle = `${C.purple}18`;
    ctx.beginPath(); ctx.roundRect(noX, y, bw, bh, 8); ctx.fill(); ctx.stroke();
    ctx.fillStyle = C.purple;
    ctx.font = `700 11px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.fillText('NO', noX + bw / 2, y + 20);
    ctx.font = `700 18px ${MONO}`;
    ctx.fillStyle = C.text;
    ctx.fillText(`$${no.toFixed(2)}`, noX + bw / 2, y + 42);

    // Sum = ?
    const sumX = noX + bw + 20;
    ctx.fillStyle = C.muted;
    ctx.font = `400 11px ${MONO}`;
    ctx.textAlign = 'left';
    ctx.fillText('+', startX + bw + gap / 2 - 4, h / 2 + 5);
    ctx.fillText('=', sumX, h / 2 + 5);

    const sumColor = isArb ? C.green : C.amber;
    ctx.fillStyle = sumColor;
    ctx.font = `700 20px ${MONO}`;
    ctx.fillText(`$${sum.toFixed(2)}`, sumX + 16, h / 2 + 5);

    if (isArb) {
      // Profit zone highlight
      const profitX = sumX + 16 + 70;
      ctx.fillStyle = `${C.green}22`;
      ctx.strokeStyle = `${C.green}55`;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.roundRect(profitX, h / 2 - 16, 90, 28, 6); ctx.fill(); ctx.stroke();
      ctx.fillStyle = C.green;
      ctx.font = `700 12px ${MONO}`;
      ctx.textAlign = 'center';
      ctx.fillText(`+$${spread}`, profitX + 45, h / 2 + 5);
      ctx.font = `400 9px ${MONO}`;
      ctx.fillStyle = `${C.green}99`;
      ctx.fillText('SPREAD', profitX + 45, h / 2 + 16);
    }

    // Label
    if (label) {
      ctx.fillStyle = C.muted;
      ctx.font = `400 10px ${MONO}`;
      ctx.textAlign = 'left';
      ctx.fillText(label, 12, h - 10);
    }
  }, [yes, no, label]);

  return <canvas ref={canvasRef} style={{ width: '100%', height: 80, display: 'block' }} />;
}

// ─── Canvas: Arb Flow Diagram ─────────────────────────────────────────────────
function ArbFlowDiagram() {
  const canvasRef = useRef(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    ctx.clearRect(0, 0, w, h);

    const boxes = [
      { x: 20,       label: 'BUY YES\n$0.60', color: C.cyan },
      { x: 145,      label: 'BUY NO\n$0.38',  color: C.purple },
      { x: 270,      label: 'COST\n$0.98',    color: C.amber },
      { x: 390,      label: 'PAYOUT\n$1.00',  color: C.green },
      { x: 510,      label: 'PROFIT\n$0.02',  color: C.green },
    ];

    const bw = 100, bh = 54, y = h / 2 - bh / 2;

    boxes.forEach((b, i) => {
      const bx = Math.min(b.x, w - bw - 10);
      ctx.fillStyle = `${b.color}18`;
      ctx.strokeStyle = `${b.color}66`;
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.roundRect(bx, y, bw, bh, 8); ctx.fill(); ctx.stroke();

      const lines = b.label.split('\n');
      ctx.fillStyle = b.color;
      ctx.font = `600 11px ${MONO}`;
      ctx.textAlign = 'center';
      ctx.fillText(lines[0], bx + bw / 2, y + 22);
      ctx.fillStyle = C.text;
      ctx.font = `700 16px ${MONO}`;
      ctx.fillText(lines[1], bx + bw / 2, y + 42);

      if (i < boxes.length - 1) {
        const ax = bx + bw + 5;
        const ay = h / 2;
        ctx.strokeStyle = C.muted;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(ax + 12, ay);
        ctx.stroke();
        ctx.fillStyle = C.muted;
        ctx.beginPath();
        ctx.moveTo(ax + 12, ay - 4);
        ctx.lineTo(ax + 18, ay);
        ctx.lineTo(ax + 12, ay + 4);
        ctx.fill();
      }
    });

    // Plus sign between YES and NO
    ctx.fillStyle = C.muted;
    ctx.font = `400 16px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.fillText('+', 135, h / 2 + 5);
    ctx.fillText('=', 258, h / 2 + 5);
    ctx.fillText('→', 378, h / 2 + 5);

  }, []);
  return <canvas ref={canvasRef} style={{ width: '100%', height: 90, display: 'block' }} />;
}

// ─── Interactive Arb Calculator ───────────────────────────────────────────────
function ArbCalculator() {
  const [yes, setYes] = useState(0.60);
  const [no, setNo] = useState(0.38);
  const canvasRef = useRef(null);

  const polyFee = 0.072;
  const opinionFee = 0.04;

  const calcNet = useCallback((yesP, noP, feeMult) => {
    const spread = 1.0 - (yesP + noP);
    const feeYes = feeMult * yesP * (1 - yesP);
    const feeNo = feeMult * noP * (1 - noP);
    return { spread, feeYes, feeNo, net: spread - feeYes - feeNo };
  }, []);

  const poly = calcNet(yes, no, polyFee);
  const opinion = calcNet(yes, no, opinionFee);
  const minSpread = 0.015;
  const wouldTrigger = (1 - (yes + no)) >= minSpread;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    ctx.clearRect(0, 0, w, h);

    const combined = yes + no;
    const spread = 1.0 - combined;
    const isProfit = poly.net > 0;

    // Background grid
    ctx.strokeStyle = C.border;
    ctx.lineWidth = 0.5;
    for (let x = 0; x <= w; x += 60) {
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
    }

    // Combined price bar
    const barY = 20, barH = 16;
    const maxBar = w - 100;
    const combinedW = Math.min(combined, 1.5) / 1.5 * maxBar;
    const threshold1W = 1.0 / 1.5 * maxBar;

    // Background bar
    ctx.fillStyle = 'rgba(255,255,255,0.04)';
    ctx.beginPath(); ctx.roundRect(60, barY, maxBar, barH, 4); ctx.fill();

    // Fill
    ctx.fillStyle = spread > 0 ? C.green + '99' : C.red + '99';
    ctx.beginPath(); ctx.roundRect(60, barY, combinedW, barH, 4); ctx.fill();

    // $1.00 line
    ctx.strokeStyle = C.amber;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(60 + threshold1W, barY - 4); ctx.lineTo(60 + threshold1W, barY + barH + 4); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = C.amber;
    ctx.font = `600 9px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.fillText('$1.00', 60 + threshold1W, barY - 8);

    ctx.fillStyle = C.muted;
    ctx.font = `400 10px ${MONO}`;
    ctx.textAlign = 'right';
    ctx.fillText('Combined:', 55, barY + 12);

    const priceColor = spread > 0 ? C.green : C.red;
    ctx.fillStyle = priceColor;
    ctx.font = `700 11px ${MONO}`;
    ctx.textAlign = 'left';
    ctx.fillText(`$${combined.toFixed(3)}`, 60 + combinedW + 6, barY + 12);

    // Polymarket net profit bar
    const row2Y = 58;
    ctx.fillStyle = C.muted;
    ctx.font = `400 10px ${MONO}`;
    ctx.textAlign = 'right';
    ctx.fillText('Poly Net:', 55, row2Y + 10);

    const polyNetNorm = (poly.net + 0.05) / 0.1; // -0.05 to +0.05 mapped to 0-1
    const polyBarW = polyNetNorm * maxBar;
    ctx.fillStyle = 'rgba(255,255,255,0.04)';
    ctx.beginPath(); ctx.roundRect(60, row2Y, maxBar, 12, 3); ctx.fill();
    ctx.fillStyle = poly.net > 0 ? C.green + '99' : C.red + '66';
    ctx.beginPath(); ctx.roundRect(
      poly.net > 0 ? 60 + maxBar * 0.5 : 60 + polyBarW,
      row2Y,
      Math.abs(polyBarW - maxBar * 0.5),
      12, 3
    ); ctx.fill();
    // Zero line
    ctx.strokeStyle = C.muted;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(60 + maxBar * 0.5, row2Y - 2); ctx.lineTo(60 + maxBar * 0.5, row2Y + 14); ctx.stroke();
    ctx.fillStyle = poly.net > 0 ? C.green : C.red;
    ctx.font = `700 10px ${MONO}`;
    ctx.textAlign = 'left';
    ctx.fillText(`${poly.net >= 0 ? '+' : ''}$${poly.net.toFixed(4)}`, 60 + maxBar + 6, row2Y + 10);

    // Opinion net
    const row3Y = 82;
    ctx.fillStyle = C.muted;
    ctx.font = `400 10px ${MONO}`;
    ctx.textAlign = 'right';
    ctx.fillText('Opin Net:', 55, row3Y + 10);
    const opNorm = (opinion.net + 0.05) / 0.1;
    const opBarW = opNorm * maxBar;
    ctx.fillStyle = 'rgba(255,255,255,0.04)';
    ctx.beginPath(); ctx.roundRect(60, row3Y, maxBar, 12, 3); ctx.fill();
    ctx.fillStyle = opinion.net > 0 ? C.green + '99' : C.red + '66';
    ctx.beginPath(); ctx.roundRect(
      opinion.net > 0 ? 60 + maxBar * 0.5 : 60 + opBarW,
      row3Y,
      Math.abs(opBarW - maxBar * 0.5),
      12, 3
    ); ctx.fill();
    ctx.strokeStyle = C.muted;
    ctx.beginPath(); ctx.moveTo(60 + maxBar * 0.5, row3Y - 2); ctx.lineTo(60 + maxBar * 0.5, row3Y + 14); ctx.stroke();
    ctx.fillStyle = opinion.net > 0 ? C.green : C.red;
    ctx.font = `700 10px ${MONO}`;
    ctx.textAlign = 'left';
    ctx.fillText(`${opinion.net >= 0 ? '+' : ''}$${opinion.net.toFixed(4)}`, 60 + maxBar + 6, row3Y + 10);

    // Trigger indicator
    const trigY = h - 20;
    ctx.fillStyle = wouldTrigger ? C.green : C.red;
    ctx.font = `700 11px ${MONO}`;
    ctx.textAlign = 'left';
    ctx.fillText(
      wouldTrigger ? '✓ WOULD TRIGGER (spread ≥ 1.5%)' : '✗ NO TRIGGER (spread < 1.5%)',
      60, trigY
    );

  }, [yes, no, poly, opinion, wouldTrigger]);

  return (
    <div style={{ fontFamily: MONO }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 16 }}>
        <div>
          <label style={{ color: C.muted, fontSize: 11, display: 'block', marginBottom: 6 }}>
            YES price: <span style={{ color: C.cyan, fontWeight: 700 }}>${yes.toFixed(2)}</span>
          </label>
          <input
            type="range" min="1" max="99" value={Math.round(yes * 100)}
            onChange={e => setYes(e.target.value / 100)}
            style={{ width: '100%', accentColor: C.cyan, cursor: 'pointer', height: 6 }}
          />
        </div>
        <div>
          <label style={{ color: C.muted, fontSize: 11, display: 'block', marginBottom: 6 }}>
            NO price: <span style={{ color: C.purple, fontWeight: 700 }}>${no.toFixed(2)}</span>
          </label>
          <input
            type="range" min="1" max="99" value={Math.round(no * 100)}
            onChange={e => setNo(e.target.value / 100)}
            style={{ width: '100%', accentColor: C.purple, cursor: 'pointer', height: 6 }}
          />
        </div>
      </div>

      <canvas ref={canvasRef} style={{ width: '100%', height: 120, display: 'block', marginBottom: 16 }} />

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10 }}>
        {[
          { label: 'Combined', val: `$${(yes + no).toFixed(3)}`, color: (yes + no) < 1 ? C.green : C.red },
          { label: 'Spread', val: `$${Math.max(0, 1 - yes - no).toFixed(4)}`, color: C.amber },
          { label: 'Poly Fee/leg', val: `~$${((polyFee * yes * (1 - yes) + polyFee * no * (1 - no)) / 2).toFixed(4)}`, color: C.muted },
        ].map(item => (
          <div key={item.label} style={{
            background: 'rgba(0,0,0,0.3)', borderRadius: 8, padding: '10px 12px',
            border: `1px solid ${C.border}`,
          }}>
            <div style={{ color: C.muted, fontSize: 9, letterSpacing: '0.1em', marginBottom: 4 }}>{item.label}</div>
            <div style={{ color: item.color, fontSize: 14, fontWeight: 700 }}>{item.val}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Canvas: VPIN Line Chart ──────────────────────────────────────────────────
function VPINLineChart({ data, height = 120 }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    ctx.clearRect(0, 0, w, h);

    const pad = { l: 40, r: 20, t: 16, b: 24 };
    const cw = w - pad.l - pad.r, ch = h - pad.t - pad.b;

    // Zone backgrounds
    const zones = [
      { min: 0.7, max: 1.0, color: C.red },
      { min: 0.55, max: 0.7, color: C.amber },
      { min: 0.4, max: 0.55, color: '#f97316' },
      { min: 0.0, max: 0.4, color: C.green },
    ];
    zones.forEach(z => {
      const y1 = pad.t + ch * (1 - z.max);
      const y2 = pad.t + ch * (1 - z.min);
      ctx.fillStyle = `${z.color}12`;
      ctx.fillRect(pad.l, y1, cw, y2 - y1);
    });

    // Zone lines
    [0.4, 0.55, 0.7].forEach(v => {
      const y = pad.t + ch * (1 - v);
      ctx.strokeStyle = `rgba(255,255,255,0.08)`;
      ctx.lineWidth = 0.5;
      ctx.setLineDash([3, 4]);
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + cw, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = C.muted;
      ctx.font = `400 8px ${MONO}`;
      ctx.textAlign = 'right';
      ctx.fillText(v.toFixed(2), pad.l - 4, y + 3);
    });

    if (!data || data.length === 0) {
      // Draw demo data
      const pts = [];
      for (let i = 0; i < 80; i++) {
        const t = i / 79;
        let v = 0.25 + Math.sin(t * 3.14) * 0.15 + Math.random() * 0.05;
        if (i > 55 && i < 70) v = 0.5 + (i - 55) / 15 * 0.35 + Math.random() * 0.03;
        if (i >= 70) v = 0.82 + Math.random() * 0.04;
        pts.push(Math.min(1, Math.max(0, v)));
      }
      data = pts;
    }

    const n = data.length;
    const getX = i => pad.l + (i / (n - 1)) * cw;
    const getY = v => pad.t + ch * (1 - v);

    // Gradient fill
    const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ch);
    grad.addColorStop(0, `${C.purple}44`);
    grad.addColorStop(1, `${C.purple}00`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(data[0]));
    data.forEach((v, i) => ctx.lineTo(getX(i), getY(v)));
    ctx.lineTo(getX(n - 1), pad.t + ch);
    ctx.lineTo(getX(0), pad.t + ch);
    ctx.closePath();
    ctx.fill();

    // Line
    ctx.strokeStyle = C.purple;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(data[0]));
    data.forEach((v, i) => ctx.lineTo(getX(i), getY(v)));
    ctx.stroke();

    // Axes
    ctx.fillStyle = C.muted;
    ctx.font = `400 8px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.fillText('Time →', pad.l + cw / 2, h - 4);
    ctx.textAlign = 'right';
    ctx.fillText('1.00', pad.l - 4, pad.t + 8);
    ctx.fillText('0.00', pad.l - 4, pad.t + ch + 4);

    // Labels
    ctx.fillStyle = C.green; ctx.font = `600 8px ${MONO}`; ctx.textAlign = 'left';
    ctx.fillText('CALM', pad.l + 4, pad.t + ch - 4);
    ctx.fillStyle = C.red;
    ctx.fillText('CASCADE', pad.l + 4, pad.t + 10);

  }, [data, height]);

  return <canvas ref={canvasRef} style={{ width: '100%', height, display: 'block' }} />;
}

// ─── VPIN Bucket Visualisation ────────────────────────────────────────────────
function VPINBuckets() {
  const canvasRef = useRef(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1200);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    ctx.clearRect(0, 0, w, h);

    const buckets = [
      { buy: 0.52, sell: 0.48, label: 'Bucket 1' },
      { buy: 0.55, sell: 0.45, label: 'Bucket 2' },
      { buy: 0.48, sell: 0.52, label: 'Bucket 3' },
      { buy: 0.78, sell: 0.22, label: 'Bucket 4' },
      { buy: 0.82, sell: 0.18, label: 'Bucket 5' },
      { buy: 0.85, sell: 0.15, label: 'Bucket 6' },
    ];

    // Add animation jitter
    const t = tick * 0.3;
    buckets[buckets.length - 1].buy = 0.85 + Math.sin(t) * 0.04;
    buckets[buckets.length - 1].sell = 1 - buckets[buckets.length - 1].buy;

    const bw = Math.min(70, (w - 80) / buckets.length - 8);
    const bh = h - 60;
    const startX = (w - (buckets.length * (bw + 8))) / 2;
    const baseY = h - 30;

    let totalImbal = 0;

    buckets.forEach((b, i) => {
      const imbal = Math.abs(b.buy - b.sell);
      totalImbal += imbal;
      const x = startX + i * (bw + 8);

      // Background
      ctx.fillStyle = 'rgba(255,255,255,0.03)';
      ctx.beginPath(); ctx.roundRect(x, baseY - bh, bw, bh, 4); ctx.fill();

      // Sell (bottom)
      const sellH = b.sell * bh;
      ctx.fillStyle = `${C.red}cc`;
      ctx.beginPath(); ctx.roundRect(x, baseY - sellH, bw, sellH, [0, 0, 4, 4]); ctx.fill();

      // Buy (top of sell)
      const buyH = b.buy * bh;
      ctx.fillStyle = `${C.green}cc`;
      ctx.beginPath(); ctx.roundRect(x, baseY - sellH - buyH + sellH, bw, buyH - sellH, [4, 4, 0, 0]); ctx.fill();
      // Actually let's do it differently: buy from top, sell from bottom
      ctx.clearRect(x, baseY - bh, bw, bh);
      ctx.fillStyle = 'rgba(255,255,255,0.03)';
      ctx.beginPath(); ctx.roundRect(x, baseY - bh, bw, bh, 4); ctx.fill();

      const bH = b.buy * bh;
      const sH = b.sell * bh;
      ctx.fillStyle = `${C.green}bb`;
      ctx.beginPath(); ctx.roundRect(x, baseY - bh, bw, bH, [4, 4, 0, 0]); ctx.fill();
      ctx.fillStyle = `${C.red}bb`;
      ctx.beginPath(); ctx.roundRect(x, baseY - sH, bw, sH, [0, 0, 4, 4]); ctx.fill();

      // Imbalance label
      ctx.fillStyle = imbal > 0.3 ? C.red : C.muted;
      ctx.font = `700 9px ${MONO}`;
      ctx.textAlign = 'center';
      ctx.fillText(`${(imbal * 100).toFixed(0)}%`, x + bw / 2, baseY - bh - 6);

      // Bucket label
      ctx.fillStyle = C.muted;
      ctx.font = `400 8px ${MONO}`;
      ctx.fillText(`B${i + 1}`, x + bw / 2, baseY + 12);
    });

    // VPIN = mean imbalance
    const vpin = totalImbal / buckets.length;
    ctx.fillStyle = vpin > 0.7 ? C.red : vpin > 0.55 ? C.amber : C.green;
    ctx.font = `700 13px ${MONO}`;
    ctx.textAlign = 'right';
    ctx.fillText(`VPIN = ${vpin.toFixed(3)}`, w - 10, 18);

    ctx.fillStyle = C.muted;
    ctx.font = `400 9px ${MONO}`;
    ctx.fillText('mean imbalance', w - 10, 30);

    // Legend
    ctx.fillStyle = C.green;
    ctx.fillRect(10, 8, 10, 8);
    ctx.fillStyle = C.muted; ctx.font = `400 9px ${MONO}`; ctx.textAlign = 'left';
    ctx.fillText('Buy vol', 24, 16);
    ctx.fillStyle = C.red;
    ctx.fillRect(10, 22, 10, 8);
    ctx.fillStyle = C.muted;
    ctx.fillText('Sell vol', 24, 30);

  }, [tick]);

  return <canvas ref={canvasRef} style={{ width: '100%', height: 160, display: 'block' }} />;
}

// ─── VPIN Threshold Zones ─────────────────────────────────────────────────────
function VPINZones({ current = 0.72 }) {
  const canvasRef = useRef(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    ctx.clearRect(0, 0, w, h);

    const zones = [
      { min: 0.00, max: 0.40, color: C.green,  label: 'CALM',     desc: 'Normal market' },
      { min: 0.40, max: 0.55, color: C.amber,  label: 'ELEVATED', desc: 'Some informed flow' },
      { min: 0.55, max: 0.70, color: '#f97316', label: 'INFORMED', desc: 'Significant toxic flow' },
      { min: 0.70, max: 1.00, color: C.red,    label: 'CASCADE',  desc: 'Extreme — likely cascade' },
    ];

    const bh = 28, gap = 6;
    const pad = { l: 100, r: 100 };
    const barW = w - pad.l - pad.r;

    zones.forEach((z, i) => {
      const y = 20 + i * (bh + gap);
      const x1 = pad.l + z.min * barW;
      const x2 = pad.l + z.max * barW;
      const bw = x2 - x1;

      ctx.fillStyle = `${z.color}22`;
      ctx.strokeStyle = `${z.color}55`;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.roundRect(x1, y, bw, bh, 4); ctx.fill(); ctx.stroke();

      ctx.fillStyle = z.color;
      ctx.font = `700 10px ${MONO}`;
      ctx.textAlign = 'center';
      ctx.fillText(z.label, x1 + bw / 2, y + 12);
      ctx.fillStyle = `${z.color}aa`;
      ctx.font = `400 9px ${MONO}`;
      ctx.fillText(z.desc, x1 + bw / 2, y + 24);

      // Range label
      ctx.fillStyle = C.muted;
      ctx.font = `400 9px ${MONO}`;
      ctx.textAlign = 'right';
      ctx.fillText(`${z.min.toFixed(2)}–${z.max.toFixed(2)}`, x1 - 6, y + 17);
    });

    // Current VPIN indicator
    const cx = pad.l + current * barW;
    ctx.strokeStyle = C.text;
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(cx, 16); ctx.lineTo(cx, 20 + 4 * (bh + gap)); ctx.stroke();
    ctx.fillStyle = C.text;
    ctx.font = `700 10px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.fillText(`▲ ${current.toFixed(3)}`, cx, 12);
    ctx.fillText('CURRENT', cx, h - 6);

  }, [current]);

  return <canvas ref={canvasRef} style={{ width: '100%', height: 180, display: 'block' }} />;
}

// ─── Cascade State Machine ────────────────────────────────────────────────────
const CASCADE_STATES = [
  {
    id: 'IDLE',
    color: C.green,
    x: 60, y: 90,
    trigger: 'System start or cooldown ends',
    doing: 'Monitoring VPIN + liquidation feed',
    duration: 'Minutes to hours',
    next: 'DETECTED',
    nextTrigger: 'VPIN > cascade_threshold',
  },
  {
    id: 'DETECTED',
    color: C.amber,
    x: 220, y: 90,
    trigger: 'VPIN spikes above 0.70',
    doing: 'Waiting for liquidation volume confirmation',
    duration: '10–60 seconds',
    next: 'EXHAUSTING',
    nextTrigger: 'Liq volume > min_liq_usd',
  },
  {
    id: 'EXHAUSTING',
    color: '#f97316',
    x: 380, y: 90,
    trigger: 'Large liquidation cascade confirmed',
    doing: 'Watching liq rate for deceleration',
    duration: '30–300 seconds',
    next: 'BET_SIGNAL',
    nextTrigger: 'Rate drops > decel_threshold',
  },
  {
    id: 'BET_SIGNAL',
    color: C.purple,
    x: 540, y: 90,
    trigger: 'Cascade exhaust detected',
    doing: 'Placing mean-reversion bet',
    duration: '< 1 second',
    next: 'COOLDOWN',
    nextTrigger: 'Bet placed successfully',
  },
  {
    id: 'COOLDOWN',
    color: C.cyan,
    x: 700, y: 90,
    trigger: 'Bet placed',
    doing: 'Waiting cooldown_minutes before next hunt',
    duration: 'cooldown_minutes (default 15min)',
    next: 'IDLE',
    nextTrigger: 'Cooldown expires',
  },
];

function CascadeFSM() {
  const [activeState, setActiveState] = useState(null);
  const [animState, setAnimState] = useState(0);
  const canvasRef = useRef(null);

  useEffect(() => {
    const id = setInterval(() => setAnimState(s => (s + 1) % CASCADE_STATES.length), 2000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    ctx.clearRect(0, 0, w, h);

    const bw = 100, bh = 40;
    const scaleX = w / 820;

    CASCADE_STATES.forEach((state, i) => {
      const sx = state.x * scaleX;
      const sy = state.y;
      const isActive = activeState === state.id || (!activeState && i === animState);
      const color = state.color;

      // Arrow to next
      if (i < CASCADE_STATES.length - 1) {
        const nx = CASCADE_STATES[i + 1].x * scaleX;
        const arrowX1 = sx + bw;
        const arrowX2 = nx;
        const ay = sy + bh / 2;
        ctx.strokeStyle = isActive ? color + 'cc' : 'rgba(255,255,255,0.15)';
        ctx.lineWidth = isActive ? 2 : 1;
        ctx.beginPath(); ctx.moveTo(arrowX1, ay); ctx.lineTo(arrowX2 - 8, ay); ctx.stroke();
        ctx.fillStyle = isActive ? color + 'cc' : 'rgba(255,255,255,0.15)';
        ctx.beginPath();
        ctx.moveTo(arrowX2 - 8, ay - 4);
        ctx.lineTo(arrowX2, ay);
        ctx.lineTo(arrowX2 - 8, ay + 4);
        ctx.fill();

        // Transition label
        const midX = (arrowX1 + arrowX2 - 8) / 2;
        ctx.fillStyle = isActive ? color + '99' : 'rgba(255,255,255,0.2)';
        ctx.font = `400 7px ${MONO}`;
        ctx.textAlign = 'center';
        // Truncate trigger
        const trig = state.nextTrigger.length > 16 ? state.nextTrigger.slice(0, 14) + '…' : state.nextTrigger;
        ctx.fillText(trig, midX, ay - 6);
      }

      // State box
      ctx.fillStyle = isActive ? `${color}25` : `${color}0c`;
      ctx.strokeStyle = isActive ? color : `${color}55`;
      ctx.lineWidth = isActive ? 2 : 1;
      ctx.shadowColor = isActive ? color : 'transparent';
      ctx.shadowBlur = isActive ? 12 : 0;
      ctx.beginPath(); ctx.roundRect(sx, sy, bw, bh, 8); ctx.fill(); ctx.stroke();
      ctx.shadowBlur = 0;

      ctx.fillStyle = isActive ? color : `${color}99`;
      ctx.font = `700 10px ${MONO}`;
      ctx.textAlign = 'center';
      ctx.fillText(state.id, sx + bw / 2, sy + bh / 2 + 4);
    });

    // Loop arrow from COOLDOWN back to IDLE (bottom arc)
    const lastState = CASCADE_STATES[CASCADE_STATES.length - 1];
    const firstState = CASCADE_STATES[0];
    const lx = lastState.x * scaleX + bw / 2;
    const fx = firstState.x * scaleX + bw / 2;
    const cy2 = lastState.y + bh + 20;
    ctx.strokeStyle = 'rgba(255,255,255,0.12)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(lx, lastState.y + bh);
    ctx.lineTo(lx, cy2);
    ctx.lineTo(fx, cy2);
    ctx.lineTo(fx, firstState.y + bh);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(255,255,255,0.12)';
    ctx.beginPath();
    ctx.moveTo(fx - 4, firstState.y + bh);
    ctx.lineTo(fx + 4, firstState.y + bh);
    ctx.lineTo(fx, firstState.y + bh - 7);
    ctx.fill();

  }, [activeState, animState]);

  const displayState = activeState
    ? CASCADE_STATES.find(s => s.id === activeState)
    : CASCADE_STATES[animState];

  return (
    <div>
      <canvas
        ref={canvasRef}
        style={{ width: '100%', height: 160, display: 'block', cursor: 'pointer' }}
        onClick={e => {
          const rect = e.currentTarget.getBoundingClientRect();
          const mx = e.clientX - rect.left;
          const my = e.clientY - rect.top;
          const scaleX = rect.width / 820;
          const bw = 100, bh = 40;
          let found = null;
          CASCADE_STATES.forEach(s => {
            const sx = s.x * scaleX, sy = s.y;
            if (mx >= sx && mx <= sx + bw && my >= sy && my <= sy + bh) found = s.id;
          });
          setActiveState(found === activeState ? null : found);
        }}
      />
      {displayState && (
        <div style={{
          background: `${displayState.color}10`,
          border: `1px solid ${displayState.color}33`,
          borderRadius: 8, padding: '14px 18px', marginTop: 12,
          fontFamily: MONO, fontSize: 12,
          animation: 'fadeIn 200ms ease-out',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
            <div style={{
              background: displayState.color, color: '#000',
              borderRadius: 4, padding: '2px 8px', fontSize: 11, fontWeight: 700,
            }}>
              {displayState.id}
            </div>
            <span style={{ color: C.muted, fontSize: 10 }}>Click any state for details • Auto-animating</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '6px 16px' }}>
            {[
              ['Triggered by', displayState.trigger],
              ['System is doing', displayState.doing],
              ['Typical duration', displayState.duration],
              ['Transitions to', `${displayState.next} when: ${displayState.nextTrigger}`],
            ].map(([k, v]) => (
              <React.Fragment key={k}>
                <span style={{ color: C.muted }}>{k}:</span>
                <span style={{ color: C.text }}>{v}</span>
              </React.Fragment>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Equity Curve Canvas ──────────────────────────────────────────────────────
function EquityCurveCanvas({ bankroll = 25, drawdownPct = 0.10 }) {
  const canvasRef = useRef(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    ctx.clearRect(0, 0, w, h);

    const pad = { l: 50, r: 20, t: 16, b: 28 };
    const cw = w - pad.l - pad.r, ch = h - pad.t - pad.b;

    const killLine = bankroll * (1 - drawdownPct);
    const maxVal = bankroll * 1.3, minVal = bankroll * 0.8;

    const toY = v => pad.t + ch * (1 - (v - minVal) / (maxVal - minVal));
    const toX = i => pad.l + (i / 79) * cw;

    // Generate demo equity curve
    let equity = bankroll;
    const pts = [equity];
    for (let i = 1; i < 80; i++) {
      const r = (Math.random() - 0.47) * 0.4;
      equity = Math.max(bankroll * 0.85, equity * (1 + r * 0.02));
      pts.push(equity);
    }

    // Gradient fill
    const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ch);
    grad.addColorStop(0, `${C.cyan}44`);
    grad.addColorStop(1, `${C.cyan}05`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.moveTo(toX(0), toY(pts[0]));
    pts.forEach((v, i) => ctx.lineTo(toX(i), toY(v)));
    ctx.lineTo(toX(79), toY(0) + 20);
    ctx.lineTo(toX(0), toY(0) + 20);
    ctx.closePath(); ctx.fill();

    // Equity line
    ctx.strokeStyle = C.cyan;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(toX(0), toY(pts[0]));
    pts.forEach((v, i) => ctx.lineTo(toX(i), toY(v)));
    ctx.stroke();

    // Starting bankroll line
    ctx.strokeStyle = C.muted;
    ctx.lineWidth = 0.5;
    ctx.setLineDash([3, 4]);
    const startY = toY(bankroll);
    ctx.beginPath(); ctx.moveTo(pad.l, startY); ctx.lineTo(pad.l + cw, startY); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = C.muted;
    ctx.font = `400 9px ${MONO}`;
    ctx.textAlign = 'right';
    ctx.fillText(`$${bankroll}`, pad.l - 4, startY + 3);

    // Kill switch line
    const ky = toY(killLine);
    ctx.strokeStyle = C.red;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(pad.l, ky); ctx.lineTo(pad.l + cw, ky); ctx.stroke();
    ctx.setLineDash([]);

    // Kill switch fill
    ctx.fillStyle = `${C.red}10`;
    ctx.fillRect(pad.l, ky, cw, pad.t + ch - ky);

    ctx.fillStyle = C.red;
    ctx.font = `700 9px ${MONO}`;
    ctx.textAlign = 'right';
    ctx.fillText(`Kill: $${killLine.toFixed(2)}`, pad.l - 4, ky + 3);
    ctx.textAlign = 'left';
    ctx.fillText(`⚡ KILL SWITCH`, pad.l + 4, ky - 4);

    // Axes
    ctx.fillStyle = C.muted;
    ctx.font = `400 8px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.fillText('Time →', pad.l + cw / 2, h - 4);

  }, [bankroll, drawdownPct]);
  return <canvas ref={canvasRef} style={{ width: '100%', height: 140, display: 'block' }} />;
}

// ─── Config Threshold Bar ─────────────────────────────────────────────────────
function ThresholdBar({ variable }) {
  const { name, default: def, range, unit, desc, impact } = variable;
  const [min, max] = range;
  const pct = ((def - min) / (max - min)) * 100;

  const formatVal = v => {
    if (unit === '%') return `${(v * 100).toFixed(1)}%`;
    if (unit === '$') return v >= 1000 ? `$${(v / 1000).toFixed(0)}k` : `$${v}`;
    if (unit === 'ms') return `${v}ms`;
    if (unit === 'min') return `${v}min`;
    if (unit === 'hr') return `${v}hr`;
    if (unit === '#') return `${v}`;
    return `${v}`;
  };

  const color = pct < 33 ? C.green : pct < 66 ? C.amber : C.purple;

  return (
    <tr style={{ borderBottom: `1px solid ${C.border}` }}>
      <td style={{ padding: '10px 12px', fontFamily: MONO, fontSize: 11, color: C.cyan, whiteSpace: 'nowrap' }}>
        {name}
      </td>
      <td style={{ padding: '10px 12px', fontFamily: MONO, fontSize: 11, color: C.text, whiteSpace: 'nowrap' }}>
        {formatVal(def)}
      </td>
      <td style={{ padding: '10px 12px', minWidth: 120 }}>
        <div style={{ height: 6, background: 'rgba(255,255,255,0.06)', borderRadius: 3, overflow: 'hidden' }}>
          <div style={{
            height: '100%', width: `${pct}%`, background: color, borderRadius: 3,
            boxShadow: `0 0 6px ${color}66`,
            transition: 'width 300ms ease-out',
          }} />
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 2 }}>
          <span style={{ color: C.muted, fontSize: 9, fontFamily: MONO }}>{formatVal(min)}</span>
          <span style={{ color: C.muted, fontSize: 9, fontFamily: MONO }}>{formatVal(max)}</span>
        </div>
      </td>
      <td style={{ padding: '10px 12px', fontSize: 12, color: 'rgba(255,255,255,0.55)' }}>
        {desc}
      </td>
      <td style={{ padding: '10px 12px', fontSize: 11, color: C.muted, fontFamily: MONO, fontSize: 11 }}>
        {impact}
      </td>
    </tr>
  );
}

// ─── Fee Curve Canvas ─────────────────────────────────────────────────────────
function FeeCurveCanvas() {
  const canvasRef = useRef(null);
  const [price, setPrice] = useState(0.50);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    ctx.clearRect(0, 0, w, h);

    const pad = { l: 52, r: 20, t: 20, b: 32 };
    const cw = w - pad.l - pad.r, ch = h - pad.t - pad.b;
    const polyMult = 0.072, opinMult = 0.04;
    const maxFee = polyMult * 0.5 * 0.5 * 1.05;

    const toX = p => pad.l + p * cw;
    const toY = fee => pad.t + ch * (1 - fee / maxFee);

    // Grid
    [0, 0.25, 0.5, 0.75, 1].forEach(p => {
      ctx.strokeStyle = C.border; ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(toX(p), pad.t); ctx.lineTo(toX(p), pad.t + ch); ctx.stroke();
    });

    // Draw curves
    const drawCurve = (mult, color) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (let i = 0; i <= 100; i++) {
        const p = i / 100;
        const fee = mult * p * (1 - p);
        if (i === 0) ctx.moveTo(toX(p), toY(fee));
        else ctx.lineTo(toX(p), toY(fee));
      }
      ctx.stroke();

      // Gradient fill
      const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ch);
      grad.addColorStop(0, color + '30');
      grad.addColorStop(1, color + '00');
      ctx.fillStyle = grad;
      ctx.beginPath();
      for (let i = 0; i <= 100; i++) {
        const p = i / 100;
        const fee = mult * p * (1 - p);
        if (i === 0) ctx.moveTo(toX(p), toY(fee));
        else ctx.lineTo(toX(p), toY(fee));
      }
      ctx.lineTo(toX(1), toY(0));
      ctx.lineTo(toX(0), toY(0));
      ctx.closePath(); ctx.fill();
    };

    drawCurve(opinMult, C.cyan);
    drawCurve(polyMult, C.purple);

    // Current price line
    const px = toX(price);
    ctx.strokeStyle = C.amber;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(px, pad.t); ctx.lineTo(px, pad.t + ch); ctx.stroke();
    ctx.setLineDash([]);

    const polyFee = polyMult * price * (1 - price);
    const opinFee = opinMult * price * (1 - price);

    ctx.fillStyle = C.amber;
    ctx.font = `700 9px ${MONO}`;
    ctx.textAlign = price > 0.7 ? 'right' : 'left';
    ctx.fillText(`p=${price.toFixed(2)}`, px + (price > 0.7 ? -4 : 4), pad.t + 12);

    // Dots
    [{ mult: polyMult, color: C.purple }, { mult: opinMult, color: C.cyan }].forEach(({ mult, color }) => {
      const fee = mult * price * (1 - price);
      const dy = toY(fee);
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(px, dy, 4, 0, Math.PI * 2); ctx.fill();
    });

    // Y axis labels
    [0, 0.005, 0.01, 0.015, 0.018].forEach(fee => {
      const fy = toY(fee);
      ctx.fillStyle = C.muted;
      ctx.font = `400 8px ${MONO}`;
      ctx.textAlign = 'right';
      ctx.fillText(`$${fee.toFixed(3)}`, pad.l - 4, fy + 3);
      ctx.strokeStyle = C.border; ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(pad.l, fy); ctx.lineTo(pad.l + cw, fy); ctx.stroke();
    });

    // X axis labels
    [0, 0.25, 0.5, 0.75, 1].forEach(p => {
      ctx.fillStyle = C.muted;
      ctx.font = `400 9px ${MONO}`;
      ctx.textAlign = 'center';
      ctx.fillText(p.toFixed(2), toX(p), pad.t + ch + 16);
    });

    // Legend
    ctx.fillStyle = C.purple; ctx.font = `600 9px ${MONO}`; ctx.textAlign = 'left';
    ctx.fillRect(pad.l + 10, pad.t + 8, 16, 2);
    ctx.fillText(`Polymarket (7.2%) → $${polyFee.toFixed(4)}`, pad.l + 30, pad.t + 12);
    ctx.fillStyle = C.cyan;
    ctx.fillRect(pad.l + 10, pad.t + 22, 16, 2);
    ctx.fillText(`Opinion (4.0%) → $${opinFee.toFixed(4)}`, pad.l + 30, pad.t + 26);

    ctx.fillStyle = C.muted; ctx.font = `400 9px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.fillText('Price (p)', pad.l + cw / 2, h - 4);

  }, [price]);

  return (
    <div>
      <canvas ref={canvasRef} style={{ width: '100%', height: 180, display: 'block', marginBottom: 12 }} />
      <div style={{ fontFamily: MONO }}>
        <label style={{ color: C.muted, fontSize: 11, display: 'block', marginBottom: 6 }}>
          Price: <span style={{ color: C.amber, fontWeight: 700 }}>${price.toFixed(2)}</span>
          {' — '}
          <span style={{ color: C.purple }}>Poly: ${(0.072 * price * (1 - price)).toFixed(4)}</span>
          {' | '}
          <span style={{ color: C.cyan }}>Opinion: ${(0.04 * price * (1 - price)).toFixed(4)}</span>
        </label>
        <input
          type="range" min="1" max="99" value={Math.round(price * 100)}
          onChange={e => setPrice(e.target.value / 100)}
          style={{ width: '100%', accentColor: C.amber, cursor: 'pointer', height: 6 }}
        />
      </div>
    </div>
  );
}

// ─── Venue Comparison Canvas ──────────────────────────────────────────────────
function VenueComparison() {
  const canvasRef = useRef(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    ctx.clearRect(0, 0, w, h);

    const metrics = [
      { label: 'Fees (round-trip)', poly: 0.072 / 0.15, opin: 0.04 / 0.15, polyVal: '7.2%', opinVal: '4.0%', invert: true },
      { label: 'Liquidity depth', poly: 0.85, opin: 0.45, polyVal: 'Deep', opinVal: 'Moderate', invert: false },
      { label: 'Execution speed', poly: 0.65, opin: 0.80, polyVal: '~300ms', opinVal: '~150ms', invert: false },
      { label: 'Market count', poly: 0.90, opin: 0.40, polyVal: '200+', opinVal: '50+', invert: false },
    ];

    const bh = 22, gap = 20, barMaxW = (w - 200) / 2 - 10;
    const leftX = 100, rightX = w / 2 + 10;

    ctx.fillStyle = C.muted;
    ctx.font = `700 10px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.fillText('POLYMARKET', leftX + barMaxW / 2, 14);
    ctx.fillText('OPINION EXCHANGE', rightX + barMaxW / 2, 14);

    metrics.forEach((m, i) => {
      const y = 30 + i * (bh + gap);

      // Label
      ctx.fillStyle = 'rgba(255,255,255,0.5)';
      ctx.font = `400 9px ${MONO}`;
      ctx.textAlign = 'center';
      ctx.fillText(m.label, w / 2, y + bh / 2 + 3);

      // Poly bar
      const polyColor = m.invert ? (m.poly > 0.5 ? C.red : C.green) : C.purple;
      ctx.fillStyle = `${polyColor}22`;
      ctx.strokeStyle = `${polyColor}44`;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.roundRect(leftX, y, barMaxW, bh, 4); ctx.fill(); ctx.stroke();
      ctx.fillStyle = `${polyColor}bb`;
      ctx.beginPath(); ctx.roundRect(leftX, y, m.poly * barMaxW, bh, 4); ctx.fill();
      ctx.fillStyle = polyColor;
      ctx.font = `700 10px ${MONO}`;
      ctx.textAlign = 'right';
      ctx.fillText(m.polyVal, leftX - 6, y + bh / 2 + 3);

      // Opinion bar
      const opinColor = m.invert ? (m.opin > 0.5 ? C.red : C.green) : C.cyan;
      ctx.fillStyle = `${opinColor}22`;
      ctx.strokeStyle = `${opinColor}44`;
      ctx.beginPath(); ctx.roundRect(rightX, y, barMaxW, bh, 4); ctx.fill(); ctx.stroke();
      ctx.fillStyle = `${opinColor}bb`;
      ctx.beginPath(); ctx.roundRect(rightX, y, m.opin * barMaxW, bh, 4); ctx.fill();
      ctx.fillStyle = opinColor;
      ctx.font = `700 10px ${MONO}`;
      ctx.textAlign = 'left';
      ctx.fillText(m.opinVal, rightX + barMaxW + 6, y + bh / 2 + 3);
    });

  }, []);
  return <canvas ref={canvasRef} style={{ width: '100%', height: 160, display: 'block' }} />;
}

// ─── Live VPIN Section ────────────────────────────────────────────────────────
function LiveVPINSection() {
  const api = useApi();
  const [vpinData, setVpinData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const load = async () => {
      try {
        const res = await api('GET', '/dashboard/vpin-history');
        setVpinData(res.data);
      } catch {
        setVpinData(null);
      } finally {
        setLoading(false);
      }
    };
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [api]);

  const current = vpinData?.current ?? 0.42;
  const history = vpinData?.history?.map(d => d.vpin) ?? null;
  const zone = current >= 0.70 ? { label: 'CASCADE', color: C.red }
    : current >= 0.55 ? { label: 'INFORMED', color: '#f97316' }
    : current >= 0.40 ? { label: 'ELEVATED', color: C.amber }
    : { label: 'CALM', color: C.green };

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
        <div style={{
          fontFamily: MONO, fontSize: 28, fontWeight: 700, color: zone.color,
        }}>
          {current.toFixed(4)}
        </div>
        <div>
          <Badge color={zone.color}>{zone.label}</Badge>
          <div style={{ color: C.muted, fontFamily: MONO, fontSize: 10, marginTop: 4 }}>
            {loading ? 'Loading...' : 'Live · updates every 30s'}
          </div>
        </div>
      </div>
      <VPINLineChart data={history} height={110} />
    </div>
  );
}

// ─── TOC Sidebar ──────────────────────────────────────────────────────────────
function TOCSidebar({ activeSection }) {
  return (
    <nav style={{
      position: 'sticky', top: 80,
      fontFamily: MONO, fontSize: 11,
      maxHeight: 'calc(100vh - 100px)',
      overflowY: 'auto',
    }}>
      <div style={{ color: C.muted, fontSize: 9, letterSpacing: '0.15em', marginBottom: 12, textTransform: 'uppercase' }}>
        Contents
      </div>
      {TOC_SECTIONS.map(s => {
        const isActive = activeSection === s.id;
        return (
          <a
            key={s.id}
            href={`#${s.id}`}
            onClick={e => {
              e.preventDefault();
              document.getElementById(s.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }}
            style={{
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '7px 10px', borderRadius: 6, textDecoration: 'none',
              color: isActive ? C.purple : C.muted,
              background: isActive ? `${C.purple}12` : 'transparent',
              borderLeft: `2px solid ${isActive ? C.purple : 'transparent'}`,
              transition: 'all 150ms ease-out',
              marginBottom: 2,
              fontSize: isActive ? 11 : 10,
              fontWeight: isActive ? 700 : 400,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}
          >
            <span style={{ color: isActive ? C.purple : 'rgba(255,255,255,0.2)', fontSize: 9, flexShrink: 0 }}>§</span>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{s.label}</span>
          </a>
        );
      })}
    </nav>
  );
}

// ─── Mobile TOC Bar ───────────────────────────────────────────────────────────
function MobileTOC({ activeSection }) {
  const [open, setOpen] = useState(false);
  const current = TOC_SECTIONS.find(s => s.id === activeSection) || TOC_SECTIONS[0];

  return (
    <div style={{
      position: 'sticky', top: 52, zIndex: 100,
      background: 'rgba(7,7,12,0.97)', backdropFilter: 'blur(12px)',
      borderBottom: `1px solid ${C.border}`,
      fontFamily: MONO,
    }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          width: '100%', background: 'none', border: 'none', cursor: 'pointer',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '10px 16px', color: C.purple, fontSize: 12,
        }}
      >
        <span><span style={{ color: 'rgba(255,255,255,0.3)' }}>§ </span>{current.label}</span>
        <span style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 200ms', fontSize: 10, color: C.muted }}>▼</span>
      </button>
      {open && (
        <div style={{ padding: '0 8px 8px' }}>
          {TOC_SECTIONS.map(s => (
            <a
              key={s.id}
              href={`#${s.id}`}
              onClick={e => {
                e.preventDefault();
                setOpen(false);
                document.getElementById(s.id)?.scrollIntoView({ behavior: 'smooth' });
              }}
              style={{
                display: 'block', padding: '9px 12px', borderRadius: 6,
                textDecoration: 'none', fontSize: 12,
                color: activeSection === s.id ? C.purple : C.muted,
                background: activeSection === s.id ? `${C.purple}12` : 'transparent',
              }}
            >
              § {s.label}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Main Learn Page ──────────────────────────────────────────────────────────
export default function Learn() {
  const [activeSection, setActiveSection] = useState('overview');
  const sectionRefs = useRef({});

  useEffect(() => {
    const observers = [];
    TOC_SECTIONS.forEach(s => {
      const el = document.getElementById(s.id);
      if (!el) return;
      const obs = new IntersectionObserver(
        entries => {
          entries.forEach(entry => {
            if (entry.isIntersecting) setActiveSection(s.id);
          });
        },
        { rootMargin: '-20% 0px -70% 0px', threshold: 0 }
      );
      obs.observe(el);
      observers.push(obs);
    });
    return () => observers.forEach(o => o.disconnect());
  }, []);

  return (
    <div style={{ background: C.bg, minHeight: '100vh', color: C.text, fontFamily: MONO }}>
      {/* Mobile TOC — only shows on mobile via CSS */}
      <div className="learn-mobile-toc">
        <MobileTOC activeSection={activeSection} />
      </div>

      <div style={{ display: 'flex', maxWidth: 1200, margin: '0 auto' }}>
        {/* ── Sidebar TOC — desktop only ── */}
        <div className="learn-toc-sidebar" style={{
          width: 220, flexShrink: 0, padding: '32px 16px 32px 24px',
        }}>
          <TOCSidebar activeSection={activeSection} />
        </div>

        {/* ── Main Content ── */}
        <div style={{ flex: 1, minWidth: 0, padding: '32px 24px 80px 24px' }}>

          {/* Page header */}
          <div style={{ marginBottom: 48 }}>
            <div style={{
              display: 'inline-flex', alignItems: 'center', gap: 8,
              background: `${C.purple}12`, border: `1px solid ${C.purple}33`,
              borderRadius: 6, padding: '4px 12px', marginBottom: 16,
              fontSize: 10, letterSpacing: '0.12em', color: C.purple,
            }}>
              📚 DOCUMENTATION
            </div>
            <h1 style={{
              margin: 0, fontSize: 32, fontWeight: 700,
              letterSpacing: '-0.03em', color: C.text,
            }}>
              How Novakash Works
            </h1>
            <p style={{ margin: '12px 0 0', color: C.muted, fontSize: 13, lineHeight: 1.7, maxWidth: 600 }}>
              A complete technical guide to the strategies, math, signals, and risk management
              behind the Novakash automated trading system.
            </p>
          </div>

          {/* ════════════════════════════════════════════════════════ */}
          {/* § OVERVIEW */}
          {/* ════════════════════════════════════════════════════════ */}
          <SectionAnchor id="overview" />
          <section style={{ marginBottom: 64 }}>
            <SectionHeader>Overview</SectionHeader>

            <Card style={{ marginBottom: 20 }}>
              <p style={{ margin: 0, fontSize: 13, lineHeight: 1.8, color: 'rgba(255,255,255,0.7)' }}>
                Novakash runs two complementary strategies on prediction markets —{' '}
                <span style={{ color: C.cyan }}>Polymarket</span> and{' '}
                <span style={{ color: C.amber }}>Opinion Exchange</span> — that exploit{' '}
                <strong style={{ color: C.text }}>microstructure inefficiencies</strong> in BTC-related binary outcome markets.
                Neither strategy predicts the future. Both exploit mathematical certainties or statistical tendencies
                in how markets behave under stress.
              </p>
            </Card>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }} className="learn-2col">
              <Card glow={C.cyan} style={{ borderColor: `${C.cyan}22` }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 12 }}>
                  <span style={{ fontSize: 24 }}>⚖️</span>
                  <Badge color={C.green}>Risk-Free</Badge>
                </div>
                <h3 style={{ margin: '0 0 8px', fontSize: 16, color: C.cyan }}>Sub-$1 Arbitrage</h3>
                <p style={{ margin: 0, color: C.muted, fontSize: 12, lineHeight: 1.7 }}>
                  Buy both YES and NO when their combined price drops below $1.00.
                  Since exactly one must resolve at $1.00, profit is <strong style={{ color: C.text }}>mathematically guaranteed</strong> if the spread exceeds fees.
                </p>
              </Card>

              <Card glow={C.purple} style={{ borderColor: `${C.purple}22` }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 12 }}>
                  <span style={{ fontSize: 24 }}>🌊</span>
                  <Badge color={C.purple}>Directional</Badge>
                </div>
                <h3 style={{ margin: '0 0 8px', fontSize: 16, color: C.purple }}>VPIN Cascade</h3>
                <p style={{ margin: 0, color: C.muted, fontSize: 12, lineHeight: 1.7 }}>
                  Detect BTC liquidation cascades using VPIN (informed flow measurement) + Binance forceOrder events.
                  Bet on mean-reversion once the cascade <strong style={{ color: C.text }}>exhausts itself</strong>.
                </p>
              </Card>
            </div>
          </section>

          {/* ════════════════════════════════════════════════════════ */}
          {/* § STRATEGY 1: SUB-$1 ARBITRAGE */}
          {/* ════════════════════════════════════════════════════════ */}
          <SectionAnchor id="arb" />
          <section style={{ marginBottom: 64 }}>
            <SectionHeader color={C.cyan}>Strategy 1: Sub-$1 Arbitrage</SectionHeader>

            {/* Step 1 */}
            <Card style={{ marginBottom: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <div style={{
                  width: 24, height: 24, borderRadius: '50%',
                  background: `${C.cyan}22`, border: `1px solid ${C.cyan}55`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontWeight: 700, fontSize: 12, color: C.cyan, flexShrink: 0,
                }}>1</div>
                <h4 style={{ margin: 0, fontSize: 14, color: C.text }}>
                  YES + NO should always sum to $1.00
                </h4>
              </div>
              <p style={{ margin: '0 0 12px', color: C.muted, fontSize: 12, lineHeight: 1.7 }}>
                In a binary prediction market, exactly one outcome will resolve to $1.00 and the other to $0.00.
                If you hold both, you are guaranteed to receive $1.00 total. In equilibrium, market makers ensure
                the combined price stays near $1.00.
              </p>
              <ArbDiagram yes={0.65} no={0.35} label="Normal: combined = $1.00 → no arbitrage" />
            </Card>

            {/* Step 2 */}
            <Card style={{ marginBottom: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <div style={{
                  width: 24, height: 24, borderRadius: '50%',
                  background: `${C.amber}22`, border: `1px solid ${C.amber}55`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontWeight: 700, fontSize: 12, color: C.amber, flexShrink: 0,
                }}>2</div>
                <h4 style={{ margin: 0, fontSize: 14, color: C.text }}>
                  Under stress, market makers pull quotes — combined price drops
                </h4>
              </div>
              <p style={{ margin: '0 0 12px', color: C.muted, fontSize: 12, lineHeight: 1.7 }}>
                During BTC flash crashes or major news events, liquidity providers hedge their risk by
                widening spreads or pulling quotes entirely. The combined YES + NO price can drop below $1.00,
                creating a risk-free profit window that lasts seconds.
              </p>
              <ArbDiagram yes={0.60} no={0.38} label="Stressed: combined = $0.98 → $0.02 spread visible" />
            </Card>

            {/* Step 3 */}
            <Card style={{ marginBottom: 20 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <div style={{
                  width: 24, height: 24, borderRadius: '50%',
                  background: `${C.green}22`, border: `1px solid ${C.green}55`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontWeight: 700, fontSize: 12, color: C.green, flexShrink: 0,
                }}>3</div>
                <h4 style={{ margin: 0, fontSize: 14, color: C.text }}>
                  Buy BOTH simultaneously — one MUST pay out
                </h4>
              </div>
              <p style={{ margin: '0 0 12px', color: C.muted, fontSize: 12, lineHeight: 1.7 }}>
                We execute both legs in parallel (target: within 500ms). Regardless of the outcome, we receive $1.00.
                The question is whether the spread minus fees is positive.
              </p>
              <ArbFlowDiagram />
            </Card>

            {/* The Math */}
            <h3 style={{ fontSize: 15, color: C.text, marginBottom: 8 }}>The Math</h3>
            <MathBlock>{`Spread = 1.00 - (YES_price + NO_price)
Fee per leg = fee_multiplier × price × (1 - price)
Net profit = Spread - Fee_YES - Fee_NO

Example A (not profitable):
  YES = $0.60, NO = $0.38
  Spread = 1.00 - 0.98 = $0.020
  Fee_YES = 0.072 × 0.60 × 0.40 = $0.01728
  Fee_NO  = 0.072 × 0.38 × 0.62 = $0.01697
  Net = $0.020 - $0.01728 - $0.01697 = -$0.014  ❌

Example B (marginally profitable):
  YES = $0.45, NO = $0.52
  Spread = 1.00 - 0.97 = $0.030  
  Fee_YES = 0.072 × 0.45 × 0.55 = $0.01782
  Fee_NO  = 0.072 × 0.52 × 0.48 = $0.01797
  Net = $0.030 - $0.01782 - $0.01797 = +$0.00421  ✓

Key: fees are QUADRATIC near 0.50 — hardest to profit from mid-priced markets`}</MathBlock>

            {/* Interactive Calculator */}
            <h3 style={{ fontSize: 15, color: C.text, marginBottom: 12 }}>
              Interactive Arbitrage Calculator
            </h3>
            <Card glow={C.cyan} style={{ marginBottom: 16 }}>
              <ArbCalculator />
            </Card>

            <InsightBox color={C.cyan}>
              Sub-$1 arbs are rare and last seconds. The scanner runs every 500ms. In calm markets,
              combined prices stay pinned at $1.00. During flash crashes, BTC news, or cascade events,
              market makers pull quotes and gaps appear. The window to execute is typically 200–800ms before
              arbitrageurs close the gap.
            </InsightBox>
          </section>

          {/* ════════════════════════════════════════════════════════ */}
          {/* § STRATEGY 2: VPIN CASCADE */}
          {/* ════════════════════════════════════════════════════════ */}
          <SectionAnchor id="vpin-cascade" />
          <section style={{ marginBottom: 64 }}>
            <SectionHeader color={C.purple}>Strategy 2: VPIN Cascade</SectionHeader>

            <Card style={{ marginBottom: 20, borderColor: `${C.purple}22` }}>
              <h3 style={{ margin: '0 0 10px', fontSize: 15, color: C.purple }}>The Thesis</h3>
              <p style={{ margin: 0, color: 'rgba(255,255,255,0.7)', fontSize: 13, lineHeight: 1.8 }}>
                When BTC dumps hard, leveraged traders get liquidated. These forced sells push price lower,
                triggering more liquidations — a <strong style={{ color: C.red }}>cascade</strong>. Each cascade
                exhausts selling pressure. Once it stops, price tends to{' '}
                <strong style={{ color: C.green }}>mean-revert</strong>. We detect the exhaustion point and bet on the bounce.
              </p>
            </Card>

            {/* Steps */}
            {[
              {
                n: 1, color: C.amber,
                title: 'VPIN detects elevated informed (toxic) order flow',
                desc: 'VPIN measures how imbalanced buy/sell flow is. When informed traders (who know something) dominate, imbalance spikes. VPIN > 0.70 → cascade threshold breached.',
                canvas: <VPINLineChart height={100} />,
              },
              {
                n: 2, color: C.red,
                title: 'Binance forceOrder events confirm liquidations',
                desc: 'We subscribe to Binance\'s forceOrder websocket stream. Each event is a forced liquidation. We track volume in USD per minute. When volume > min_liq_usd → cascade confirmed.',
                canvas: null,
              },
              {
                n: 3, color: '#f97316',
                title: 'Watch for deceleration — the cascade exhausts itself',
                desc: 'Peak liquidation intensity is followed by deceleration. When the rolling liquidation rate drops by > cascade_decel_threshold (30%), the cascade is exhausting. Selling pressure is running out.',
                canvas: null,
              },
              {
                n: 4, color: C.green,
                title: 'Place mean-reversion bet — price will bounce',
                desc: 'Once the cascade exhausts, we place a BTC price bounce bet on Polymarket or Opinion Exchange. The market has already priced in the dump — now fear is priced in, creating value on the YES side.',
                canvas: null,
              },
            ].map(step => (
              <Card key={step.n} style={{ marginBottom: 12 }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
                  <div style={{
                    width: 28, height: 28, borderRadius: '50%', flexShrink: 0,
                    background: `${step.color}22`, border: `1px solid ${step.color}55`,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontWeight: 700, fontSize: 13, color: step.color,
                  }}>{step.n}</div>
                  <div style={{ flex: 1 }}>
                    <h4 style={{ margin: '0 0 6px', fontSize: 13, color: step.color }}>{step.title}</h4>
                    <p style={{ margin: step.canvas ? '0 0 12px' : 0, color: C.muted, fontSize: 12, lineHeight: 1.7 }}>
                      {step.desc}
                    </p>
                    {step.canvas}
                  </div>
                </div>
              </Card>
            ))}
          </section>

          {/* ════════════════════════════════════════════════════════ */}
          {/* § VPIN EXPLAINED */}
          {/* ════════════════════════════════════════════════════════ */}
          <SectionAnchor id="vpin-explained" />
          <section style={{ marginBottom: 64 }}>
            <SectionHeader color={C.amber}>VPIN Explained</SectionHeader>

            <Card style={{ marginBottom: 20 }}>
              <h3 style={{ margin: '0 0 10px', fontSize: 15, color: C.amber }}>
                Volume-synchronized Probability of Informed Trading
              </h3>
              <p style={{ margin: 0, color: 'rgba(255,255,255,0.7)', fontSize: 13, lineHeight: 1.8 }}>
                VPIN measures <strong style={{ color: C.text }}>how much of trading volume comes from informed traders</strong> vs noise traders.
                Informed traders (institutions, algos with edge) trade directionally and aggressively.
                Noise traders trade randomly. When informed traders dominate, the order flow becomes
                imbalanced — more buys or more sells — and VPIN spikes. This precedes price impact.
              </p>
            </Card>

            <h3 style={{ fontSize: 14, color: C.text, marginBottom: 8 }}>The Algorithm</h3>
            <MathBlock>{`1. Stream BTC/USDT trades from Binance websocket

2. Group trades into fixed-USD-volume "buckets" ($50,000 each):
   - Each bucket fills until cumulative notional ≥ bucket_size_usd
   
3. Classify each trade direction:
   - is_buyer_maker = true  → aggressive SELLER (sell-side pressure)
   - is_buyer_maker = false → aggressive BUYER  (buy-side pressure)
   
4. For each completed bucket:
   bucket_imbalance = |buy_volume - sell_volume| / total_volume
   Range: 0.00 (perfectly balanced) → 1.00 (all one side)
   
5. VPIN = rolling mean of last N bucket imbalances
   VPIN = (1/N) × Σ bucket_imbalance[i]  for i in [t-N, t]
   
   Default N = 50 buckets (vpin_window)`}</MathBlock>

            <h3 style={{ fontSize: 14, color: C.text, marginBottom: 12 }}>Live Bucket Visualisation</h3>
            <Card style={{ marginBottom: 16 }}>
              <VPINBuckets />
              <p style={{ margin: '10px 0 0', color: C.muted, fontSize: 11, textAlign: 'center' }}>
                Green = buy volume, Red = sell volume. % shows bucket imbalance. VPIN = mean of all. Animating.
              </p>
            </Card>

            <h3 style={{ fontSize: 14, color: C.text, marginBottom: 12 }}>Threshold Zones</h3>
            <Card style={{ marginBottom: 16 }}>
              <VPINZones current={0.72} />
            </Card>

            <h3 style={{ fontSize: 14, color: C.text, marginBottom: 12 }}>Live VPIN Reading</h3>
            <Card glow={C.amber}>
              <LiveVPINSection />
            </Card>
          </section>

          {/* ════════════════════════════════════════════════════════ */}
          {/* § CASCADE STATE MACHINE */}
          {/* ════════════════════════════════════════════════════════ */}
          <SectionAnchor id="cascade-fsm" />
          <section style={{ marginBottom: 64 }}>
            <SectionHeader color={C.purple}>The Cascade State Machine</SectionHeader>

            <p style={{ color: C.muted, fontSize: 13, lineHeight: 1.7, marginBottom: 20 }}>
              The cascade detector is a finite state machine (FSM) that transitions through 5 states.
              It can only move forward through the pipeline — no shortcuts. Click any state box to see
              details. The machine auto-animates to show current state flow.
            </p>

            <Card>
              <CascadeFSM />
            </Card>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10, marginTop: 16 }} className="learn-5col">
              {CASCADE_STATES.map(s => (
                <div key={s.id} style={{
                  background: `${s.color}0c`, border: `1px solid ${s.color}33`,
                  borderRadius: 8, padding: '10px 12px',
                }}>
                  <div style={{ color: s.color, fontWeight: 700, fontSize: 11, marginBottom: 6 }}>{s.id}</div>
                  <div style={{ color: C.muted, fontSize: 10, lineHeight: 1.6 }}>{s.doing}</div>
                </div>
              ))}
            </div>
          </section>

          {/* ════════════════════════════════════════════════════════ */}
          {/* § RISK MANAGEMENT */}
          {/* ════════════════════════════════════════════════════════ */}
          <SectionAnchor id="risk" />
          <section style={{ marginBottom: 64 }}>
            <SectionHeader color={C.red}>Risk Management</SectionHeader>

            {/* Kill Switch */}
            <Card glow={C.red} style={{ marginBottom: 16, borderColor: `${C.red}22` }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <span style={{ fontSize: 20 }}>⚡</span>
                <h3 style={{ margin: 0, fontSize: 15, color: C.red }}>Kill Switch</h3>
              </div>
              <p style={{ margin: '0 0 12px', color: 'rgba(255,255,255,0.7)', fontSize: 12, lineHeight: 1.7 }}>
                If your balance drops below the kill switch threshold, <strong style={{ color: C.red }}>ALL trading stops immediately</strong>.
                No new positions. No new signals. You must manually restart and review what happened.
              </p>
              <MathBlock>{`kill_threshold = starting_bankroll × (1 - max_drawdown_pct)

Example: $25 bankroll, 10% max drawdown
kill_threshold = $25 × (1 - 0.10) = $22.50

If balance < $22.50 → TRADING HALTED ⚡`}</MathBlock>
              <EquityCurveCanvas bankroll={25} drawdownPct={0.10} />
            </Card>

            {/* Position sizing */}
            <Card style={{ marginBottom: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
                <span style={{ fontSize: 18 }}>📐</span>
                <h3 style={{ margin: 0, fontSize: 15 }}>Position Sizing</h3>
              </div>
              <p style={{ margin: '0 0 10px', color: C.muted, fontSize: 12, lineHeight: 1.7 }}>
                Every trade stakes a fixed fraction of current bankroll. As bankroll grows, stakes grow.
                As bankroll shrinks (after losses), stakes automatically shrink — a form of{' '}
                <strong style={{ color: C.text }}>Kelly-adjacent risk management</strong>.
              </p>
              <MathBlock>{`stake = bankroll × bet_fraction

Example: $25 bankroll, bet_fraction = 0.05
stake = $25 × 0.05 = $1.25 per trade

After 3 winning trades (+$0.50 each): $26.50
stake = $26.50 × 0.05 = $1.325 (auto-adjusts up)

After loss to $24: stake = $24 × 0.05 = $1.20 (auto-adjusts down)`}</MathBlock>
            </Card>

            {/* Other guards */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }} className="learn-2col">
              <Card>
                <div style={{ display: 'flex', gap: 10, marginBottom: 8 }}>
                  <span>📅</span>
                  <h4 style={{ margin: 0, fontSize: 13, color: C.amber }}>Daily Loss Limit</h4>
                </div>
                <p style={{ margin: 0, color: C.muted, fontSize: 12, lineHeight: 1.7 }}>
                  After losing <code style={{ color: C.cyan }}>daily_loss_limit_pct</code> of bankroll in one calendar day,
                  trading <strong style={{ color: C.text }}>pauses until UTC midnight</strong>. Prevents a single bad day
                  from compounding losses.
                </p>
              </Card>
              <Card>
                <div style={{ display: 'flex', gap: 10, marginBottom: 8 }}>
                  <span>⏱️</span>
                  <h4 style={{ margin: 0, fontSize: 13, color: C.purple }}>Streak Cooldown</h4>
                </div>
                <p style={{ margin: 0, color: C.muted, fontSize: 12, lineHeight: 1.7 }}>
                  After <code style={{ color: C.cyan }}>consecutive_loss_limit</code> (default: 3) consecutive losses,
                  trading pauses for <code style={{ color: C.cyan }}>cooldown_minutes</code> (default: 15min).
                  Prevents panic trading in adverse conditions.
                </p>
              </Card>
            </div>
          </section>

          {/* ════════════════════════════════════════════════════════ */}
          {/* § THRESHOLDS & CONFIG */}
          {/* ════════════════════════════════════════════════════════ */}
          <SectionAnchor id="thresholds" />
          <section style={{ marginBottom: 64 }}>
            <SectionHeader color={C.cyan}>Thresholds & Config</SectionHeader>

            <p style={{ color: C.muted, fontSize: 13, lineHeight: 1.7, marginBottom: 20 }}>
              All {CONFIG_VARS.length} configurable variables with their defaults, valid ranges, and impact.
              The bar shows where the default sits within its range.
            </p>

            <div style={{ overflowX: 'auto', borderRadius: 12, border: `1px solid ${C.border}` }}>
              <table style={{
                width: '100%', borderCollapse: 'collapse',
                fontFamily: MONO, fontSize: 12,
              }}>
                <thead>
                  <tr style={{ background: 'rgba(255,255,255,0.03)', borderBottom: `1px solid ${C.border}` }}>
                    {['Variable', 'Default', 'Range', 'What it does', 'Impact of changing'].map(h => (
                      <th key={h} style={{
                        padding: '10px 12px', textAlign: 'left',
                        color: C.muted, fontSize: 10, letterSpacing: '0.1em',
                        fontWeight: 600, whiteSpace: 'nowrap',
                      }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {CONFIG_VARS.map(v => <ThresholdBar key={v.name} variable={v} />)}
                </tbody>
              </table>
            </div>
          </section>

          {/* ════════════════════════════════════════════════════════ */}
          {/* § MARKETS & VENUES */}
          {/* ════════════════════════════════════════════════════════ */}
          <SectionAnchor id="venues" />
          <section style={{ marginBottom: 64 }}>
            <SectionHeader color={C.amber}>Markets & Venues</SectionHeader>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 20 }} className="learn-2col">
              <Card glow={C.purple} style={{ borderColor: `${C.purple}22` }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
                  <span style={{ fontSize: 22 }}>🔷</span>
                  <h3 style={{ margin: 0, fontSize: 16, color: C.purple }}>Polymarket</h3>
                </div>
                {[
                  ['Chain', 'Polygon (MATIC)'],
                  ['Settlement', 'USDC'],
                  ['Fee multiplier', '0.072 (7.2% round-trip)'],
                  ['Liquidity', '★★★★★ Deep'],
                  ['Markets', '200+ active'],
                  ['API', 'CLOB REST + WS'],
                  ['Auth', 'Proxy wallet + API key'],
                ].map(([k, v]) => (
                  <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '5px 0', borderBottom: `1px solid ${C.border}` }}>
                    <span style={{ color: C.muted, fontSize: 11 }}>{k}</span>
                    <span style={{ color: C.text, fontSize: 11, fontFamily: MONO }}>{v}</span>
                  </div>
                ))}
              </Card>

              <Card glow={C.amber} style={{ borderColor: `${C.amber}22` }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
                  <span style={{ fontSize: 22 }}>🟡</span>
                  <h3 style={{ margin: 0, fontSize: 16, color: C.amber }}>Opinion Exchange</h3>
                </div>
                {[
                  ['Chain', 'BNB Chain'],
                  ['Settlement', 'USDT'],
                  ['Fee multiplier', '0.04 (4.0% round-trip)'],
                  ['Liquidity', '★★★ Moderate'],
                  ['Markets', '50+ active'],
                  ['API', 'REST HTTP'],
                  ['Auth', 'API key + secret'],
                ].map(([k, v]) => (
                  <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '5px 0', borderBottom: `1px solid ${C.border}` }}>
                    <span style={{ color: C.muted, fontSize: 11 }}>{k}</span>
                    <span style={{ color: C.text, fontSize: 11, fontFamily: MONO }}>{v}</span>
                  </div>
                ))}
              </Card>
            </div>

            <h3 style={{ fontSize: 14, color: C.text, marginBottom: 12 }}>Side-by-Side Comparison</h3>
            <Card style={{ marginBottom: 16 }}>
              <VenueComparison />
            </Card>

            <InsightBox color={C.amber}>
              Opinion Exchange is preferred for Sub-$1 arb due to lower fees — the edge is small, so every
              basis point matters. Polymarket is preferred for VPIN cascade bets because its deeper liquidity
              means larger positions can be filled without slippage.
            </InsightBox>
          </section>

          {/* ════════════════════════════════════════════════════════ */}
          {/* § FEE STRUCTURE */}
          {/* ════════════════════════════════════════════════════════ */}
          <SectionAnchor id="fees" />
          <section style={{ marginBottom: 64 }}>
            <SectionHeader color={C.green}>Fee Structure</SectionHeader>

            <Card style={{ marginBottom: 20 }}>
              <h3 style={{ margin: '0 0 8px', fontSize: 15, color: C.green }}>Per-Leg Fee Formula</h3>
              <MathBlock>{`fee = fee_multiplier × price × (1 - price)

This is a QUADRATIC formula — highest fees near p=0.50, lowest near p=0.00 or p=1.00

Why? Market makers face maximum adverse selection risk when outcome is most uncertain (p≈0.50).
Near p=0.00 or p=1.00, the outcome is more certain — lower risk, lower fee.

Polymarket: fee = 0.072 × p × (1 - p)   → max $0.018 at p=0.50
Opinion:    fee = 0.040 × p × (1 - p)   → max $0.010 at p=0.50

Round-trip (both sides): 2× the above`}</MathBlock>
            </Card>

            <h3 style={{ fontSize: 14, color: C.text, marginBottom: 12 }}>Fee Curve — Interactive</h3>
            <Card glow={C.green} style={{ marginBottom: 16 }}>
              <FeeCurveCanvas />
            </Card>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }} className="learn-2col">
              <Card>
                <h4 style={{ margin: '0 0 10px', color: C.purple, fontSize: 13 }}>Fee at common prices</h4>
                {[0.10, 0.25, 0.40, 0.50, 0.60, 0.75, 0.90].map(p => {
                  const polyF = 0.072 * p * (1 - p);
                  const opinF = 0.040 * p * (1 - p);
                  return (
                    <div key={p} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '5px 0', borderBottom: `1px solid ${C.border}` }}>
                      <span style={{ color: C.amber, fontFamily: MONO, fontSize: 12 }}>p=${p.toFixed(2)}</span>
                      <span style={{ color: C.purple, fontFamily: MONO, fontSize: 11 }}>${polyF.toFixed(4)}</span>
                      <span style={{ color: C.cyan, fontFamily: MONO, fontSize: 11 }}>${opinF.toFixed(4)}</span>
                    </div>
                  );
                })}
                <div style={{ display: 'flex', justifyContent: 'space-between', paddingTop: 6 }}>
                  <span style={{ color: C.muted, fontSize: 9 }}>price</span>
                  <span style={{ color: C.purple, fontSize: 9 }}>Polymarket</span>
                  <span style={{ color: C.cyan, fontSize: 9 }}>Opinion</span>
                </div>
              </Card>

              <Card>
                <h4 style={{ margin: '0 0 10px', color: C.green, fontSize: 13 }}>Break-even spreads</h4>
                <p style={{ color: C.muted, fontSize: 12, lineHeight: 1.7, marginBottom: 10 }}>
                  Minimum spread needed to profit after fees (at p=0.50, worst case):
                </p>
                <MathBlock>{`Polymarket:
  fee_YES = 0.072 × 0.50 × 0.50 = $0.018
  fee_NO  = 0.072 × 0.50 × 0.50 = $0.018
  min_spread = $0.036 (3.6%)

Opinion:
  fee_YES = 0.040 × 0.50 × 0.50 = $0.010  
  fee_NO  = 0.040 × 0.50 × 0.50 = $0.010
  min_spread = $0.020 (2.0%)

Near extremes (p=0.10):
  Polymarket min_spread = $0.013 (1.3%)
  Opinion min_spread = $0.007 (0.7%)`}</MathBlock>
                <InsightBox color={C.green}>
                  Arbs near extreme prices (p near 0 or 1) are <strong>easier to profit from</strong> due to lower fees.
                  A mid-priced market needs a much wider spread.
                </InsightBox>
              </Card>
            </div>
          </section>

          {/* ════════════════════════════════════════════════════════ */}
          {/* § PAPER VS LIVE */}
          {/* ════════════════════════════════════════════════════════ */}
          <SectionAnchor id="paper-vs-live" />
          <section style={{ marginBottom: 64 }}>
            <SectionHeader color={C.cyan}>Paper vs Live Trading</SectionHeader>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 20 }} className="learn-2col">
              <Card>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                  <Badge color={C.cyan}>PAPER</Badge>
                  <h3 style={{ margin: 0, fontSize: 15 }}>Simulation Mode</h3>
                </div>
                {[
                  'Runs all strategies with virtual capital',
                  'Realistic slippage and fee simulation',
                  'No real money at risk',
                  'Full P&L tracking and analytics',
                  'Can run simultaneously with live mode',
                  'Default mode — always safe to enable',
                ].map((item, i) => (
                  <div key={i} style={{ display: 'flex', gap: 8, padding: '5px 0', color: C.muted, fontSize: 12 }}>
                    <span style={{ color: C.cyan }}>✓</span> {item}
                  </div>
                ))}
              </Card>

              <Card glow={C.red} style={{ borderColor: `${C.red}22` }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                  <Badge color={C.red}>LIVE</Badge>
                  <h3 style={{ margin: 0, fontSize: 15 }}>Real Money</h3>
                </div>
                {[
                  'Executes real trades on Polymarket/Opinion',
                  'Requires approved config version',
                  'Requires manual confirmation toggle',
                  'Kill switch active — auto-halts on drawdown',
                  'All trades logged with full audit trail',
                  'Can run alongside paper for comparison',
                ].map((item, i) => (
                  <div key={i} style={{ display: 'flex', gap: 8, padding: '5px 0', color: C.muted, fontSize: 12 }}>
                    <span style={{ color: C.red }}>⚡</span> {item}
                  </div>
                ))}
              </Card>
            </div>

            <h3 style={{ fontSize: 14, color: C.text, marginBottom: 12 }}>Promoting Paper → Live</h3>
            <Card style={{ marginBottom: 16 }}>
              <MathBlock>{`Paper → Live promotion process:

1. Create config (Config page) → saves as draft
2. Run in paper mode → observe 7+ days of signals
3. Review P&L analytics → confirm edge is real
4. Config must be "approved" → set approved=true via API
5. Toggle LIVE mode on (top-right header toggle)
6. System confirms: "Live mode requires approved config"
7. First live trade executes with starting_bankroll capital
8. Kill switch becomes active immediately

You can run paper and live simultaneously:
  - Live config: uses real money, strict risk limits
  - Paper config: experiments with new settings
  - Compare P&L side-by-side on Paper Trading page`}</MathBlock>
            </Card>

            <InsightBox color={C.red}>
              Never promote a config to live without at least 50 paper signals. The system's edge is
              statistically small — you need enough samples to see it above noise. A config that looks
              profitable after 10 trades is almost certainly noise.
            </InsightBox>

            <h3 style={{ fontSize: 14, color: C.text, margin: '20px 0 12px' }}>Slippage Simulation</h3>
            <Card>
              <p style={{ margin: '0 0 10px', color: C.muted, fontSize: 12, lineHeight: 1.7 }}>
                Paper mode applies realistic slippage to approximate live execution:
              </p>
              <MathBlock>{`For arb bets:
  effective_yes_price = yes_price × (1 + slippage_factor)
  effective_no_price  = no_price  × (1 + slippage_factor)
  Default slippage_factor = 0.002 (0.2% per leg)

For cascade bets:
  effective_price = market_price × (1 + directional_slippage)
  Default directional_slippage = 0.005 (0.5%)