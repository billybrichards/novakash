/**
 * FactoryFloor.jsx — BTC Trading Factory Floor
 *
 * Visualizes the entire trading pipeline in real-time like a
 * manufacturing control room: Data Feeds -> Signals -> Gates -> Orders
 *
 * Single file, inline styles, no external chart libraries.
 */

import React, {
  useState, useEffect, useCallback, useMemo,
} from 'react';
import { useApi } from '../hooks/useApi.js';

// ─── Theme tokens (same as V58Monitor) ───────────────────────────────────────
const T = {
  bg:      '#07070c',
  card:    'rgba(255,255,255,0.018)',
  border:  'rgba(255,255,255,0.07)',
  purple:  '#a855f7',
  cyan:    '#06b6d4',
  profit:  '#4ade80',
  loss:    '#f87171',
  warning: '#f59e0b',
  label:   'rgba(255,255,255,0.35)',
  label2:  'rgba(255,255,255,0.55)',
  mono:    "'IBM Plex Mono', monospace",
};

// ─── Inject font + keyframes (once) ──────────────────────────────────────────
if (!document.getElementById('factory-floor-styles')) {
  const style = document.createElement('style');
  style.id = 'factory-floor-styles';
  style.textContent = `
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap');

    @keyframes flowDot {
      0%   { transform: translateX(0);    opacity: 0; }
      10%  { opacity: 1; }
      90%  { opacity: 1; }
      100% { transform: translateX(40px); opacity: 0; }
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50%      { opacity: 0.4; }
    }
    @keyframes progressStripe {
      0%   { background-position: 0 0; }
      100% { background-position: 40px 0; }
    }

    @media (max-width: 768px) {
      .ff-pipeline { flex-direction: column !important; }
      .ff-pipeline > div { min-width: 0 !important; width: 100% !important; }
      .ff-connector { display: none !important; }
      .ff-bottom-row { flex-direction: column !important; }
      .ff-header { flex-wrap: wrap; gap: 8px !important; }
    }
  `;
  document.head.appendChild(style);
}

// ─── Helpers ─────────────────────────────────────────────────────────────────
function fmt(v, decimals = 2) {
  if (v == null || isNaN(v)) return '\u2014';
  return Number(v).toFixed(decimals);
}

function utcHHMM(ts) {
  if (!ts) return '\u2014';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toISOString().slice(11, 16);
}

function utcClock() {
  return new Date().toISOString().slice(11, 19) + ' UTC';
}

// ─── StatusDot ───────────────────────────────────────────────────────────────
function StatusDot({ color, size = 8, pulse: shouldPulse = false }) {
  return (
    <span style={{
      display: 'inline-block',
      width: size,
      height: size,
      borderRadius: '50%',
      background: color,
      boxShadow: `0 0 6px ${color}88`,
      animation: shouldPulse ? 'pulse 2s ease-in-out infinite' : 'none',
      flexShrink: 0,
    }} />
  );
}

// ─── SectionLabel ────────────────────────────────────────────────────────────
function SectionLabel({ children }) {
  return (
    <div style={{
      fontSize: 9,
      color: T.purple,
      letterSpacing: '0.14em',
      fontWeight: 700,
      fontFamily: T.mono,
      marginBottom: 10,
      textTransform: 'uppercase',
    }}>
      {children}
    </div>
  );
}

