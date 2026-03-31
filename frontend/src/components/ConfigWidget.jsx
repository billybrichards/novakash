import React, { useRef, useEffect, useCallback, useState } from 'react';

/**
 * ConfigWidget — Reusable interactive canvas visualization widget.
 *
 * Props:
 *   type        — 'equity_projection' | 'bet_size_bars' | 'drawdown_line' |
 *                 'vpin_histogram' | 'spread_scale' | 'fee_comparison'
 *   value       — current numeric value (threshold, fraction, etc.)
 *   onChange    — optional (val) => void for draggable widgets
 *   config      — { starting_bankroll, bet_fraction, ... }
 *   vpinHistory — array of VPIN values (0–1)
 *   label       — optional label for the widget
 *   height      — canvas height (default depends on type)
 *   color       — accent color override
 */
export default function ConfigWidget({
  type,
  value,
  onChange,
  config = {},
  vpinHistory = [],
  label,
  height: heightProp,
  color: colorProp,
}) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const [containerWidth, setContainerWidth] = useState(300);
  const isDragging = useRef(false);

  // ── Default heights per type ─────────────────────────────────────────────
  const DEFAULT_HEIGHTS = {
    equity_projection: 80,
    bet_size_bars: 80,
    drawdown_line: 110,
    vpin_histogram: 100,
    spread_scale: 70,
    fee_comparison: 100,
  };
  const height = heightProp ?? DEFAULT_HEIGHTS[type] ?? 80;

  // ── Observe container width ──────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver(entries => {
      for (const entry of entries) {
        const w = Math.floor(entry.contentRect.width);
        if (w > 0) setContainerWidth(w);
      }
    });
    ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, []);

  // ── HiDPI canvas setup ───────────────────────────────────────────────────
  const setupCanvas = useCallback((canvas, w, h) => {
    if (!canvas) return null;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    return ctx;
  }, []);

  // ── Draw: equity_projection ──────────────────────────────────────────────
  const drawEquityProjection = useCallback((canvas, w, h) => {
    const ctx = setupCanvas(canvas, w, h);
    if (!ctx) return;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#08080e';
    ctx.fillRect(0, 0, w, h);

    const bankroll = parseFloat(config.starting_bankroll) || 25;
    const fraction = parseFloat(config.bet_fraction) || 0.025;
    const winRate = 0.55;
    const numTrades = 100;

    // Seed-based pseudo-random for stable render
    let seed = Math.floor(bankroll * 100 + fraction * 10000);
    const rand = () => {
      seed = (seed * 9301 + 49297) % 233280;
      return seed / 233280;
    };

    const equity = [bankroll];
    for (let i = 1; i < numTrades; i++) {
      const prev = equity[i - 1];
      const win = rand() < winRate;
      const change = win ? prev * fraction * 1.9 : -prev * fraction;
      equity.push(Math.max(0.01, prev + change));
    }

    const minE = Math.min(...equity) * 0.97;
    const maxE = Math.max(...equity) * 1.03;
    const range = maxE - minE || 1;
    const pad = { t: 8, b: 8 };
    const drawH = h - pad.t - pad.b;

    const toY = (v) => pad.t + drawH - ((v - minE) / range) * drawH;
    const toX = (i) => (i / (equity.length - 1)) * (w - 2) + 1;

    // Gradient fill
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, 'rgba(168,85,247,0.25)');
    grad.addColorStop(1, 'rgba(168,85,247,0)');
    ctx.beginPath();
    ctx.moveTo(toX(0), toY(equity[0]));
    for (let i = 1; i < equity.length; i++) ctx.lineTo(toX(i), toY(equity[i]));
    ctx.lineTo(toX(equity.length - 1), h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.moveTo(toX(0), toY(equity[0]));
    for (let i = 1; i < equity.length; i++) ctx.lineTo(toX(i), toY(equity[i]));
    ctx.strokeStyle = '#a855f7';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Start / end labels
    ctx.font = '9px IBM Plex Mono';
    ctx.fillStyle = 'rgba(255,255,255,0.3)';
    ctx.fillText(`$${bankroll.toFixed(0)}`, 3, h - 4);
    const endVal = equity[equity.length - 1];
    const endColor = endVal >= bankroll ? '#4ade80' : '#f87171';
    ctx.fillStyle = endColor;
    ctx.textAlign = 'right';
    ctx.fillText(`$${endVal.toFixed(0)}`, w - 3, 14);
    ctx.textAlign = 'left';
  }, [config.starting_bankroll, config.bet_fraction, setupCanvas]);

  // ── Draw: bet_size_bars ──────────────────────────────────────────────────
  const drawBetSizeBars = useCallback((canvas, w, h) => {
    const ctx = setupCanvas(canvas, w, h);
    if (!ctx) return;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#08080e';
    ctx.fillRect(0, 0, w, h);

    const bankroll = parseFloat(config.starting_bankroll) || 25;
    const currentFraction = parseFloat(value) || 0.025;
    // Kelly optimal for 55% WR, ~1.9:1 odds
    const kellyOptimal = (0.55 * 1.9 - 0.45) / 1.9; // ≈ 0.313 → clip
    const kellyClipped = Math.min(kellyOptimal * 0.25, 0.20); // quarter-Kelly safety

    const fractions = [0.01, 0.025, 0.05, 0.10, 0.15, 0.20];
    const barPad = 6;
    const barW = (w - barPad * (fractions.length + 1)) / fractions.length;
    const maxAmount = bankroll * 0.20;
    const pad = { t: 18, b: 20 };
    const drawH = h - pad.t - pad.b;

    fractions.forEach((f, i) => {
      const amount = bankroll * f;
      const barH = (amount / maxAmount) * drawH;
      const x = barPad + i * (barW + barPad);
      const y = pad.t + drawH - barH;

      const isCurrent = Math.abs(f - currentFraction) < 0.001;
      const isKelly = Math.abs(f - kellyClipped) < 0.005;

      ctx.fillStyle = isCurrent
        ? 'rgba(168,85,247,0.8)'
        : f > kellyClipped
          ? 'rgba(248,113,113,0.35)'
          : 'rgba(255,255,255,0.12)';
      ctx.fillRect(x, y, barW, barH);

      // Kelly star marker
      if (isKelly) {
        ctx.fillStyle = '#f59e0b';
        ctx.font = '9px serif';
        ctx.textAlign = 'center';
        ctx.fillText('★', x + barW / 2, y - 3);
      }

      // Current highlight glow
      if (isCurrent) {
        ctx.shadowBlur = 8;
        ctx.shadowColor = '#a855f7';
        ctx.fillStyle = 'rgba(168,85,247,0.8)';
        ctx.fillRect(x, y, barW, barH);
        ctx.shadowBlur = 0;
      }

      // Label at bottom
      ctx.fillStyle = isCurrent ? '#a855f7' : 'rgba(255,255,255,0.25)';
      ctx.font = '8px IBM Plex Mono';
      ctx.textAlign = 'center';
      ctx.fillText(`${(f * 100).toFixed(0)}%`, x + barW / 2, h - 6);

      // Amount at top of bar
      if (isCurrent) {
        ctx.fillStyle = '#a855f7';
        ctx.font = '8px IBM Plex Mono';
        ctx.fillText(`$${amount.toFixed(2)}`, x + barW / 2, y - (isKelly ? 13 : 3));
      }
    });

    ctx.textAlign = 'left';
  }, [value, config.starting_bankroll, setupCanvas]);

  // ── Draw: drawdown_line ──────────────────────────────────────────────────
  const drawDrawdownLine = useCallback((canvas, w, h) => {
    const ctx = setupCanvas(canvas, w, h);
    if (!ctx) return;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#08080e';
    ctx.fillRect(0, 0, w, h);

    const drawdownPct = parseFloat(value) || 0.10;
    const bankroll = parseFloat(config.starting_bankroll) || 100;
    const numPoints = 80;

    // Deterministic equity curve
    let seed = 42;
    const rand = () => { seed = (seed * 9301 + 49297) % 233280; return seed / 233280; };

    const equity = [bankroll];
    for (let i = 1; i < numPoints; i++) {
      const prev = equity[i - 1];
      const win = rand() < 0.52;
      equity.push(Math.max(0.01, prev + (win ? prev * 0.03 : -prev * 0.05)));
    }

    const maxE = Math.max(...equity);
    const killLevel = bankroll * (1 - drawdownPct);
    const minE = Math.min(killLevel * 0.9, Math.min(...equity));
    const range = maxE - minE || 1;
    const pad = { t: 14, b: 10 };
    const drawH = h - pad.t - pad.b;

    const toY = (v) => pad.t + drawH - ((v - minE) / range) * drawH;
    const toX = (i) => (i / (numPoints - 1)) * (w - 2) + 1;

    // Gradient fill (green above kill, red below)
    const killY = toY(killLevel);
    const gradFill = ctx.createLinearGradient(0, 0, 0, h);
    gradFill.addColorStop(0, 'rgba(74,222,128,0.2)');
    gradFill.addColorStop(1, 'rgba(74,222,128,0)');
    ctx.beginPath();
    ctx.moveTo(toX(0), toY(equity[0]));
    for (let i = 1; i < equity.length; i++) ctx.lineTo(toX(i), toY(equity[i]));
    ctx.lineTo(toX(equity.length - 1), h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fillStyle = gradFill;
    ctx.fill();

    // Equity line
    ctx.beginPath();
    ctx.moveTo(toX(0), toY(equity[0]));
    for (let i = 1; i < equity.length; i++) ctx.lineTo(toX(i), toY(equity[i]));
    ctx.strokeStyle = '#4ade80';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Kill-switch dashed red line
    ctx.beginPath();
    ctx.moveTo(1, killY);
    ctx.lineTo(w - 1, killY);
    ctx.strokeStyle = 'rgba(248,113,113,0.9)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.stroke();
    ctx.setLineDash([]);

    // Red zone fill below kill line
    ctx.fillStyle = 'rgba(248,113,113,0.06)';
    ctx.fillRect(0, killY, w, h - killY);

    // Kill label
    ctx.fillStyle = '#f87171';
    ctx.font = 'bold 9px IBM Plex Mono';
    ctx.fillText(`KILL ${(drawdownPct * 100).toFixed(0)}% — $${killLevel.toFixed(0)}`, 4, killY - 3);

    // Starting bankroll line (faint)
    const startY = toY(bankroll);
    ctx.beginPath();
    ctx.moveTo(1, startY);
    ctx.lineTo(w - 1, startY);
    ctx.strokeStyle = 'rgba(255,255,255,0.1)';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.stroke();
    ctx.setLineDash([]);
  }, [value, config.starting_bankroll, setupCanvas]);

  // ── Draw: vpin_histogram ─────────────────────────────────────────────────
  const drawVpinHistogram = useCallback((canvas, w, h) => {
    const ctx = setupCanvas(canvas, w, h);
    if (!ctx) return;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#08080e';
    ctx.fillRect(0, 0, w, h);

    const threshold = parseFloat(value) || 0.55;
    const accent = colorProp || '#f59e0b';

    const data = vpinHistory.length > 0
      ? vpinHistory
      : Array.from({ length: 300 }, (_, idx) => {
          // Simulated beta-ish distribution
          const u = (Math.sin(idx * 1.37 + 0.5) + 1) / 2;
          return 0.15 + u * 0.70;
        });

    const bins = 24;
    const counts = new Array(bins).fill(0);
    data.forEach(v => {
      const bin = Math.min(bins - 1, Math.floor(v * bins));
      counts[bin]++;
    });

    const maxCount = Math.max(...counts, 1);
    const barW = w / bins;
    const pad = { t: 6, b: 20 };
    const drawH = h - pad.t - pad.b;

    counts.forEach((count, i) => {
      const x = i * barW;
      const bh = (count / maxCount) * drawH;
      const binMid = (i + 0.5) / bins;
      const isAbove = binMid >= threshold;

      ctx.fillStyle = isAbove ? `${accent}55` : 'rgba(255,255,255,0.08)';
      ctx.fillRect(x + 0.5, pad.t + drawH - bh, barW - 1, bh);
    });

    // Baseline
    ctx.fillStyle = 'rgba(255,255,255,0.12)';
    ctx.fillRect(0, pad.t + drawH, w, 1);

    // Threshold vertical line
    const lineX = threshold * w;
    ctx.beginPath();
    ctx.moveTo(lineX, 0);
    ctx.lineTo(lineX, pad.t + drawH + 2);
    ctx.strokeStyle = accent;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Triangle indicator at bottom
    ctx.beginPath();
    ctx.moveTo(lineX - 4, pad.t + drawH + 8);
    ctx.lineTo(lineX + 4, pad.t + drawH + 8);
    ctx.lineTo(lineX, pad.t + drawH + 3);
    ctx.closePath();
    ctx.fillStyle = accent;
    ctx.fill();

    // Stats text
    const aboveCount = data.filter(v => v >= threshold).length;
    const abovePct = ((aboveCount / data.length) * 100).toFixed(0);
    ctx.font = '9px IBM Plex Mono';
    ctx.fillStyle = accent;
    const textX = lineX > w * 0.6 ? lineX - 4 : lineX + 4;
    ctx.textAlign = lineX > w * 0.6 ? 'right' : 'left';
    ctx.fillText(`${abovePct}% above`, textX, 14);

    // Threshold value label at bottom
    ctx.textAlign = lineX > w * 0.6 ? 'right' : 'left';
    ctx.fillStyle = accent;
    ctx.font = 'bold 9px IBM Plex Mono';
    ctx.fillText(threshold.toFixed(2), textX, h - 4);

    ctx.textAlign = 'left';
  }, [value, vpinHistory, colorProp, setupCanvas]);

  // ── Draw: spread_scale ───────────────────────────────────────────────────
  const drawSpreadScale = useCallback((canvas, w, h) => {
    const ctx = setupCanvas(canvas, w, h);
    if (!ctx) return;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#08080e';
    ctx.fillRect(0, 0, w, h);

    const threshold = parseFloat(value) || 0.015;
    const maxSpread = 0.06;
    const pad = { t: 14, b: 22 };
    const drawH = h - pad.t - pad.b;

    // Background gradient — green below threshold (arb profitable), grey above
    const triggerX = (threshold / maxSpread) * (w - 2) + 1;

    // Below threshold: green (arb opportunity zone)
    const greenGrad = ctx.createLinearGradient(0, 0, triggerX, 0);
    greenGrad.addColorStop(0, 'rgba(74,222,128,0.3)');
    greenGrad.addColorStop(1, 'rgba(74,222,128,0.05)');
    ctx.fillStyle = greenGrad;
    ctx.fillRect(1, pad.t, triggerX - 1, drawH);

    // Labels
    ctx.font = '9px IBM Plex Mono';
    ctx.fillStyle = '#4ade80';
    ctx.fillText('ARB ZONE', 4, pad.t + 12);

    ctx.fillStyle = 'rgba(255,255,255,0.2)';
    ctx.fillText('NO ARB', triggerX + 4, pad.t + 12);

    // Parity line ($1.00)
    const parityX = 0;
    ctx.fillStyle = 'rgba(255,255,255,0.15)';
    ctx.fillRect(0, pad.t + drawH / 2, w, 1);
    ctx.fillStyle = 'rgba(255,255,255,0.25)';
    ctx.fillText('$1.000', 3, pad.t + drawH / 2 - 2);

    // Trigger line
    ctx.beginPath();
    ctx.moveTo(triggerX, pad.t);
    ctx.lineTo(triggerX, pad.t + drawH);
    ctx.strokeStyle = '#06b6d4';
    ctx.lineWidth = 2;
    ctx.stroke();

    // Triangle at bottom
    ctx.beginPath();
    ctx.moveTo(triggerX - 4, pad.t + drawH + 7);
    ctx.lineTo(triggerX + 4, pad.t + drawH + 7);
    ctx.lineTo(triggerX, pad.t + drawH + 2);
    ctx.closePath();
    ctx.fillStyle = '#06b6d4';
    ctx.fill();

    // Axis ticks
    const ticks = [0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06];
    ticks.forEach(t => {
      const tx = (t / maxSpread) * (w - 2) + 1;
      ctx.fillStyle = 'rgba(255,255,255,0.15)';
      ctx.fillRect(tx, pad.t + drawH, 1, 4);
      ctx.fillStyle = 'rgba(255,255,255,0.25)';
      ctx.font = '8px IBM Plex Mono';
      ctx.textAlign = 'center';
      ctx.fillText(`${(t * 100).toFixed(0)}%`, tx, h - 4);
    });

    // Threshold label
    ctx.textAlign = triggerX > w * 0.6 ? 'right' : 'left';
    ctx.fillStyle = '#06b6d4';
    ctx.font = 'bold 9px IBM Plex Mono';
    const lx = triggerX > w * 0.6 ? triggerX - 4 : triggerX + 4;
    ctx.fillText(`min ${(threshold * 100).toFixed(1)}%`, lx, pad.t + drawH - 4);

    ctx.textAlign = 'left';
  }, [value, setupCanvas]);

  // ── Draw: fee_comparison ─────────────────────────────────────────────────
  const drawFeeComparison = useCallback((canvas, w, h) => {
    const ctx = setupCanvas(canvas, w, h);
    if (!ctx) return;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#08080e';
    ctx.fillRect(0, 0, w, h);

    const venues = [
      { name: 'Opinion', perLeg: 0.010, color: '#a855f7' },
      { name: 'Polymarket', perLeg: 0.018, color: '#06b6d4' },
    ];

    const colW = w / 2;
    const tradeSize = 10;
    const maxFee = 0.10;
    const pad = { t: 16, b: 28 };
    const drawH = h - pad.t - pad.b;

    venues.forEach((venue, i) => {
      const x = i * colW;
      const roundTrip = venue.perLeg * 2;
      const fee = tradeSize * roundTrip;
      const barH = (roundTrip / maxFee) * drawH;
      const selected = value === venue.name.toLowerCase();

      // Bar
      ctx.fillStyle = selected ? `${venue.color}bb` : `${venue.color}44`;
      ctx.fillRect(x + colW * 0.2, pad.t + drawH - barH, colW * 0.6, barH);

      if (selected) {
        ctx.shadowBlur = 10;
        ctx.shadowColor = venue.color;
        ctx.fillStyle = `${venue.color}bb`;
        ctx.fillRect(x + colW * 0.2, pad.t + drawH - barH, colW * 0.6, barH);
        ctx.shadowBlur = 0;
      }

      // Name
      ctx.font = '9px IBM Plex Mono';
      ctx.textAlign = 'center';
      ctx.fillStyle = selected ? venue.color : `${venue.color}88`;
      ctx.fillText(venue.name, x + colW / 2, 12);

      // Fee value
      ctx.fillStyle = selected ? '#fff' : 'rgba(255,255,255,0.35)';
      ctx.font = selected ? 'bold 10px IBM Plex Mono' : '9px IBM Plex Mono';
      ctx.fillText(`${(roundTrip * 100).toFixed(1)}%`, x + colW / 2, pad.t + drawH - barH - 3);

      // Dollar cost
      ctx.fillStyle = selected ? venue.color : `${venue.color}77`;
      ctx.font = '8px IBM Plex Mono';
      ctx.fillText(`$${fee.toFixed(2)}/10`, x + colW / 2, h - 12);
      ctx.fillText('per $10', x + colW / 2, h - 4);
    });

    // Baseline
    ctx.fillStyle = 'rgba(255,255,255,0.12)';
    ctx.fillRect(0, pad.t + drawH, w, 1);

    ctx.textAlign = 'left';
  }, [value, setupCanvas]);

  // ── Trigger redraws ──────────────────────────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || containerWidth <= 0) return;

    const w = containerWidth;
    const h = height;

    switch (type) {
      case 'equity_projection':
        drawEquityProjection(canvas, w, h);
        break;
      case 'bet_size_bars':
        drawBetSizeBars(canvas, w, h);
        break;
      case 'drawdown_line':
        drawDrawdownLine(canvas, w, h);
        break;
      case 'vpin_histogram':
        drawVpinHistogram(canvas, w, h);
        break;
      case 'spread_scale':
        drawSpreadScale(canvas, w, h);
        break;
      case 'fee_comparison':
        drawFeeComparison(canvas, w, h);
        break;
      default:
        break;
    }
  }, [type, value, containerWidth, height, config, vpinHistory,
      drawEquityProjection, drawBetSizeBars, drawDrawdownLine,
      drawVpinHistogram, drawSpreadScale, drawFeeComparison]);

  // ── Draggable threshold (vpin_histogram, spread_scale) ───────────────────
  const handleMouseDown = useCallback((e) => {
    if (!onChange) return;
    if (type !== 'vpin_histogram' && type !== 'spread_scale' && type !== 'drawdown_line') return;
    isDragging.current = true;

    const rect = canvasRef.current.getBoundingClientRect();
    const handleMove = (ev) => {
      if (!isDragging.current) return;
      const clientX = ev.touches ? ev.touches[0].clientX : ev.clientX;
      const clientY = ev.touches ? ev.touches[0].clientY : ev.clientY;
      const x = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const y = Math.max(0, Math.min(1, (clientY - rect.top) / rect.height));

      if (type === 'vpin_histogram') {
        onChange(Math.round(x * 100) / 100);
      } else if (type === 'spread_scale') {
        onChange(Math.round(x * 0.06 * 1000) / 1000);
      } else if (type === 'drawdown_line') {
        // Y drag: top=50% drawdown, bottom=5%
        const drawdownPct = 0.05 + (1 - y) * 0.45;
        onChange(Math.round(drawdownPct * 100) / 100);
      }
    };

    const handleUp = () => {
      isDragging.current = false;
      window.removeEventListener('mousemove', handleMove);
      window.removeEventListener('mouseup', handleUp);
      window.removeEventListener('touchmove', handleMove);
      window.removeEventListener('touchend', handleUp);
    };

    window.addEventListener('mousemove', handleMove);
    window.addEventListener('mouseup', handleUp);
    window.addEventListener('touchmove', handleMove, { passive: true });
    window.addEventListener('touchend', handleUp);
  }, [type, onChange]);

  const isDraggable = onChange && (
    type === 'vpin_histogram' || type === 'spread_scale' || type === 'drawdown_line'
  );

  return (
    <div
      ref={containerRef}
      style={{
        width: '100%',
        borderRadius: 6,
        overflow: 'hidden',
        background: '#08080e',
        border: '1px solid rgba(255,255,255,0.04)',
        cursor: isDraggable ? 'ew-resize' : 'default',
        userSelect: 'none',
        touchAction: isDraggable ? 'none' : 'auto',
      }}
    >
      <canvas
        ref={canvasRef}
        style={{ display: 'block', width: '100%' }}
        onMouseDown={handleMouseDown}
        onTouchStart={handleMouseDown}
      />
    </div>
  );
}
