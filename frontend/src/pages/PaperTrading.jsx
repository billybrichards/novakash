/**
 * PaperTrading.jsx — Paper Trading Dashboard
 *
 * Full simulation performance view:
 *  1. Header stats row (balance, P&L, win rate, sharpe, drawdown, PAPER MODE badge)
 *  2. Live Engine Status (feeds, VPIN, cascade state, regime, uptime)
 *  3. Open Positions table
 *  4. Recent Trades table (sortable, CSV export)
 *  5. Strategy Breakdown (arb vs vpin_cascade with sparklines)
 *  6. Simulation Log (terminal-style)
 *  7. Performance Over Time (cumulative P&L canvas chart)
 */

import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { useApi } from '../hooks/useApi.js';

// ─── Google Fonts ─────────────────────────────────────────────────────────────
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
  winBg: 'rgba(74,222,128,0.06)',
  lossBg: 'rgba(248,113,113,0.06)',
};

// ─── Canvas helpers ───────────────────────────────────────────────────────────
function setupCanvas(canvas) {
  if (!canvas) return { ctx: null, w: 0, h: 0 };
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = rect.width || canvas.offsetWidth || 600;
  const h = rect.height || canvas.offsetHeight || 200;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  return { ctx, w, h };
}

function drawGrid(ctx, w, h, cols = 6, rows = 4) {
  ctx.save();
  ctx.strokeStyle = T.gridLine;
  ctx.lineWidth = 1;
  for (let i = 1; i < cols; i++) {
    const x = (w / cols) * i;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
  }
  for (let i = 1; i < rows; i++) {
    const y = (h / rows) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  ctx.restore();
}

// ─── Helpers ─────────────────────────────────────────────────────────────────
function fmt$(v) {
  if (v == null) return '—';
  const n = parseFloat(v);
  return (n >= 0 ? '+$' : '-$') + Math.abs(n).toFixed(2);
}

function fmtPct(v) {
  if (v == null) return '—';
  return ((v >= 0 ? '+' : '') + (parseFloat(v) * 100).toFixed(1) + '%');
}

function fmtDuration(seconds) {
  if (!seconds) return '—';
  const s = parseInt(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function fmtUptime(seconds) {
  if (!seconds) return '0s';
  const s = parseInt(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function fmtTime(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString('en-GB', { hour12: false });
  } catch { return ts; }
}

function fmtDate(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return d.toLocaleString('en-GB', { hour12: false, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch { return ts; }
}

function vpinColor(v) {
  if (!v) return T.label;
  if (v < 0.40) return T.profit;
  if (v < 0.55) return T.warning;
  if (v < 0.70) return '#fb923c';
  return T.loss;
}

// ─── Shared Styles ────────────────────────────────────────────────────────────
const S = {
  page: {
    background: T.bg,
    minHeight: '100vh',
    fontFamily: T.font,
    color: '#fff',
    paddingBottom: 48,
  },
  header: {
    background: 'rgba(255,255,255,0.02)',
    borderBottom: `1px solid ${T.border}`,
    padding: '14px 24px',
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    flexWrap: 'wrap',
  },
  pill: {
    background: T.card,
    border: `1px solid ${T.border}`,
    borderRadius: 6,
    padding: '6px 14px',
  },
  pillLabel: {
    fontSize: 9,
    color: T.label,
    textTransform: 'uppercase',
    letterSpacing: '0.06em',
    marginBottom: 2,
  },
  pillValue: {
    fontSize: 14,
    fontWeight: 600,
    color: '#fff',
  },
  body: { padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 20 },
  section: {
    background: T.card,
    border: `1px solid ${T.border}`,
    borderRadius: 8,
    overflow: 'hidden',
  },
  sectionHead: {
    padding: '12px 16px',
    borderBottom: `1px solid ${T.border}`,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  sectionTitle: {
    fontSize: 10,
    color: T.purple,
    letterSpacing: '0.12em',
    textTransform: 'uppercase',
    fontWeight: 600,
  },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 11 },
  th: {
    padding: '8px 12px',
    textAlign: 'left',
    fontSize: 9,
    color: T.label,
    letterSpacing: '0.06em',
    textTransform: 'uppercase',
    borderBottom: `1px solid ${T.border}`,
    cursor: 'pointer',
    userSelect: 'none',
    whiteSpace: 'nowrap',
  },
  td: {
    padding: '8px 12px',
    borderBottom: `1px solid rgba(255,255,255,0.03)`,
    fontFamily: T.font,
    verticalAlign: 'middle',
    whiteSpace: 'nowrap',
  },
};

// ─── PAPER MODE Badge ─────────────────────────────────────────────────────────
function PaperModeBadge() {
  return (
    <>
      <style>{`
        @keyframes paperGlow {
          0%, 100% { box-shadow: 0 0 8px rgba(168,85,247,0.6), 0 0 24px rgba(168,85,247,0.3); opacity: 1; }
          50% { box-shadow: 0 0 14px rgba(168,85,247,0.9), 0 0 40px rgba(168,85,247,0.5); opacity: 0.85; }
        }
      `}</style>
      <div style={{
        background: 'rgba(168,85,247,0.12)',
        border: '1px solid rgba(168,85,247,0.5)',
        borderRadius: 6,
        padding: '6px 14px',
        animation: 'paperGlow 2.5s ease-in-out infinite',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
      }}>
        <span style={{ width: 7, height: 7, borderRadius: '50%', background: T.purple, display: 'inline-block' }} />
        <span style={{ fontSize: 11, fontWeight: 700, color: T.purple, letterSpacing: '0.12em' }}>PAPER MODE</span>
      </div>
    </>
  );
}

// ─── Stat Pill ────────────────────────────────────────────────────────────────
function StatPill({ label, value, color }) {
  return (
    <div style={S.pill}>
      <div style={S.pillLabel}>{label}</div>
      <div style={{ ...S.pillValue, color: color || '#fff' }}>{value}</div>
    </div>
  );
}

// ─── Feed Indicator ───────────────────────────────────────────────────────────
function FeedDot({ name, connected }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
      <span style={{
        width: 6, height: 6, borderRadius: '50%',
        background: connected ? T.profit : 'rgba(255,255,255,0.15)',
        boxShadow: connected ? `0 0 5px ${T.profit}` : 'none',
        display: 'inline-block',
      }} />
      <span style={{ fontSize: 10, color: connected ? T.label2 : T.label }}>{name}</span>
    </div>
  );
}

// ─── Engine Status pulse ──────────────────────────────────────────────────────
function EnginePulse({ running }) {
  return (
    <>
      <style>{`
        @keyframes enginePulse {
          0%, 100% { transform: scale(1); opacity: 1; }
          50% { transform: scale(1.4); opacity: 0.5; }
        }
      `}</style>
      <span style={{
        width: 10, height: 10, borderRadius: '50%',
        background: running ? T.profit : T.loss,
        boxShadow: running ? `0 0 8px ${T.profit}` : 'none',
        display: 'inline-block',
        animation: running ? 'enginePulse 1.8s ease-in-out infinite' : 'none',
        marginRight: 6,
      }} />
    </>
  );
}

// ─── Section 1: Live Engine Status ───────────────────────────────────────────
function EngineStatus({ data }) {
  const running = data?.engine_status?.toLowerCase() === 'running';
  const vpin = data?.last_vpin;
  const cascade = data?.last_cascade_state || 'IDLE';
  const regime = data?.regime || 'UNKNOWN';
  const uptime = data?.uptime_seconds;
  const heartbeat = data?.last_heartbeat;

  const regimeColor = {
    LOW_VOL: T.profit, NORMAL: T.cyan, HIGH_VOL: T.warning, TRENDING: T.purple,
  }[regime] || T.label;

  return (
    <div style={S.section}>
      <div style={S.sectionHead}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <EnginePulse running={running} />
          <span style={S.sectionTitle}>Live Engine Status</span>
        </div>
        <span style={{ fontSize: 11, color: running ? T.profit : T.loss, fontWeight: 600 }}>
          {running ? 'RUNNING' : 'STOPPED'}
        </span>
      </div>

      <div style={{ padding: 16, display: 'flex', flexWrap: 'wrap', gap: 24 }}>
        {/* Connected feeds */}
        <div>
          <div style={{ fontSize: 9, color: T.label, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>Connected Feeds</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <FeedDot name="Binance" connected={data?.binance_connected} />
            <FeedDot name="Polymarket" connected={data?.polymarket_connected} />
            <FeedDot name="Opinion" connected={data?.opinion_connected} />
            <FeedDot name="CoinGlass" connected={data?.coinglass_connected} />
            <FeedDot name="Chainlink" connected={data?.chainlink_connected} />
          </div>
        </div>

        {/* VPIN */}
        <div>
          <div style={{ fontSize: 9, color: T.label, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>Current VPIN</div>
          <div style={{ fontSize: 32, fontWeight: 700, color: vpinColor(vpin), lineHeight: 1, letterSpacing: '-0.02em' }}>
            {vpin != null ? vpin.toFixed(3) : '—'}
          </div>
          <div style={{ fontSize: 9, color: T.label, marginTop: 4 }}>
            {vpin != null ? (vpin < 0.40 ? 'CALM' : vpin < 0.55 ? 'ELEVATED' : vpin < 0.70 ? 'INFORMED' : 'CASCADE') : ''}
          </div>
        </div>

        {/* Cascade State */}
        <div>
          <div style={{ fontSize: 9, color: T.label, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>Cascade State</div>
          <div style={{ fontSize: 16, fontWeight: 600, color: cascade === 'BET_SIGNAL' ? T.profit : T.purple }}>
            {cascade}
          </div>
        </div>

        {/* Regime */}
        <div>
          <div style={{ fontSize: 9, color: T.label, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>Regime</div>
          <div style={{ fontSize: 14, fontWeight: 600, color: regimeColor }}>
            {regime}
          </div>
        </div>

        {/* Uptime */}
        <div>
          <div style={{ fontSize: 9, color: T.label, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>Uptime</div>
          <div style={{ fontSize: 14, color: T.label2 }}>{fmtUptime(uptime)}</div>
        </div>

        {/* Heartbeat */}
        <div>
          <div style={{ fontSize: 9, color: T.label, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>Last Heartbeat</div>
          <div style={{ fontSize: 12, color: T.label2 }}>{fmtTime(heartbeat)}</div>
        </div>
      </div>
    </div>
  );
}

// ─── Section 2: Open Positions ────────────────────────────────────────────────
function stratColor(strategy) {
  if (!strategy) return T.label;
  if (strategy.includes('vpin') || strategy.includes('cascade')) return T.purple;
  if (strategy.includes('arb')) return T.cyan;
  return T.label2;
}

function OpenPositions({ positions }) {
  const empty = !positions || positions.length === 0;

  return (
    <div style={S.section}>
      <div style={S.sectionHead}>
        <span style={S.sectionTitle}>Open Positions</span>
        <span style={{ fontSize: 10, color: T.label }}>
          {empty ? 'none' : `${positions.length} active`}
        </span>
      </div>

      {empty ? (
        <div style={{ padding: '32px 24px', textAlign: 'center', color: T.label, fontSize: 12 }}>
          No open positions — waiting for signals
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={S.table}>
            <thead>
              <tr>
                {['ID', 'Strategy', 'Direction', 'Entry Price', 'Stake', 'Venue', 'Age', 'Status'].map(h => (
                  <th key={h} style={S.th}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.map(pos => {
                const color = stratColor(pos.strategy);
                const age = pos.created_at ? Math.floor((Date.now() - new Date(pos.created_at)) / 1000) : null;
                return (
                  <tr key={pos.id} style={{ borderLeft: `2px solid ${color}` }}>
                    <td style={{ ...S.td, color: T.label, fontSize: 10 }}>#{String(pos.id).slice(-6)}</td>
                    <td style={{ ...S.td, color }}>{pos.strategy || '—'}</td>
                    <td style={{ ...S.td, color: pos.direction === 'LONG' ? T.profit : pos.direction === 'SHORT' ? T.loss : T.label2 }}>
                      {pos.direction || '—'}
                    </td>
                    <td style={{ ...S.td, color: T.label2 }}>${parseFloat(pos.entry_price || 0).toFixed(4)}</td>
                    <td style={{ ...S.td, color: T.label2 }}>${parseFloat(pos.stake_usd || 0).toFixed(2)}</td>
                    <td style={{ ...S.td, color: T.label }}>{pos.venue || '—'}</td>
                    <td style={{ ...S.td, color: T.label }}>{age != null ? fmtDuration(age) : '—'}</td>
                    <td style={{ ...S.td }}>
                      <span style={{
                        background: 'rgba(6,182,212,0.12)',
                        color: T.cyan,
                        border: `1px solid rgba(6,182,212,0.3)`,
                        borderRadius: 3,
                        padding: '2px 7px',
                        fontSize: 9,
                        letterSpacing: '0.06em',
                      }}>OPEN</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Section 3: Recent Trades ─────────────────────────────────────────────────
const TRADE_COLS = [
  { key: 'created_at', label: 'Time' },
  { key: 'strategy', label: 'Strategy' },
  { key: 'direction', label: 'Dir' },
  { key: 'entry_price', label: 'Entry Price' },
  { key: 'resolved_at', label: 'Exit / Resolved' },
  { key: 'pnl_usd', label: 'P&L ($)' },
  { key: 'status', label: 'Outcome' },
  { key: 'vpin_at_entry', label: 'VPIN' },
  { key: 'duration', label: 'Duration' },
];

function exportCSV(trades) {
  const headers = TRADE_COLS.map(c => c.label).join(',');
  const rows = trades.map(t => {
    const dur = t.created_at && t.resolved_at
      ? Math.floor((new Date(t.resolved_at) - new Date(t.created_at)) / 1000)
      : null;
    return [
      t.created_at || '',
      t.strategy || '',
      t.direction || '',
      t.entry_price || '',
      t.resolved_at || '',
      t.pnl_usd || '',
      t.status || '',
      t.vpin_at_entry || '',
      dur != null ? dur : '',
    ].join(',');
  });
  const csv = [headers, ...rows].join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `paper_trades_${Date.now()}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function RecentTrades({ trades }) {
  const [sortKey, setSortKey] = useState('created_at');
  const [sortDir, setSortDir] = useState(-1); // -1 = desc

  const sorted = useMemo(() => {
    if (!trades) return [];
    return [...trades].sort((a, b) => {
      let av = a[sortKey], bv = b[sortKey];
      if (sortKey === 'created_at' || sortKey === 'resolved_at') {
        av = av ? new Date(av).getTime() : 0;
        bv = bv ? new Date(bv).getTime() : 0;
      } else {
        av = parseFloat(av) || 0;
        bv = parseFloat(bv) || 0;
      }
      return sortDir * (bv - av);
    });
  }, [trades, sortKey, sortDir]);

  function toggleSort(key) {
    if (sortKey === key) setSortDir(d => -d);
    else { setSortKey(key); setSortDir(-1); }
  }

  const empty = !trades || trades.length === 0;

  return (
    <div style={S.section}>
      <div style={S.sectionHead}>
        <span style={S.sectionTitle}>Recent Trades (last 50)</span>
        <button
          onClick={() => exportCSV(sorted)}
          style={{
            background: 'rgba(255,255,255,0.05)',
            border: `1px solid ${T.border}`,
            borderRadius: 4,
            padding: '4px 12px',
            fontSize: 10,
            color: T.label2,
            cursor: 'pointer',
            fontFamily: T.font,
          }}
        >
          Export CSV
        </button>
      </div>

      {empty ? (
        <div style={{ padding: '32px 24px', textAlign: 'center', color: T.label, fontSize: 12 }}>
          No trades yet — engine is collecting data
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={S.table}>
            <thead>
              <tr>
                {TRADE_COLS.map(col => (
                  <th
                    key={col.key}
                    style={{ ...S.th, color: sortKey === col.key ? T.purple : T.label }}
                    onClick={() => toggleSort(col.key)}
                  >
                    {col.label} {sortKey === col.key ? (sortDir === -1 ? '↓' : '↑') : ''}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map(t => {
                const win = t.status === 'WIN';
                const loss = t.status === 'LOSS';
                const pnl = parseFloat(t.pnl_usd || 0);
                const dur = t.created_at && t.resolved_at
                  ? Math.floor((new Date(t.resolved_at) - new Date(t.created_at)) / 1000)
                  : null;
                return (
                  <tr key={t.id} style={{
                    background: win ? T.winBg : loss ? T.lossBg : 'transparent',
                  }}>
                    <td style={{ ...S.td, color: T.label, fontSize: 10 }}>{fmtDate(t.created_at)}</td>
                    <td style={{ ...S.td, color: stratColor(t.strategy) }}>{t.strategy || '—'}</td>
                    <td style={{ ...S.td, color: t.direction === 'LONG' ? T.profit : t.direction === 'SHORT' ? T.loss : T.label2 }}>
                      {t.direction || '—'}
                    </td>
                    <td style={{ ...S.td, color: T.label2 }}>${parseFloat(t.entry_price || 0).toFixed(4)}</td>
                    <td style={{ ...S.td, color: T.label, fontSize: 10 }}>{fmtDate(t.resolved_at)}</td>
                    <td style={{ ...S.td, color: pnl >= 0 ? T.profit : T.loss, fontWeight: 600 }}>
                      {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}
                    </td>
                    <td style={{ ...S.td }}>
                      <span style={{
                        background: win ? 'rgba(74,222,128,0.12)' : 'rgba(248,113,113,0.12)',
                        color: win ? T.profit : T.loss,
                        border: `1px solid ${win ? 'rgba(74,222,128,0.3)' : 'rgba(248,113,113,0.3)'}`,
                        borderRadius: 3,
                        padding: '2px 7px',
                        fontSize: 9,
                        letterSpacing: '0.06em',
                      }}>
                        {t.status}
                      </span>
                    </td>
                    <td style={{ ...S.td, color: vpinColor(t.vpin_at_entry) }}>
                      {t.vpin_at_entry != null ? parseFloat(t.vpin_at_entry).toFixed(3) : '—'}
                    </td>
                    <td style={{ ...S.td, color: T.label }}>{dur != null ? fmtDuration(dur) : '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Mini Sparkline ───────────────────────────────────────────────────────────
function Sparkline({ data }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !data || !data.length) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = 100 * dpr;
    canvas.height = 40 * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);

    const w = 100, h = 40;
    ctx.fillStyle = 'transparent';
    ctx.clearRect(0, 0, w, h);

    const minV = Math.min(...data);
    const maxV = Math.max(...data);
    const range = maxV - minV || 1;
    const toX = (i) => (i / Math.max(data.length - 1, 1)) * w;
    const toY = (v) => h - ((v - minV) / range) * h;

    // Area fill
    const lastVal = data[data.length - 1];
    const isUp = lastVal >= 0;
    ctx.beginPath();
    ctx.moveTo(toX(0), h);
    data.forEach((v, i) => ctx.lineTo(toX(i), toY(v)));
    ctx.lineTo(toX(data.length - 1), h);
    ctx.closePath();
    ctx.fillStyle = isUp ? 'rgba(74,222,128,0.15)' : 'rgba(248,113,113,0.15)';
    ctx.fill();

    // Line
    ctx.beginPath();
    data.forEach((v, i) => i === 0 ? ctx.moveTo(toX(i), toY(v)) : ctx.lineTo(toX(i), toY(v)));
    ctx.strokeStyle = isUp ? T.profit : T.loss;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }, [data]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width: 100, height: 40, display: 'block' }}
    />
  );
}

// ─── Section 4: Strategy Breakdown ───────────────────────────────────────────
function StrategyCard({ title, color, stats, sparkData }) {
  return (
    <div style={{
      flex: 1,
      background: T.card,
      border: `1px solid ${T.border}`,
      borderLeft: `2px solid ${color}`,
      borderRadius: 8,
      padding: 16,
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 16 }}>
        <div style={{ fontSize: 11, fontWeight: 600, color, letterSpacing: '0.06em' }}>{title}</div>
        <Sparkline data={sparkData} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        {stats.map(({ label, value, valueColor }) => (
          <div key={label}>
            <div style={{ fontSize: 9, color: T.label, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 3 }}>{label}</div>
            <div style={{ fontSize: 13, color: valueColor || T.label2, fontWeight: 600 }}>{value}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function StrategyBreakdown({ breakdown }) {
  const arb = breakdown?.arb || {};
  const vpin = breakdown?.vpin_cascade || {};

  const arbSpark = arb.equity_curve || [];
  const vpinSpark = vpin.equity_curve || [];

  const arbStats = [
    { label: 'Trades', value: arb.trade_count ?? '—' },
    { label: 'Win Rate', value: arb.win_rate != null ? `${(arb.win_rate * 100).toFixed(1)}%` : '—', valueColor: arb.win_rate > 0.5 ? T.profit : T.loss },
    { label: 'Total P&L', value: arb.total_pnl != null ? fmt$(arb.total_pnl) : '—', valueColor: (arb.total_pnl || 0) >= 0 ? T.profit : T.loss },
    { label: 'Avg Spread', value: arb.avg_spread != null ? `${(arb.avg_spread * 100).toFixed(2)}¢` : '—' },
    { label: 'Best', value: arb.best_trade != null ? fmt$(arb.best_trade) : '—', valueColor: T.profit },
    { label: 'Worst', value: arb.worst_trade != null ? fmt$(arb.worst_trade) : '—', valueColor: T.loss },
  ];

  const vpinStats = [
    { label: 'Trades', value: vpin.trade_count ?? '—' },
    { label: 'Win Rate', value: vpin.win_rate != null ? `${(vpin.win_rate * 100).toFixed(1)}%` : '—', valueColor: vpin.win_rate > 0.5 ? T.profit : T.loss },
    { label: 'Total P&L', value: vpin.total_pnl != null ? fmt$(vpin.total_pnl) : '—', valueColor: (vpin.total_pnl || 0) >= 0 ? T.profit : T.loss },
    { label: 'Avg VPIN Entry', value: vpin.avg_vpin_entry != null ? parseFloat(vpin.avg_vpin_entry).toFixed(3) : '—', valueColor: vpinColor(vpin.avg_vpin_entry) },
    { label: 'Avg Hold', value: vpin.avg_hold_seconds != null ? fmtDuration(vpin.avg_hold_seconds) : '—' },
    { label: 'Best', value: vpin.best_trade != null ? fmt$(vpin.best_trade) : '—', valueColor: T.profit },
  ];

  return (
    <div style={S.section}>
      <div style={S.sectionHead}>
        <span style={S.sectionTitle}>Strategy Breakdown</span>
      </div>
      <div style={{ padding: 16, display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        <StrategyCard title="Sub-Dollar Arb" color={T.cyan} stats={arbStats} sparkData={arbSpark} />
        <StrategyCard title="VPIN Cascade" color={T.purple} stats={vpinStats} sparkData={vpinSpark} />
      </div>
    </div>
  );
}

// ─── Section 5: Simulation Log ────────────────────────────────────────────────
const LOG_COLORS = {
  WIN: T.profit,
  LOSS: T.loss,
  SIGNAL: T.warning,
  ORDER_PLACED: T.cyan,
  ORDER_RESOLVED: T.label2,
  CASCADE: T.purple,
  SYSTEM: 'rgba(255,255,255,0.4)',
};

function logColor(entry) {
  if (!entry) return T.label;
  const t = (entry.type || '').toUpperCase();
  for (const [key, color] of Object.entries(LOG_COLORS)) {
    if (t.includes(key)) return color;
  }
  return T.label;
}

function SimulationLog({ log }) {
  const bottomRef = useRef(null);
  const containerRef = useRef(null);

  // Auto-scroll to bottom when new entries arrive
  useEffect(() => {
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [log]);

  const empty = !log || log.length === 0;

  return (
    <div style={S.section}>
      <div style={S.sectionHead}>
        <span style={S.sectionTitle}>Simulation Log</span>
        <span style={{ fontSize: 10, color: T.label }}>{log?.length || 0} entries</span>
      </div>
      <div
        ref={containerRef}
        style={{
          background: '#04040a',
          height: 280,
          overflowY: 'auto',
          padding: '10px 14px',
          fontFamily: T.font,
          fontSize: 11,
        }}
      >
        {empty ? (
          <div style={{ color: T.label, padding: '20px 0' }}>No log entries yet — engine is starting up...</div>
        ) : (
          log.map((entry, i) => (
            <div key={i} style={{
              display: 'flex',
              gap: 12,
              padding: '3px 0',
              borderBottom: '1px solid rgba(255,255,255,0.02)',
            }}>
              <span style={{ color: T.label, flexShrink: 0, fontSize: 10 }}>
                {fmtTime(entry.timestamp || entry.created_at)}
              </span>
              <span style={{
                color: logColor(entry),
                flexShrink: 0,
                fontSize: 9,
                textTransform: 'uppercase',
                letterSpacing: '0.05em',
                width: 96,
              }}>
                [{entry.type || 'SYSTEM'}]
              </span>
              <span style={{ color: logColor(entry), opacity: 0.85 }}>
                {entry.message || JSON.stringify(entry.metadata || {})}
              </span>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

// ─── Section 6: Performance Over Time ────────────────────────────────────────
function EquityChart({ equityData }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const draw = () => {
      const { ctx, w, h } = setupCanvas(canvas);
      if (!ctx) return;

      ctx.fillStyle = T.chartBg;
      ctx.fillRect(0, 0, w, h);
      drawGrid(ctx, w, h, 8, 5);

      if (!equityData || equityData.length < 2) {
        ctx.font = `12px ${T.font}`;
        ctx.fillStyle = T.label;
        ctx.textAlign = 'center';
        ctx.fillText('No trade data yet — awaiting first resolved trades', w / 2, h / 2);
        ctx.textAlign = 'left';
        return;
      }

      const values = equityData.map(d => d.cumulative_pnl ?? 0);
      const minV = Math.min(...values, -10);
      const maxV = Math.max(...values, 10);
      const range = maxV - minV || 1;

      const PAD = { top: 28, right: 80, bottom: 32, left: 60 };
      const cw = w - PAD.left - PAD.right;
      const ch = h - PAD.top - PAD.bottom;

      const toX = (i) => PAD.left + (i / Math.max(values.length - 1, 1)) * cw;
      const toY = (v) => PAD.top + (1 - (v - minV) / range) * ch;
      const zeroY = toY(0);

      // Zero line
      ctx.save();
      ctx.strokeStyle = 'rgba(255,255,255,0.2)';
      ctx.lineWidth = 1;
      ctx.setLineDash([6, 6]);
      ctx.beginPath();
      ctx.moveTo(PAD.left, zeroY);
      ctx.lineTo(PAD.left + cw, zeroY);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();

      ctx.font = `10px ${T.font}`;
      ctx.fillStyle = 'rgba(255,255,255,0.2)';
      ctx.fillText('Breakeven', PAD.left + 4, zeroY - 4);

      // Area fill (green above zero, red below)
      // Green zone (above zero)
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(toX(0), Math.min(zeroY, toY(values[0])));
      values.forEach((v, i) => ctx.lineTo(toX(i), toY(v)));
      ctx.lineTo(toX(values.length - 1), zeroY);
      ctx.lineTo(toX(0), zeroY);
      ctx.closePath();
      ctx.fillStyle = 'rgba(74,222,128,0.08)';
      ctx.fill();
      ctx.restore();

      // Red zone (below zero)
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(toX(0), zeroY);
      values.forEach((v, i) => ctx.lineTo(toX(i), toY(v)));
      ctx.lineTo(toX(values.length - 1), zeroY);
      ctx.closePath();
      ctx.fillStyle = 'rgba(248,113,113,0.08)';
      ctx.fill();
      ctx.restore();

      // Draw line segments (green above zero, red below)
      ctx.save();
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      for (let i = 1; i < values.length; i++) {
        const x0 = toX(i - 1), y0 = toY(values[i - 1]);
        const x1 = toX(i), y1 = toY(values[i]);
        const midVal = (values[i - 1] + values[i]) / 2;
        ctx.strokeStyle = midVal >= 0 ? T.profit : T.loss;
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.lineTo(x1, y1);
        ctx.stroke();
      }
      ctx.restore();

      // Final value callout
      const last = values[values.length - 1];
      const callColor = last >= 0 ? T.profit : T.loss;
      ctx.save();
      ctx.fillStyle = 'rgba(0,0,0,0.7)';
      ctx.strokeStyle = callColor;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(w - 74, 6, 68, 22, 4);
      ctx.fill(); ctx.stroke();
      ctx.fillStyle = callColor;
      ctx.font = `600 12px ${T.font}`;
      ctx.fillText(`${last >= 0 ? '+' : ''}$${last.toFixed(2)}`, w - 68, 22);
      ctx.restore();

      // Y-axis labels
      ctx.font = `10px ${T.font}`;
      ctx.fillStyle = T.label;
      [minV, 0, maxV].forEach(v => {
        const y = toY(v);
        if (Math.abs(y - zeroY) > 10) {
          ctx.fillText(`${v >= 0 ? '+' : ''}$${v.toFixed(0)}`, 2, y + 4);
        }
      });
      ctx.fillText('$0', 2, zeroY + 4);

      // X-axis labels
      ctx.fillStyle = T.label;
      const step = Math.max(1, Math.floor(values.length / 6));
      for (let i = 0; i < values.length; i += step) {
        const xLabel = equityData[i]?.trade_num != null ? `T${equityData[i].trade_num}` : `#${i + 1}`;
        ctx.fillText(xLabel, toX(i) - 8, h - 6);
      }
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, [equityData]);

  return (
    <div style={S.section}>
      <div style={S.sectionHead}>
        <span style={S.sectionTitle}>Performance Over Time — Cumulative P&L</span>
      </div>
      <div style={{ padding: 16 }}>
        <canvas ref={canvasRef} style={{ width: '100%', height: 220, display: 'block' }} />
      </div>
    </div>
  );
}

// ─── Loading / Error ──────────────────────────────────────────────────────────
function Spinner() {
  return (
    <span style={{ display: 'inline-block', width: 6, height: 6, borderRadius: '50%', background: T.purple, animation: 'pulse 1.2s infinite' }} />
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN PAGE
// ═══════════════════════════════════════════════════════════════════════════════
export default function PaperTrading() {
  const api = useApi();

  const [status, setStatus] = useState(null);
  const [positions, setPositions] = useState([]);
  const [trades, setTrades] = useState([]);
  const [stats, setStats] = useState(null);
  const [breakdown, setBreakdown] = useState(null);
  const [log, setLog] = useState([]);
  const [equity, setEquity] = useState([]);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(null);

  const fetchAll = useCallback(async () => {
    try {
      const endpoints = [
        '/paper/status',
        '/paper/positions',
        '/paper/trades',
        '/paper/stats',
        '/paper/strategy-breakdown',
        '/paper/log',
        '/paper/equity',
      ];

      const results = await Promise.allSettled(
        endpoints.map(url => api('GET', url))
      );

      const get = (res, fallback) =>
        res.status === 'fulfilled' ? (res.value?.data ?? fallback) : fallback;

      setStatus(get(results[0], null));
      setPositions(get(results[1], []));
      setTrades(get(results[2], []));
      setStats(get(results[3], null));
      setBreakdown(get(results[4], null));
      setLog(get(results[5], []));
      setEquity(get(results[6], []));
      setLastRefresh(new Date());
    } catch (err) {
      console.error('PaperTrading fetch error:', err);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    fetchAll();
    const iv = setInterval(fetchAll, 10000);
    return () => clearInterval(iv);
  }, [fetchAll]);

  // ─── Derived header stats ────────────────────────────────────────────────
  const balance = stats?.current_balance ?? status?.current_balance;
  const totalPnl = stats?.total_pnl;
  const startBalance = 10000; // default paper starting balance
  const pnlPct = balance != null ? (balance - startBalance) / startBalance : null;
  const winRate = stats?.win_rate;
  const totalTrades = stats?.total_trades ?? 0;
  const avgDuration = stats?.avg_duration_seconds;
  const bestTrade = stats?.best_trade;
  const worstTrade = stats?.worst_trade;
  const sharpe = stats?.sharpe_ratio;
  const maxDrawdown = stats?.max_drawdown_pct ?? status?.current_drawdown_pct;

  return (
    <div style={S.page}>
      <style>{`
        @keyframes pulse { 0%, 100% { opacity: 1 } 50% { opacity: 0.3 } }
        @keyframes enginePulse { 0%, 100% { transform: scale(1); opacity: 1 } 50% { transform: scale(1.4); opacity: 0.4 } }
        @keyframes paperGlow {
          0%, 100% { box-shadow: 0 0 8px rgba(168,85,247,0.6), 0 0 24px rgba(168,85,247,0.3); }
          50% { box-shadow: 0 0 16px rgba(168,85,247,0.9), 0 0 48px rgba(168,85,247,0.5); }
        }
        @media (max-width: 768px) {
          .pt-header { padding: 10px 12px !important; gap: 8px !important; }
          .pt-body { padding: 12px !important; }
          .pt-strat-row { flex-direction: column !important; }
        }
        ::-webkit-scrollbar { width: 4px; height: 4px; }
        ::-webkit-scrollbar-track { background: rgba(255,255,255,0.02); }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }
      `}</style>

      {/* ── Header Bar ─────────────────────────────────────────────────────── */}
      <div className="pt-header" style={S.header}>
        <PaperModeBadge />

        <StatPill
          label="Paper Balance"
          value={balance != null ? `$${parseFloat(balance).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—'}
        />
        <StatPill
          label="Total P&L"
          value={totalPnl != null ? `${fmt$(totalPnl)} (${fmtPct(pnlPct)})` : '—'}
          color={(totalPnl || 0) >= 0 ? T.profit : T.loss}
        />
        <StatPill
          label="Win Rate"
          value={winRate != null ? `${(winRate * 100).toFixed(1)}%` : '—'}
          color={winRate > 0.54 ? T.profit : winRate > 0.5 ? T.warning : winRate != null ? T.loss : undefined}
        />
        <StatPill label="Trades" value={totalTrades} />
        <StatPill
          label="Avg Duration"
          value={avgDuration != null ? fmtDuration(avgDuration) : '—'}
        />
        <StatPill
          label="Best Trade"
          value={bestTrade != null ? fmt$(bestTrade) : '—'}
          color={T.profit}
        />
        <StatPill
          label="Worst Trade"
          value={worstTrade != null ? fmt$(worstTrade) : '—'}
          color={T.loss}
        />
        <StatPill
          label="Sharpe"
          value={sharpe != null ? parseFloat(sharpe).toFixed(2) : '—'}
          color={sharpe > 1 ? T.profit : sharpe > 0 ? T.warning : sharpe != null ? T.loss : undefined}
        />
        <StatPill
          label="Max Drawdown"
          value={maxDrawdown != null ? `-${Math.abs(parseFloat(maxDrawdown)).toFixed(1)}%` : '—'}
          color={T.loss}
        />

        {loading && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Spinner />
            <span style={{ fontSize: 10, color: T.label }}>loading…</span>
          </div>
        )}
        {lastRefresh && !loading && (
          <span style={{ fontSize: 9, color: T.label, marginLeft: 'auto' }}>
            refreshed {fmtTime(lastRefresh)}
          </span>
        )}
      </div>

      {/* ── Body ───────────────────────────────────────────────────────────── */}
      <div className="pt-body" style={S.body}>
        <EngineStatus data={status} />
        <OpenPositions positions={positions} />
        <RecentTrades trades={trades} />

        <div className="pt-strat-row" style={{ display: 'flex', gap: 0 }}>
          <StrategyBreakdown breakdown={breakdown} />
        </div>

        <SimulationLog log={log} />
        <EquityChart equityData={equity} />
      </div>
    </div>
  );
}