// ─── Card wrapper ────────────────────────────────────────────────────────────
function Card({ children, style: extra = {} }) {
  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.border}`,
      borderRadius: 10,
      padding: '14px 16px',
      fontFamily: T.mono,
      ...extra,
    }}>
      {children}
    </div>
  );
}

// ─── Connector (animated flowing dots between pipeline columns) ──────────────
function Connector({ color = T.purple }) {
  return (
    <div className="ff-connector" style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      width: 48,
      flexShrink: 0,
      position: 'relative',
      overflow: 'hidden',
    }}>
      {/* Line */}
      <div style={{
        position: 'absolute',
        top: '50%',
        left: 4,
        right: 4,
        height: 1,
        background: `${color}44`,
      }} />
      {/* Flowing dots */}
      {[0, 1, 2].map(i => (
        <div key={i} style={{
          position: 'absolute',
          top: '50%',
          left: 0,
          width: 5,
          height: 5,
          borderRadius: '50%',
          background: color,
          transform: 'translateY(-50%)',
          animation: `flowDot 1.8s ${i * 0.6}s ease-in-out infinite`,
          opacity: 0,
        }} />
      ))}
    </div>
  );
}

// ─── Feed Row ────────────────────────────────────────────────────────────────
function FeedRow({ name, freq, connected }) {
  const color = connected === true ? T.profit
    : connected === false ? T.loss
    : T.warning;
  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '5px 0',
      borderBottom: `1px solid ${T.border}`,
    }}>
      <StatusDot color={color} pulse={connected === false} />
      <span style={{ fontSize: 11, color: '#fff', flex: 1 }}>{name}</span>
      <span style={{ fontSize: 9, color: T.label, whiteSpace: 'nowrap' }}>{freq}</span>
    </div>
  );
}

// ─── Gate Row ────────────────────────────────────────────────────────────────
function GateRow({ name, passed, threshold, blocked }) {
  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '5px 0',
      borderBottom: `1px solid ${T.border}`,
      background: blocked ? 'rgba(248,113,113,0.06)' : 'transparent',
      borderRadius: 4,
      paddingLeft: blocked ? 6 : 0,
    }}>
      <span style={{ fontSize: 13, width: 18, textAlign: 'center' }}>
        {passed ? '\u2705' : '\u274C'}
      </span>
      <span style={{ fontSize: 11, color: '#fff', flex: 1 }}>{name}</span>
      <span style={{ fontSize: 9, color: T.label }}>{threshold}</span>
    </div>
  );
}

// ─── Derive gate results from a window object ───────────────────────────────
function deriveGates(w) {
  if (!w) return [];
  const skip = (w.skip_reason || '').toUpperCase();
  const gammaPrice = w.direction === 'UP'
    ? w.gamma_up_price
    : w.gamma_down_price;

  const gates = [
    {
      name: 'VPIN',
      passed: w.vpin != null && w.vpin >= 0.45,
      threshold: '\u22650.45',
      blocked: skip.includes('VPIN'),
    },
    {
      name: 'TWAP',
      passed: !skip.includes('TWAP'),
      threshold: 'align',
      blocked: skip.includes('TWAP'),
    },
    {
      name: 'Delta',
      passed: w.delta_pct != null && Math.abs(w.delta_pct) >= 0.02,
      threshold: '\u22650.02%',
      blocked: skip.includes('DELTA'),
    },
    {
      name: 'CG Veto',
      passed: !skip.includes('CG VETO') && !skip.includes('CG_VETO'),
      threshold: 'no veto',
      blocked: skip.includes('CG'),
    },
    {
      name: 'Floor',
      passed: gammaPrice != null ? gammaPrice >= 0.30 : true,
      threshold: '\u2265$0.30',
      blocked: skip.includes('FLOOR'),
    },
    {
      name: 'Cap',
      passed: gammaPrice != null ? gammaPrice <= 0.83 : true,
      threshold: '\u2264$0.83',
      blocked: skip.includes('CAP'),
    },
  ];
  return gates;
}

// ─── Outcome row for the timeline ────────────────────────────────────────────
function outcomeLabel(o) {
  if (!o) return { text: '\u2014', color: T.label };
  if (o.v71_correct === true) return { text: 'WIN', color: T.profit };
  if (o.v71_correct === false) return { text: 'LOSS', color: T.loss };
  if (!o.v71_would_trade && !o.v58_would_trade) return { text: 'SKIP', color: T.label };
  if (o.v58_correct === true) return { text: 'WIN', color: T.profit };
  if (o.v58_correct === false) return { text: 'LOSS', color: T.loss };
  return { text: 'SKIP', color: T.label };
}

function outcomeGateString(o) {
  if (!o) return '';
  const skip = (o.skip_reason || '').toUpperCase();
  // Simple compact gate pass/fail string
  const checks = [
    !skip.includes('VPIN'),
    !skip.includes('TWAP'),
    !skip.includes('DELTA'),
    !skip.includes('CG'),
    !skip.includes('FLOOR'),
    !skip.includes('CAP'),
  ];
  return checks.map(p => p ? '\u2705' : '\u274C').join('');
}

// =============================================================================
// Main Component
// =============================================================================
export default function FactoryFloor() {
  const api = useApi();

  // ── State ──────────────────────────────────────────────────────────────────
  const [latestWindow, setLatestWindow] = useState(null);
  const [outcomes, setOutcomes]         = useState([]);
  const [accuracy, setAccuracy]         = useState(null);
  const [stats, setStats]               = useState(null);
  const [livePrices, setLivePrices]     = useState(null);
  const [systemStatus, setSystemStatus] = useState(null);
  const [clock, setClock]               = useState(utcClock());
  const [loading, setLoading]           = useState(true);

  // ── Clock tick ─────────────────────────────────────────────────────────────
  useEffect(() => {
    const id = setInterval(() => setClock(utcClock()), 1000);
    return () => clearInterval(id);
  }, []);

  // ── Primary fetch (windows, outcomes, accuracy, stats) ─────────────────────
  const fetchPrimary = useCallback(async () => {
    try {
      const [winRes, outRes, accRes, statsRes] = await Promise.allSettled([
        api('GET', '/v58/windows?limit=1'),
        api('GET', '/v58/outcomes?limit=15'),
        api('GET', '/v58/accuracy?limit=50'),
        api('GET', '/v58/stats?days=7'),
      ]);

      if (winRes.status === 'fulfilled') {
        const ws = winRes.value?.data?.windows ?? [];
        setLatestWindow(ws[0] ?? null);
      }
      if (outRes.status === 'fulfilled') {
        setOutcomes(outRes.value?.data?.outcomes ?? []);
      }
      if (accRes.status === 'fulfilled') {
        setAccuracy(accRes.value?.data ?? null);
      }
      if (statsRes.status === 'fulfilled') {
        setStats(statsRes.value?.data ?? null);
      }
    } catch (_) { /* swallow */ }
    setLoading(false);
  }, [api]);

  // ── Live prices (fast poll) ────────────────────────────────────────────────
  const fetchPrices = useCallback(async () => {
    try {
      const res = await api('GET', '/v58/live-prices');
      setLivePrices(res?.data ?? null);
    } catch (_) { /* swallow */ }
  }, [api]);

  // ── System status ──────────────────────────────────────────────────────────
  const fetchSystem = useCallback(async () => {
    try {
      const res = await api('GET', '/system/status');
      setSystemStatus(res?.data ?? null);
    } catch (_) { /* swallow */ }
  }, [api]);

  // ── Polling setup ──────────────────────────────────────────────────────────
  useEffect(() => {
    fetchPrimary();
    fetchPrices();
    fetchSystem();

    const idPrimary = setInterval(fetchPrimary, 15000);
    const idWindow  = setInterval(async () => {
      try {
        const res = await api('GET', '/v58/windows?limit=1');
        const ws = res?.data?.windows ?? [];
        if (ws[0]) setLatestWindow(ws[0]);
      } catch (_) {}
    }, 5000);
    const idPrices  = setInterval(fetchPrices, 3000);
    const idSystem  = setInterval(fetchSystem, 10000);

    return () => {
      clearInterval(idPrimary);
      clearInterval(idWindow);
      clearInterval(idPrices);
      clearInterval(idSystem);
    };
  }, [fetchPrimary, fetchPrices, fetchSystem, api]);

  // ── Derived values ─────────────────────────────────────────────────────────
  const w = latestWindow;
  const gates = useMemo(() => deriveGates(w), [w]);
  const paperMode = systemStatus?.paper_mode ?? true;
  const engineRunning = systemStatus?.engine_running ?? systemStatus?.running ?? null;

  // Feed connectivity — try multiple possible field names
  const feeds = useMemo(() => {
    const s = systemStatus || {};
    const conn = s.connections || s.feeds || {};
    return [
      { name: 'Binance',   freq: '1-3 Hz',  connected: conn.binance_connected ?? conn.binance ?? null },
      { name: 'Chainlink', freq: '5s',       connected: conn.chainlink_connected ?? conn.chainlink ?? null },
      { name: 'Tiingo',    freq: '2s',       connected: conn.tiingo_connected ?? conn.tiingo ?? null },
      { name: 'CoinGlass', freq: '15s',      connected: conn.coinglass_connected ?? conn.coinglass ?? null },
      { name: 'Gamma',     freq: '1s',       connected: conn.gamma_connected ?? conn.gamma ?? null },
      { name: 'CLOB',      freq: '10s',      connected: conn.clob_connected ?? conn.clob ?? null },
      { name: 'TimesFM',   freq: '1s',       connected: conn.timesfm_connected ?? conn.timesfm ?? null },
    ];
  }, [systemStatus]);

  // Window progress
  const windowProgress = useMemo(() => {
    if (!w?.window_ts) return null;
    const ts = typeof w.window_ts === 'number' ? w.window_ts : new Date(w.window_ts).getTime() / 1000;
    const now = Date.now() / 1000;
    const elapsed = now - ts;
    const remaining = Math.max(0, 300 - elapsed);
    const pct = Math.min(100, (elapsed / 300) * 100);
    return { elapsed, remaining, pct };
  }, [w, clock]); // clock dependency forces re-render every second

  // Performance from accuracy
  const perf = useMemo(() => {
    if (!accuracy) return null;
    return {
      wins: accuracy.v71_wins ?? 0,
      losses: accuracy.v71_losses ?? 0,
      streak: accuracy.current_streak ?? 0,
      pnl: accuracy.cumulative_pnl ?? accuracy.v71_pnl ?? 0,
    };
  }, [accuracy]);

  // ── Render ─────────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div style={{
        background: T.bg,
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontFamily: T.mono,
        color: T.label,
        fontSize: 13,
      }}>
        Loading factory floor...
      </div>
    );
  }

  return (
    <div style={{
      background: T.bg,
      minHeight: '100vh',
      color: '#fff',
      fontFamily: T.mono,
      padding: '16px 20px 32px',
    }}>
      {/* ─── SECTION 1: Header Bar ─────────────────────────────────────────── */}
      <div className="ff-header" style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: 20,
        gap: 16,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{
            fontSize: 15,
            fontWeight: 700,
            letterSpacing: '0.08em',
            color: '#fff',
          }}>
            TRADING FACTORY FLOOR
          </span>
          <span style={{
            fontSize: 10,
            color: T.label,
            letterSpacing: '0.06em',
          }}>
            {clock}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          {/* Paper/Live badge */}
          <span style={{
            fontSize: 9,
            fontWeight: 700,
            padding: '3px 8px',
            borderRadius: 4,
            background: paperMode ? 'rgba(168,85,247,0.15)' : 'rgba(248,113,113,0.15)',
            color: paperMode ? T.purple : T.loss,
            letterSpacing: '0.1em',
          }}>
            {paperMode ? 'PAPER' : 'LIVE'}
          </span>
          {/* Engine status dot */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <StatusDot
              color={engineRunning === true ? T.profit : engineRunning === false ? T.loss : T.warning}
              size={7}
              pulse={engineRunning === false}
            />
            <span style={{ fontSize: 9, color: T.label }}>ENGINE</span>
          </div>
        </div>
      </div>

      {/* ─── SECTION 2: Pipeline Flow ──────────────────────────────────────── */}
      <div className="ff-pipeline" style={{
        display: 'flex',
        gap: 0,
        marginBottom: 20,
        alignItems: 'stretch',
      }}>
        {/* Column 1: DATA FEEDS */}
        <Card style={{ flex: 1, minWidth: 170 }}>
          <SectionLabel>DATA FEEDS</SectionLabel>
          {feeds.map((f, i) => (
            <FeedRow key={i} name={f.name} freq={f.freq} connected={f.connected} />
          ))}
        </Card>

        <Connector color={T.cyan} />

        {/* Column 2: SIGNALS */}
        <Card style={{ flex: 1.1, minWidth: 180 }}>
          <SectionLabel>SIGNALS</SectionLabel>

          {/* VPIN */}
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 9, color: T.label, marginBottom: 3 }}>VPIN</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{
                fontSize: 18,
                fontWeight: 700,
                color: w?.vpin >= 0.65 ? T.profit : w?.vpin >= 0.45 ? T.warning : T.label2,
              }}>
                {fmt(w?.vpin, 3)}
              </span>
              {/* Color bar */}
              <div style={{
                flex: 1,
                height: 4,
                background: 'rgba(255,255,255,0.06)',
                borderRadius: 2,
                overflow: 'hidden',
              }}>
                <div style={{
                  height: '100%',
                  width: `${Math.min(100, (w?.vpin ?? 0) * 100)}%`,
                  background: w?.vpin >= 0.65 ? T.profit : w?.vpin >= 0.45 ? T.warning : T.label2,
                  borderRadius: 2,
                  transition: 'width 0.4s',
                }} />
              </div>
            </div>
          </div>

          {/* Delta */}
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 9, color: T.label, marginBottom: 3 }}>DELTA</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{
                fontSize: 15,
                fontWeight: 600,
                color: w?.delta_pct > 0 ? T.profit : w?.delta_pct < 0 ? T.loss : T.label2,
              }}>
                {w?.delta_pct != null
                  ? `${w.delta_pct > 0 ? '\u25B2' : '\u25BC'} ${fmt(Math.abs(w.delta_pct), 3)}%`
                  : '\u2014'}
              </span>
            </div>
          </div>

          {/* Direction */}
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 9, color: T.label, marginBottom: 3 }}>DIRECTION</div>
            <span style={{
              fontSize: 12,
              fontWeight: 700,
              padding: '2px 10px',
              borderRadius: 4,
              background: w?.direction === 'UP'
                ? 'rgba(74,222,128,0.12)' : w?.direction === 'DOWN'
                ? 'rgba(248,113,113,0.12)' : 'rgba(255,255,255,0.04)',
              color: w?.direction === 'UP' ? T.profit : w?.direction === 'DOWN' ? T.loss : T.label,
            }}>
              {w?.direction || '\u2014'}
            </span>
          </div>

          {/* Confidence */}
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 9, color: T.label, marginBottom: 3 }}>CONFIDENCE</div>
            <span style={{ fontSize: 13, fontWeight: 600, color: T.cyan }}>
              {w?.confidence != null ? `${(w.confidence * 100).toFixed(0)}%` : '\u2014'}
            </span>
          </div>

          {/* Regime */}
          <div>
            <div style={{ fontSize: 9, color: T.label, marginBottom: 3 }}>REGIME</div>
            <span style={{
              fontSize: 10,
              fontWeight: 700,
              padding: '2px 8px',
              borderRadius: 4,
              background: w?.regime === 'CASCADE'
                ? 'rgba(248,113,113,0.15)' : w?.regime === 'TRANSITION'
                ? 'rgba(245,158,11,0.15)' : 'rgba(255,255,255,0.04)',
              color: w?.regime === 'CASCADE' ? T.loss : w?.regime === 'TRANSITION' ? T.warning : T.label2,
            }}>
              {w?.regime || 'NORMAL'}
            </span>
          </div>
        </Card>

        <Connector color={T.purple} />

        {/* Column 3: GATES */}
        <Card style={{ flex: 1, minWidth: 170 }}>
          <SectionLabel>GATES</SectionLabel>
          {gates.length > 0 ? gates.map((g, i) => (
            <GateRow key={i} name={g.name} passed={g.passed} threshold={g.threshold} blocked={g.blocked} />
          )) : (
            <div style={{ fontSize: 11, color: T.label }}>No window data</div>
          )}
          {/* Pipeline blocked indicator */}
          {w && !w.trade_placed && w.skip_reason && (
            <div style={{
              marginTop: 10,
              padding: '6px 8px',
              background: 'rgba(248,113,113,0.08)',
              border: `1px solid rgba(248,113,113,0.2)`,
              borderRadius: 6,
              fontSize: 9,
              color: T.loss,
              lineHeight: 1.4,
            }}>
              BLOCKED: {w.skip_reason}
            </div>
          )}
        </Card>

        <Connector color={T.profit} />

        {/* Column 4: ORDER STATUS */}
        <Card style={{ flex: 1, minWidth: 170 }}>
          <SectionLabel>ORDER STATUS</SectionLabel>
          {w ? (
            w.trade_placed ? (
              <div>
                <div style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  marginBottom: 10,
                }}>
                  <span style={{
                    fontSize: 11,
                    fontWeight: 700,
                    padding: '2px 10px',
                    borderRadius: 4,
                    background: w.direction === 'UP' ? 'rgba(74,222,128,0.15)' : 'rgba(248,113,113,0.15)',
                    color: w.direction === 'UP' ? T.profit : T.loss,
                  }}>
                    {w.direction || '\u2014'}
                  </span>
                  <span style={{ fontSize: 10, color: T.label }}>GTC</span>
                </div>
                {/* Entry price */}
                <div style={{ marginBottom: 8 }}>
                  <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>ENTRY</div>
                  <span style={{ fontSize: 14, fontWeight: 600, color: T.cyan }}>
                    ${fmt(w.direction === 'UP' ? (livePrices?.up_price ?? w.gamma_up_price) : (livePrices?.down_price ?? w.gamma_down_price), 3)}
                  </span>
                </div>
                {/* Stake */}
                <div style={{ marginBottom: 8 }}>
                  <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>STAKE</div>
                  <span style={{ fontSize: 13, fontWeight: 600, color: '#fff' }}>$4.00</span>
                </div>
                {/* Resolution (if available via outcomes) */}
                {outcomes[0] && outcomes[0].window_ts === w.window_ts && (
                  <div style={{ marginTop: 6 }}>
                    <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>RESULT</div>
                    <span style={{
                      fontSize: 13,
                      fontWeight: 700,
                      color: outcomes[0].v71_correct ? T.profit : T.loss,
                    }}>
                      {outcomes[0].v71_correct ? 'WIN' : 'LOSS'}
                    </span>
                  </div>
                )}
              </div>
            ) : (
              <div>
                <div style={{
                  fontSize: 14,
                  fontWeight: 700,
                  color: T.warning,
                  marginBottom: 8,
                }}>
                  SKIP
                </div>
                <div style={{ fontSize: 10, color: T.label2, lineHeight: 1.5 }}>
                  {w.skip_reason || 'No trade signal'}
                </div>
              </div>
            )
          ) : (
            <div style={{ fontSize: 11, color: T.label }}>Waiting for window...</div>
          )}
        </Card>
      </div>

      {/* ─── SECTION 3: Window Progress Bar ────────────────────────────────── */}
      <Card style={{ marginBottom: 20, padding: '12px 16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
          <SectionLabel>WINDOW PROGRESS</SectionLabel>
          {windowProgress && (
            <span style={{
              fontSize: 11,
              fontWeight: 600,
              color: windowProgress.remaining < 60 ? T.loss
                : windowProgress.remaining < 120 ? T.warning
                : T.profit,
            }}>
              T-{Math.floor(windowProgress.remaining)}s
            </span>
          )}
        </div>
        {/* Progress bar */}
        <div style={{
          width: '100%',
          height: 10,
          background: 'rgba(255,255,255,0.04)',
          borderRadius: 5,
          overflow: 'hidden',
          position: 'relative',
        }}>
          <div style={{
            height: '100%',
            width: `${windowProgress?.pct ?? 0}%`,
            background: !windowProgress ? T.label
              : windowProgress.remaining < 60 ? T.loss
              : windowProgress.remaining < 120 ? T.warning
              : T.profit,
            borderRadius: 5,
            transition: 'width 1s linear',
            backgroundImage: 'linear-gradient(45deg, rgba(255,255,255,0.08) 25%, transparent 25%, transparent 50%, rgba(255,255,255,0.08) 50%, rgba(255,255,255,0.08) 75%, transparent 75%)',
            backgroundSize: '40px 40px',
            animation: 'progressStripe 1s linear infinite',
          }} />
        </div>
        {/* Metadata row */}
        <div style={{
          display: 'flex',
          justifyContent: 'space-between',
          marginTop: 6,
          fontSize: 9,
          color: T.label,
        }}>
          <span>
            Window: {w?.window_ts ? utcHHMM(w.window_ts) : '\u2014'}
          </span>
          <span>
            Delta: {w?.delta_pct != null ? `${fmt(w.delta_pct, 3)}%` : '\u2014'}
          </span>
          <span>
            Elapsed: {windowProgress ? `${Math.floor(windowProgress.elapsed)}s / 300s` : '\u2014'}
          </span>
          <span>
            UP: ${fmt(livePrices?.up_price ?? w?.gamma_up_price, 3)} | DOWN: ${fmt(livePrices?.down_price ?? w?.gamma_down_price, 3)}
          </span>
        </div>
      </Card>

      {/* ─── SECTION 4: Recent Flow Timeline ───────────────────────────────── */}
      <Card style={{ marginBottom: 20, padding: '12px 16px' }}>
        <SectionLabel>RECENT FLOW TIMELINE</SectionLabel>
        {/* Header */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: '60px 50px 1fr 100px 60px',
          gap: 8,
          padding: '4px 0 6px',
          borderBottom: `1px solid ${T.border}`,
          fontSize: 9,
          color: T.label,
          letterSpacing: '0.08em',
        }}>
          <span>TIME</span>
          <span>DIR</span>
          <span>GATES</span>
          <span>REASON</span>
          <span style={{ textAlign: 'right' }}>RESULT</span>
        </div>
        {/* Rows */}
        {outcomes.length > 0 ? outcomes.slice(0, 15).map((o, i) => {
          const result = outcomeLabel(o);
          const gateStr = outcomeGateString(o);
          const rowBg = result.text === 'WIN'
            ? 'rgba(74,222,128,0.04)'
            : result.text === 'LOSS'
            ? 'rgba(248,113,113,0.04)'
            : 'transparent';
          return (
            <div key={i} style={{
              display: 'grid',
              gridTemplateColumns: '60px 50px 1fr 100px 60px',
              gap: 8,
              padding: '5px 0',
              borderBottom: `1px solid ${T.border}`,
              fontSize: 10,
              background: rowBg,
            }}>
              <span style={{ color: T.label2 }}>{utcHHMM(o.window_ts)}</span>
              <span style={{
                fontWeight: 600,
                color: o.actual_direction === 'UP' ? T.profit
                  : o.actual_direction === 'DOWN' ? T.loss
                  : T.label,
              }}>
                {o.actual_direction || o.direction || '\u2014'}
              </span>
              <span style={{ fontSize: 10, letterSpacing: 1 }}>{gateStr}</span>
              <span style={{ fontSize: 9, color: T.label, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {o.skip_reason || (o.v71_would_trade || o.v58_would_trade ? 'traded' : '\u2014')}
              </span>
              <span style={{
                textAlign: 'right',
                fontWeight: 700,
                color: result.color,
              }}>
                {result.text}
              </span>
            </div>
          );
        }) : (
          <div style={{ fontSize: 11, color: T.label, padding: '10px 0' }}>No recent outcomes</div>
        )}
      </Card>

      {/* ─── SECTION 5: Bottom Stats Row ───────────────────────────────────── */}
      <div className="ff-bottom-row" style={{
        display: 'flex',
        gap: 16,
      }}>
        {/* CONFIG */}
        <Card style={{ flex: 1 }}>
          <SectionLabel>CONFIG</SectionLabel>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px 16px', fontSize: 10 }}>
            {[
              ['VPIN Gate', '\u22650.45'],
              ['Delta Min', '\u22650.02%'],
              ['Floor', '$0.30'],
              ['Cap', '$0.83'],
              ['Stake', '$4.00'],
              ['Order Type', 'GTC'],
              ['Mode', paperMode ? 'PAPER' : 'LIVE'],
              ['Trade Rate', stats?.trade_rate_pct != null ? `${fmt(stats.trade_rate_pct, 1)}%` : '\u2014'],
            ].map(([k, v], i) => (
              <div key={i} style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: T.label }}>{k}</span>
                <span style={{ color: T.label2, fontWeight: 500 }}>{v}</span>
              </div>
            ))}
          </div>
        </Card>

        {/* PERFORMANCE */}
        <Card style={{ flex: 1 }}>
          <SectionLabel>PERFORMANCE</SectionLabel>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px 16px', fontSize: 10 }}>
            {[
              ['Wins', perf ? String(perf.wins) : '\u2014'],
              ['Losses', perf ? String(perf.losses) : '\u2014'],
              ['Streak', perf ? `${perf.streak > 0 ? '+' : ''}${perf.streak}` : '\u2014'],
              ['Cumulative P&L', perf ? `$${fmt(perf.pnl)}` : '\u2014'],
              ['Windows (7d)', stats?.total_windows != null ? String(stats.total_windows) : '\u2014'],
              ['Trades (7d)', stats?.trades_placed != null ? String(stats.trades_placed) : '\u2014'],
              ['Skipped (7d)', stats?.windows_skipped != null ? String(stats.windows_skipped) : '\u2014'],
              ['TimesFM Agree', stats?.timesfm_agreement != null ? `${fmt(stats.timesfm_agreement, 1)}%` : '\u2014'],
            ].map(([k, v], i) => {
              const isProfit = k === 'Cumulative P&L' && perf?.pnl > 0;
              const isLoss = k === 'Cumulative P&L' && perf?.pnl < 0;
              return (
                <div key={i} style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <span style={{ color: T.label }}>{k}</span>
                  <span style={{
                    fontWeight: 500,
                    color: isProfit ? T.profit : isLoss ? T.loss : T.label2,
                  }}>
                    {v}
                  </span>
                </div>
              );
            })}
          </div>
        </Card>
      </div>
    </div>
  );
}
