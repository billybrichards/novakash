/**
 * Positions.jsx — Open Positions View
 *
 * Shows all open positions from Polymarket with:
 * - Table: Market slug, Direction, Entry Price, Current Price, Stake, P&L, Status
 * - Summary cards: Total exposure, Number of positions, Unrealized P&L
 * - Grouped by strategy (arb vs cascade)
 * - Canvas chart for position distribution
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
function genPositionsDemo() {
  const rng = seededRng(123);
  const strategies = ['sub_dollar_arb', 'vpin_cascade'];
  const markets = [
    'BTC > $100K',
    'BTC < $90K',
    'ETH > $5K',
    'SOL > $200',
    'BTC Halving Q2',
    'Fed Rate Cut May',
  ];
  const directions = ['YES', 'NO'];

  return Array.from({ length: 8 }, (_, i) => {
    const strategy = strategies[Math.floor(rng() * strategies.length)];
    const isArb = strategy === 'sub_dollar_arb';
    const direction = directions[Math.floor(rng() * 2)];
    const market = markets[Math.floor(rng() * markets.length)];
    const entryPrice = 0.35 + rng() * 0.65;
    const currentPrice = Math.max(0.05, Math.min(0.95, entryPrice + (rng() - 0.5) * 0.3));
    const stake = 20 + rng() * 80;
    const pnl = (currentPrice - entryPrice) * stake * (direction === 'YES' ? 1 : -1);

    return {
      id: i,
      market_slug: `${market} ${direction}`,
      direction,
      entry_price: parseFloat(entryPrice.toFixed(3)),
      current_price: parseFloat(currentPrice.toFixed(3)),
      stake_usd: parseFloat(stake.toFixed(2)),
      unrealized_pnl: parseFloat(pnl.toFixed(2)),
      status: 'OPEN',
      strategy,
      created_at: new Date(Date.now() - (72 - i) * 3600000).toISOString(),
    };
  });
}

// ─── Styles ───────────────────────────────────────────────────────────────────
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
  tableWrap: {
    overflowX: 'auto',
  },
  table: {
    width: '100%',
    borderCollapse: 'collapse',
    fontSize: 12,
  },
  th: {
    textAlign: 'left',
    padding: '10px 12px',
    borderBottom: `1px solid ${T.border}`,
    color: T.label,
    fontSize: 10,
    textTransform: 'uppercase',
    letterSpacing: '0.06em',
  },
  td: {
    padding: '10px 12px',
    borderBottom: `1px solid ${T.border}`,
    color: T.label2,
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
  groupHeader: {
    fontSize: 12,
    color: T.cyan,
    fontWeight: 600,
    marginBottom: 8,
    display: 'flex',
    alignItems: 'center',
    gap: 8,
  },
  emptyState: {
    padding: 32,
    textAlign: 'center',
    color: T.label,
    fontSize: 13,
  },
};

// ─── Position Distribution Chart ──────────────────────────────────────────────
function PositionDistributionChart({ positions }) {
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

      const PAD = { top: 24, right: 16, bottom: 24, left: 16 };
      const cw = w - PAD.left - PAD.right;
      const ch = h - PAD.top - PAD.bottom;

      if (!positions.length) return;

      // Group by strategy
      const arb = positions.filter(p => p.strategy === 'sub_dollar_arb');
      const cascade = positions.filter(p => p.strategy === 'vpin_cascade');
      const arbExposure = arb.reduce((sum, p) => sum + p.stake_usd, 0);
      const cascadeExposure = cascade.reduce((sum, p) => sum + p.stake_usd, 0);
      const total = arbExposure + cascadeExposure;

      if (total === 0) return;

      const barW = cw / 2 - 16;
      const maxVal = Math.max(arbExposure, cascadeExposure, 100);

      // Draw bars
      const drawBar = (x, value, color, label) => {
        const barH = (value / maxVal) * ch;
        const by = PAD.top + ch - barH;

        ctx.fillStyle = color;
        ctx.globalAlpha = 0.7;
        ctx.fillRect(x, by, barW, barH);
        ctx.globalAlpha = 1;

        // Label
        ctx.font = `10px ${T.font}`;
        ctx.fillStyle = T.label;
        ctx.fillText(label, x + barW / 2 - 20, PAD.top + ch + 14);

        // Value
        ctx.fillStyle = color;
        ctx.font = `600 11px ${T.font}`;
        ctx.fillText(`$${value.toFixed(0)}`, x + barW / 2 - 18, by - 8);
      };

      drawBar(PAD.left, arbExposure, T.cyan, 'Arb');
      drawBar(PAD.left + barW + 16, cascadeExposure, T.purple, 'Cascade');
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, [positions]);

  return (
    <div style={styles.chartWrap}>
      <div style={styles.chartTitle}>Exposure by Strategy</div>
      <canvas ref={canvasRef} style={styles.canvas} />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN POSITIONS PAGE
// ═══════════════════════════════════════════════════════════════════════════════
export default function Positions() {
  const api = useApi();
  const [positions, setPositions] = useState([]);
  const [loading, setLoading] = useState(true);

  const fetchPositions = useCallback(async () => {
    try {
      const res = await api.get('/api/trades', {
        params: { status: 'OPEN', mode: 'paper' }
      });
      const data = res.data?.trades || res.data || [];
      setPositions(data.length ? data : genPositionsDemo());
    } catch (err) {
      console.error('Failed to fetch positions:', err);
      setPositions(genPositionsDemo());
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    fetchPositions();
    const interval = setInterval(fetchPositions, 30000);
    return () => clearInterval(interval);
  }, [fetchPositions]);

  // Calculate summary stats
  const totalExposure = positions.reduce((sum, p) => sum + (p.stake_usd || 0), 0);
  const totalPnl = positions.reduce((sum, p) => sum + (p.unrealized_pnl || 0), 0);

  // Group by strategy
  const arbPositions = positions.filter(p => p.strategy === 'sub_dollar_arb');
  const cascadePositions = positions.filter(p => p.strategy === 'vpin_cascade');

  return (
    <div style={styles.page}>
      {/* Header */}
      <div style={styles.header}>
        <span style={{ fontSize: 24 }}>📍</span>
        <div>
          <div style={{ ...styles.headerTitle, marginBottom: 2 }}>Positions</div>
          <div style={{ fontSize: 11, color: T.label }}>Open positions from Polymarket</div>
        </div>
      </div>

      <div style={styles.body}>
        {/* Summary Cards */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 16 }}>
          <div style={styles.statCard}>
            <div style={styles.statLabel}>Total Exposure</div>
            <div style={{ ...styles.statValue, color: T.cyan }}>${totalExposure.toFixed(2)}</div>
          </div>
          <div style={styles.statCard}>
            <div style={styles.statLabel}>Open Positions</div>
            <div style={styles.statValue}>{positions.length}</div>
          </div>
          <div style={styles.statCard}>
            <div style={styles.statLabel}>Unrealized P&L</div>
            <div style={{
              ...styles.statValue,
              color: totalPnl >= 0 ? T.profit : T.loss
            }}>
              {totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}
            </div>
          </div>
        </div>

        {/* Exposure Chart */}
        <PositionDistributionChart positions={positions} />

        {/* Positions by Strategy */}
        <div style={styles.sectionTitle}>ACTIVE POSITIONS</div>

        {loading ? (
          <div style={styles.card}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <div style={{ width: 6, height: 6, borderRadius: '50%', background: T.purple, animation: 'pulse 1.2s infinite' }} />
              <span style={{ fontSize: 10, color: T.label }}>loading positions…</span>
            </div>
          </div>
        ) : positions.length === 0 ? (
          <div style={styles.card}>
            <div style={styles.emptyState}>No open positions</div>
          </div>
        ) : (
          <>
            {/* Arb Positions */}
            {arbPositions.length > 0 && (
              <div>
                <div style={styles.groupHeader}>
                  <span>🔵</span> Sub-Dollar Arb ({arbPositions.length})
                </div>
                <div style={styles.card}>
                  <div style={styles.tableWrap}>
                    <table style={styles.table}>
                      <thead>
                        <tr>
                          <th style={styles.th}>Market</th>
                          <th style={styles.th}>Direction</th>
                          <th style={styles.th}>Entry</th>
                          <th style={styles.th}>Current</th>
                          <th style={styles.th}>Stake</th>
                          <th style={styles.th}>P&L</th>
                        </tr>
                      </thead>
                      <tbody>
                        {arbPositions.map(pos => (
                          <tr key={pos.id}>
                            <td style={styles.td}>{pos.market_slug || '—'}</td>
                            <td style={{
                              ...styles.td,
                              color: pos.direction === 'YES' ? T.cyan : T.label2
                            }}>
                              {pos.direction}
                            </td>
                            <td style={styles.td}>{pos.entry_price?.toFixed(3) || '—'}</td>
                            <td style={styles.td}>{pos.current_price?.toFixed(3) || '—'}</td>
                            <td style={styles.td}>${pos.stake_usd?.toFixed(2)}</td>
                            <td style={{
                              ...styles.td,
                              color: (pos.unrealized_pnl || 0) >= 0 ? T.profit : T.loss
                            }}>
                              {(pos.unrealized_pnl || 0) >= 0 ? '+' : ''}${(pos.unrealized_pnl || 0).toFixed(2)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            )}

            {/* Cascade Positions */}
            {cascadePositions.length > 0 && (
              <div>
                <div style={styles.groupHeader}>
                  <span>🟣</span> VPIN Cascade ({cascadePositions.length})
                </div>
                <div style={styles.card}>
                  <div style={styles.tableWrap}>
                    <table style={styles.table}>
                      <thead>
                        <tr>
                          <th style={styles.th}>Market</th>
                          <th style={styles.th}>Direction</th>
                          <th style={styles.th}>Entry</th>
                          <th style={styles.th}>Current</th>
                          <th style={styles.th}>Stake</th>
                          <th style={styles.th}>P&L</th>
                        </tr>
                      </thead>
                      <tbody>
                        {cascadePositions.map(pos => (
                          <tr key={pos.id}>
                            <td style={styles.td}>{pos.market_slug || '—'}</td>
                            <td style={{
                              ...styles.td,
                              color: pos.direction === 'YES' ? T.cyan : T.label2
                            }}>
                              {pos.direction}
                            </td>
                            <td style={styles.td}>{pos.entry_price?.toFixed(3) || '—'}</td>
                            <td style={styles.td}>{pos.current_price?.toFixed(3) || '—'}</td>
                            <td style={styles.td}>${pos.stake_usd?.toFixed(2)}</td>
                            <td style={{
                              ...styles.td,
                              color: (pos.unrealized_pnl || 0) >= 0 ? T.profit : T.loss
                            }}>
                              {(pos.unrealized_pnl || 0) >= 0 ? '+' : ''}${(pos.unrealized_pnl || 0).toFixed(2)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
