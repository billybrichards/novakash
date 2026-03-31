/**
 * Risk.jsx — Risk Management View
 *
 * Risk monitoring dashboard with:
 * - Current drawdown % with visual bar (red zone at 45%)
 * - Daily loss tracker (how close to 10% daily limit)
 * - Position concentration bars showing exposure by market
 * - Kelly fraction utilization display
 * - Consecutive loss counter (cooldown at 3)
 * - Max position size vs current largest position
 * - Risk alerts panel (any threshold breaches)
 * - Canvas charts for visualizations
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

// ─── Demo Data Generator ──────────────────────────────────────────────────────
function genRiskDemo() {
  const rng = seededRng(456);
  const markets = ['BTC > $100K', 'BTC < $90K', 'ETH > $5K', 'SOL > $200', 'Fed Rate Cut'];
  
  return {
    system: {
      drawdown: 12.5,
      daily_pnl: -45.2,
      daily_limit: 100,
      kelly_fraction: 0.08,
      kelly_max: 0.15,
      consecutive_losses: 2,
      max_consecutive: 3,
      max_position_usd: 100,
      current_max_position: 65,
      balance: 875,
      starting_balance: 1000,
    },
    positions: markets.map((m, i) => ({
      market_slug: m,
      stake_usd: 20 + rng() * 50,
    })),
    alerts: [
      { type: 'warning', message: 'Daily loss at 45% of limit' },
      { type: 'info', message: 'Drawdown within acceptable range' },
    ],
  };
}

// ─── Styles ───────────────────────────────────────────────────────────────────
const styles = {
  page: {
    background: T.bg,
    minHeight: '10vh',
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
  card: {
    background: T.card,
    border: `1px solid ${T.border}`,
    borderRadius: 8,
    padding: 16,
  },
  statCard: {
    background: T.card,
    border: `1px solid ${T.border}`,
    borderRadius: 8,
    padding: 16,
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
  },
  statLabel: {
    fontSize: 10,
    color: T.label,
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
  },
  statValue: {
    fontSize: 18,
    color: '#fff',
    fontWeight: 600,
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
  progressBg: {
    background: 'rgba(255,255,255,0.05)',
    borderRadius: 4,
    height: 12,
    overflow: 'hidden',
  },
  progressBar: (pct, color) => ({
    width: `${Math.min(100, pct)}%`,
    height: '100%',
    background: color,
    borderRadius: 4,
    transition: 'width 0.3s ease',
  }),
  alert: {
    padding: 10,
    borderRadius: 6,
    marginBottom: 8,
    fontSize: 11,
    display: 'flex',
    alignItems: 'center',
    gap: 8,
  },
  alertWarning: {
    background: 'rgba(245,158,11,0.1)',
    border: `1px solid ${T.warning}`,
    color: T.warning,
  },
  alertDanger: {
    background: 'rgba(248,113,113,0.1)',
    border: `1px solid ${T.loss}`,
    color: T.loss,
  },
  alertInfo: {
    background: 'rgba(6,182,212,0.1)',
    border: `1px solid ${T.cyan}`,
    color: T.cyan,
  },
  grid2: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
    gap: 16,
  },
};

// ─── Drawdown Gauge Chart ─────────────────────────────────────────────────────
function DrawdownGauge({ drawdown, maxDrawdown = 45 }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const draw = () => {
      const { ctx, w, h } = setupCanvas(canvas);
      if (!ctx) return;

      ctx.fillStyle = T.chartBg;
      ctx.fillRect(0, 0, w, h);
      drawGrid(ctx, w, h, 4, 2);

      const PAD = { top: 20, right: 16, bottom: 20, left: 16 };
      const cw = w - PAD.left - PAD.right;
      const ch = h - PAD.top - PAD.bottom;

      // Draw gauge background
      const gaugeH = 14;
      const gaugeY = PAD.top + ch / 2 - gaugeH / 2;

      // Background bar
      ctx.fillStyle = 'rgba(255,255,255,0.05)';
      ctx.fillRect(PAD.left, gaugeY, cw, gaugeH);

      // Danger zone (red) starting at 45%
      const dangerStart = (45 / maxDrawdown) * cw;
      ctx.fillStyle = 'rgba(248,113,113,0.3)';
      ctx.fillRect(PAD.left + dangerStart, gaugeY, cw - dangerStart, gaugeH);

      // Warning zone (orange) at 30-45%
      const warnStart = (30 / maxDrawdown) * cw;
      ctx.fillStyle = 'rgba(245,158,11,0.2)';
      ctx.fillRect(PAD.left + warnStart, gaugeY, dangerStart - warnStart, gaugeH);

      // Current drawdown fill
      const fillPct = Math.min(1, drawdown / maxDrawdown);
      ctx.fillStyle = drawdown > 35 ? T.loss : drawdown > 25 ? T.warning : T.cyan;
      ctx.fillRect(PAD.left, gaugeY, cw * fillPct, gaugeH);

      // Threshold markers
      [25, 35, 45].forEach(th => {
        const x = PAD.left + (th / maxDrawdown) * cw;
        ctx.strokeStyle = 'rgba(255,255,255,0.3)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, gaugeY - 2);
        ctx.lineTo(x, gaugeY + gaugeH + 2);
        ctx.stroke();
      });

      // Labels
      ctx.font = `9px ${T.font}`;
      ctx.fillStyle = T.label;
      ctx.fillText('0%', PAD.left, gaugeY + gaugeH + 14);
      ctx.fillText('25%', PAD.left + (25 / maxDrawdown) * cw - 6, gaugeY + gaugeH + 14);
      ctx.fillText('35%', PAD.left + (35 / maxDrawdown) * cw - 6, gaugeY + gaugeH + 14);
      ctx.fillText('45%', PAD.left + cw - 18, gaugeY + gaugeH + 14);

      // Current value
      ctx.font = `600 14px ${T.font}`;
      ctx.fillStyle = drawdown > 35 ? T.loss : drawdown > 25 ? T.warning : T.cyan;
      ctx.fillText(`${drawdown.toFixed(1)}%`, PAD.left + cw - 45, gaugeY + 10);
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, [drawdown]);

  return (
    <div style={styles.chartWrap}>
      <div style={styles.chartTitle}>Drawdown</div>
      <canvas ref={canvasRef} style={styles.canvas} />
    </div>
  );
}

// ─── Concentration Chart ──────────────────────────────────────────────────────
function ConcentrationChart({ positions, maxPosition = 100 }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const draw = () => {
      const { ctx, w, h } = setupCanvas(canvas);
      if (!ctx) return;

      ctx.fillStyle = T.chartBg;
      ctx.fillRect(0, 0, w, h);
      drawGrid(ctx, w, h, 4, 4);

      const PAD = { top: 24, right: 16, bottom: 32, left: 16 };
      const cw = w - PAD.left - PAD.right;
      const ch = h - PAD.top - PAD.bottom;

      if (!positions.length) return;

      const total = positions.reduce((sum, p) => sum + (p.stake_usd || 0), 0);
      const barW = cw / positions.length - 8;

      positions.forEach((pos, i) => {
        const x = PAD.left + i * (barW + 8) + 4;
        const pct = (pos.stake_usd / maxPosition) * 100;
        const barH = (pos.stake_usd / maxPosition) * ch;
        const by = PAD.top + ch - barH;

        // Bar
        ctx.fillStyle = pct > 80 ? T.loss : pct > 60 ? T.warning : T.cyan;
        ctx.globalAlpha = 0.7;
        ctx.fillRect(x, by, barW, barH);
        ctx.globalAlpha = 1;

        // Max line
        const maxY = PAD.top + ch - (maxPosition / maxPosition) * ch;
        ctx.strokeStyle = 'rgba(248,113,113,0.4)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(PAD.left, maxY);
        ctx.lineTo(PAD.left + cw, maxY);
        ctx.stroke();
        ctx.setLineDash([]);

        // Label
        ctx.font = `8px ${T.font}`;
        ctx.fillStyle = T.label;
        const label = pos.market_slug?.slice(0, 12) || `Pos ${i + 1}`;
        ctx.fillText(label, x + 2, PAD.top + ch + 12);

        // Value
        ctx.fillStyle = T.label2;
        ctx.font = `9px ${T.font}`;
        ctx.fillText(`$${pos.stake_usd?.toFixed(0)}`, x + 2, by - 4);
      });
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, [positions, maxPosition]);

  return (
    <div style={styles.chartWrap}>
      <div style={styles.chartTitle}>Position Concentration</div>
      <canvas ref={canvasRef} style={styles.canvas} />
    </div>
  );
}

// ─── Kelly Fraction Chart ─────────────────────────────────────────────────────
function KellyChart({ current, max }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const draw = () => {
      const { ctx, w, h } = setupCanvas(canvas);
      if (!ctx) return;

      ctx.fillStyle = T.chartBg;
      ctx.fillRect(0, 0, w, h);
      drawGrid(ctx, w, h, 4, 2);

      const PAD = { top: 20, right: 16, bottom: 20, left: 16 };
      const cw = w - PAD.left - PAD.right;
      const ch = h - PAD.top - PAD.bottom;

      const gaugeH = 12;
      const gaugeY = PAD.top + ch / 2 - gaugeH / 2;

      // Background
      ctx.fillStyle = 'rgba(255,255,255,0.05)';
      ctx.fillRect(PAD.left, gaugeY, cw, gaugeH);

      // Fill
      const pct = Math.min(1, current / max);
      ctx.fillStyle = T.purple;
      ctx.fillRect(PAD.left, gaugeY, cw * pct, gaugeH);

      // Labels
      ctx.font = `10px ${T.font}`;
      ctx.fillStyle = T.label;
      ctx.fillText('0%', PAD.left, gaugeY + gaugeH + 14);
      ctx.fillText('100%', PAD.left + cw - 20, gaugeY + gaugeH + 14);

      // Current value
      const utilPct = (current / max) * 100;
      ctx.font = `600 14px ${T.font}`;
      ctx.fillStyle = T.purple;
      ctx.fillText(`${utilPct.toFixed(0)}%`, PAD.left + cw - 40, gaugeY + 10);
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, [current, max]);

  return (
    <div style={styles.chartWrap}>
      <div style={styles.chartTitle}>Kelly Utilization</div>
      <canvas ref={canvasRef} style={styles.canvas} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN RISK PAGE
// ═══════════════════════════════════════════════════════════════════════════════
export default function Risk() {
  const api = useApi();
  const [system, setSystem] = useState(null);
  const [positions, setPositions] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [loading, setLoading] = useState(true);

  const fetchRiskData = useCallback(async () => {
    try {
      const [systemRes, positionsRes] = await Promise.allSettled([
        api.get('/api/system/status'),
        api.get('/api/trades', { params: { mode: 'paper' } }),
      ]);

      const sys = systemRes.status === 'fulfilled' ? systemRes.data : genRiskDemo().system;
      const pos = positionsRes.status === 'fulfilled' 
        ? (positionsRes.data?.trades || positionsRes.data || [])
        : genRiskDemo().positions;

      setSystem(sys);
      setPositions(pos.slice(0, 5)); // Top 5 positions by stake

      // Generate alerts
      const newAlerts = [];
      
      if (Math.abs(sys.daily_pnl || 0) > (sys.daily_limit || 100) * 0.8) {
        newAlerts.push({ type: 'danger', message: 'Daily loss approaching limit!' });
      }
      if ((sys.drawdown || 0) > 35) {
        newAlerts.push({ type: 'warning', message: 'Drawdown exceeds 35% threshold' });
      }
      if ((sys.consecutive_losses || 0) >= (sys.max_consecutive || 3) - 1) {
        newAlerts.push({ type: 'warning', message: 'One more loss triggers cooldown' });
      }
      if ((sys.current_max_position || 0) > (sys.max_position_usd || 100) * 0.8) {
        newAlerts.push({ type: 'warning', message: 'Position size approaching maximum' });
      }
      
      if (newAlerts.length === 0) {
        newAlerts.push({ type: 'info', message: 'All risk parameters within limits' });
      }

      setAlerts(newAlerts);
    } catch (err) {
      console.error('Failed to fetch risk data:', err);
      const demo = genRiskDemo();
      setSystem(demo.system);
      setPositions(demo.positions);
      setAlerts(demo.alerts);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    fetchRiskData();
    const interval = setInterval(fetchRiskData, 30000);
    return () => clearInterval(interval);
  }, [fetchRiskData]);

  if (loading) {
    return (
      <div style={styles.page}>
        <div style={styles.header}>
          <span style={{ fontSize: 24 }}>🛡️</span>
          <div>
            <div style={{ ...styles.headerTitle, marginBottom: 2 }}>Risk</div>
            <div style={{ fontSize: 11, color: T.label }}>Risk management dashboard</div>
          </div>
        </div>
        <div style={styles.body}>
          <div style={styles.card}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <div style={{ width: 6, height: 6, borderRadius: '50%', background: T.purple, animation: 'pulse 1.2s infinite' }} />
              <span style={{ fontSize: 10, color: T.label }}>loading risk data…</span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const dailyPnlPct = system ? (Math.abs(system.daily_pnl || 0) / (system.daily_limit || 100)) * 100 : 0;
  const drawdown = system?.drawdown || 0;
  const kellyUtil = system && system.kelly_max > 0 
    ? (system.kelly_fraction / system.kelly_max) * 100 
    : 0;

  return (
    <div style={styles.page}>
      {/* Header */}
      <div style={styles.header}>
        <span style={{ fontSize: 24 }}>🛡️</span>
        <div>
          <div style={{ ...styles.headerTitle, marginBottom: 2 }}>Risk</div>
          <div style={{ fontSize: 11, color: T.label }}>Risk management dashboard</div>
        </div>
      </div>

      <div style={styles.body}>
        {/* Alerts */}
        <div style={styles.card}>
          <div style={styles.sectionTitle}>RISK ALERTS</div>
          {alerts.map((alert, i) => (
            <div key={i} style={{
              ...styles.alert,
              ...(alert.type === 'danger' ? styles.alertDanger : 
                  alert.type === 'warning' ? styles.alertWarning : styles.alertInfo)
            }}>
              <span>{alert.type === 'danger' ? '🔴' : alert.type === 'warning' ? '🟡' : '🔵'}</span>
              <span>{alert.message}</span>
            </div>
          ))}
        </div>

        {/* Risk Stats Grid */}
        <div style={styles.grid2}>
          {/* Drawdown */}
          <div style={styles.statCard}>
            <div style={styles.statLabel}>Current Drawdown</div>
            <div style={{
              ...styles.statValue,
              color: drawdown > 35 ? T.loss : drawdown > 25 ? T.warning : T.cyan
            }}>
              {drawdown.toFixed(1)}%
            </div>
            <div style={{ fontSize: 10, color: T.label }}>Max: 45% (cooldown)</div>
          </div>

          {/* Daily P&L */}
          <div style={styles.statCard}>
            <div style={styles.statLabel}>Today's P&L</div>
            <div style={{
              ...styles.statValue,
              color: (system?.daily_pnl || 0) >= 0 ? T.profit : T.loss
            }}>
              ${(system?.daily_pnl || 0).toFixed(2)}
            </div>
            <div style={{ fontSize: 10, color: T.label }}>
              Limit: ${system?.daily_limit || 100} ({dailyPnlPct.toFixed(0)}% used)
            </div>
          </div>

          {/* Consecutive Losses */}
          <div style={styles.statCard}>
            <div style={styles.statLabel}>Consecutive Losses</div>
            <div style={{
              ...styles.statValue,
              color: (system?.consecutive_losses || 0) >= (system?.max_consecutive || 3) - 1 ? T.warning : T.label2
            }}>
              {system?.consecutive_losses || 0} / {system?.max_consecutive || 3}
            </div>
            <div style={{ fontSize: 10, color: T.label }}>
              Next loss: cooldown triggered
            </div>
          </div>

          {/* Kelly Fraction */}
          <div style={styles.statCard}>
            <div style={styles.statLabel}>Kelly Fraction</div>
            <div style={{ ...styles.statValue, color: T.purple }}>
              {(system?.kelly_fraction || 0).toFixed(3)}
            </div>
            <div style={{ fontSize: 10, color: T.label }}>
              Max: {(system?.kelly_max || 0).toFixed(3)} ({kellyUtil.toFixed(0)}% util)
            </div>
          </div>

          {/* Position Size */}
          <div style={styles.statCard}>
            <div style={styles.statLabel}>Max Position Size</div>
            <div style={{ ...styles.statValue, color: T.cyan }}>
              ${(system?.current_max_position || 0).toFixed(0)}
            </div>
            <div style={{ fontSize: 10, color: T.label }}>
              Limit: ${system?.max_position_usd || 100}
            </div>
          </div>

          {/* Balance */}
          <div style={styles.statCard}>
            <div style={styles.statLabel}>Balance</div>
            <div style={{
              ...styles.statValue,
              color: ((system?.balance || 0) - (system?.starting_balance || 1000)) >= 0 ? T.profit : T.loss
            }}>
              ${(system?.balance || 0).toFixed(0)}
            </div>
            <div style={{ fontSize: 10, color: T.label }}>
              P&L: {((system?.balance || 0) - (system?.starting_balance || 1000)) >= 0 ? '+' : ''}
              ${((system?.balance || 0) - (system?.starting_balance || 1000)).toFixed(0)}
            </div>
          </div>
        </div>

        {/* Charts Row */}
        <div style={styles.sectionTitle}>RISK VISUALIZATIONS</div>

        <div style={styles.grid2}>
          <DrawdownGauge drawdown={drawdown} />
          <KellyChart current={system?.kelly_fraction || 0} max={system?.kelly_max || 1} />
        </div>

        <ConcentrationChart positions={positions} maxPosition={system?.max_position_usd || 100} />

        {/* Daily Progress */}
        <div style={styles.card}>
          <div style={styles.sectionTitle}>DAILY LOSS TRACKER</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            <div style={{ flex: 1 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                <span style={{ fontSize: 11, color: T.label }}>Daily Loss Progress</span>
                <span style={{ fontSize: 11, color: T.label }}>
                  ${(Math.abs(system?.daily_pnl || 0)).toFixed(2)} / ${system?.daily_limit || 100}
                </span>
              </div>
              <div style={styles.progressBg}>
                <div style={{
                  ...styles.progressBar(dailyPnlPct, 
                    dailyPnlPct > 80 ? T.loss : dailyPnlPct > 60 ? T.warning : T.cyan
                  )
                }} />
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4 }}>
                <span style={{ fontSize: 9, color: T.label }}>0%</span>
                <span style={{ fontSize: 9, color: T.label }}>50%</span>
                <span style={{ fontSize: 9, color: T.label }}>80%</span>
                <span style={{ fontSize: 9, color: T.label }}>100%</span>
              </div>
            </div>
            <div style={{ minWidth: 100, textAlign: 'right' }}>
              <div style={{ fontSize: 10, color: T.label }}>Remaining</div>
              <div style={{ 
                fontSize: 16, 
                fontWeight: 600,
                color: dailyPnlPct > 80 ? T.loss : dailyPnlPct > 60 ? T.warning : T.cyan
              }}>
                ${((system?.daily_limit || 100) - Math.abs(system?.daily_pnl || 0)).toFixed(2)}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
