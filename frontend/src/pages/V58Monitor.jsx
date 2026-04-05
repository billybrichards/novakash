/**
 * V58Monitor.jsx — v5.8 BTC Trading Strategy Monitor
 *
 * Panels:
 *  1. Live BTC price chart (lightweight-charts candlestick)
 *  2. Window timeline — horizontal strip with colour-coded status pills
 *  3. Signal sources panel — Point / TWAP / Gamma / TimesFM per window
 *  4. v5.8 Agreement tracker — TimesFM vs v5.7c agreement rate
 *  5. Trade log — recent trades with entry price, direction, P&L
 *  6. Countdown status — live countdown for current window
 */

import React, {
  useState, useEffect, useRef, useCallback, useMemo
} from 'react';
import { createChart } from 'lightweight-charts';
import CountdownTimer from '../components/CountdownTimer.jsx';
import { useApi } from '../hooks/useApi.js';

// ─── Theme tokens ─────────────────────────────────────────────────────────────
const T = {
  bg:       '#07070c',
  card:     'rgba(255,255,255,0.018)',
  border:   'rgba(255,255,255,0.07)',
  purple:   '#a855f7',
  cyan:     '#06b6d4',
  profit:   '#4ade80',
  loss:     '#f87171',
  warning:  '#f59e0b',
  label:    'rgba(255,255,255,0.35)',
  label2:   'rgba(255,255,255,0.55)',
  mono:     "'IBM Plex Mono', monospace",
};

// ─── Inject font (once) ───────────────────────────────────────────────────────
if (!document.getElementById('ibm-plex-mono-font')) {
  const link = document.createElement('link');
  link.id = 'ibm-plex-mono-font';
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap';
  document.head.appendChild(link);
}

// ─── Window status helpers ────────────────────────────────────────────────────
function windowStatus(w) {
  if (!w.trade_placed) {
    return { label: 'SKIP', color: 'rgba(255,255,255,0.2)', bg: 'rgba(255,255,255,0.04)' };
  }
  // We don't store WIN/LOSS in the snapshot itself — use delta_pct as proxy
  if (w.delta_pct == null) {
    return { label: 'TRADE', color: T.cyan, bg: 'rgba(6,182,212,0.12)' };
  }
  if (w.direction === 'UP' && w.delta_pct > 0) {
    return { label: 'WIN', color: T.profit, bg: 'rgba(74,222,128,0.12)' };
  }
  if (w.direction === 'DOWN' && w.delta_pct < 0) {
    return { label: 'WIN', color: T.profit, bg: 'rgba(74,222,128,0.12)' };
  }
  return { label: 'LOSS', color: T.loss, bg: 'rgba(248,113,113,0.12)' };
}

function directionColor(dir) {
  if (!dir) return T.label;
  return dir === 'UP' ? T.profit : T.loss;
}

function confidenceBar(conf, color) {
  const pct = Math.round((conf ?? 0) * 100);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, width: '100%' }}>
      <div style={{
        flex: 1,
        height: 3,
        background: 'rgba(255,255,255,0.06)',
        borderRadius: 2,
        overflow: 'hidden',
      }}>
        <div style={{
          height: '100%',
          width: `${pct}%`,
          background: color,
          borderRadius: 2,
          boxShadow: `0 0 6px ${color}66`,
        }} />
      </div>
      <span style={{ fontSize: 10, color, fontFamily: T.mono, minWidth: 30, textAlign: 'right' }}>
        {pct}%
      </span>
    </div>
  );
}

// ─── StatCard (same pattern as Dashboard) ────────────────────────────────────
function StatCard({ label, value, sub, color = '#fff', icon }) {
  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.border}`,
      borderRadius: 12,
      padding: '14px 16px',
      fontFamily: T.mono,
      minWidth: 0,
    }}>
      <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 6 }}>
        {icon && <span style={{ marginRight: 4 }}>{icon}</span>}
        {label}
      </div>
      <div style={{ fontSize: 22, fontWeight: 700, color, lineHeight: 1.1, marginBottom: 2 }}>{value}</div>
      {sub && <div style={{ fontSize: 10, color: T.label }}>{sub}</div>}
    </div>
  );
}

// ─── Section header ───────────────────────────────────────────────────────────
function SectionHeader({ children }) {
  return (
    <div style={{
      fontSize: 10,
      color: T.purple,
      letterSpacing: '0.12em',
      marginBottom: 12,
      fontFamily: T.mono,
      fontWeight: 600,
      opacity: 0.75,
    }}>
      § {children}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// 1. BTC Price Chart (lightweight-charts)
// ═══════════════════════════════════════════════════════════════════════════════
function PriceChart({ candles, loading }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);

  // Init chart
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: '#08080e' },
        textColor: 'rgba(255,255,255,0.45)',
        fontFamily: "'IBM Plex Mono', monospace",
      },
      grid: {
        vertLines: { color: 'rgba(255,255,255,0.04)' },
        horzLines: { color: 'rgba(255,255,255,0.04)' },
      },
      crosshair: {
        mode: 1,
        vertLine: { color: T.purple, labelBackgroundColor: '#1a0a2e' },
        horzLine: { color: T.purple, labelBackgroundColor: '#1a0a2e' },
      },
      rightPriceScale: {
        borderColor: 'rgba(255,255,255,0.06)',
      },
      timeScale: {
        borderColor: 'rgba(255,255,255,0.06)',
        timeVisible: true,
        secondsVisible: false,
      },
      handleScroll: true,
      handleScale: true,
    });

    const series = chart.addCandlestickSeries({
      upColor: T.profit,
      downColor: T.loss,
      borderUpColor: T.profit,
      borderDownColor: T.loss,
      wickUpColor: 'rgba(74,222,128,0.6)',
      wickDownColor: 'rgba(248,113,113,0.6)',
    });

    chartRef.current = chart;
    seriesRef.current = series;

    // Resize observer
    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({
          width: containerRef.current.clientWidth,
          height: containerRef.current.clientHeight,
        });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Feed candles
  useEffect(() => {
    if (!seriesRef.current || !candles?.length) return;
    try {
      seriesRef.current.setData(candles);
      chartRef.current?.timeScale().fitContent();
    } catch (_) {}
  }, [candles]);

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.border}`,
      borderRadius: 12,
      overflow: 'hidden',
      position: 'relative',
    }}>
      <div style={{
        padding: '12px 16px 0',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
      }}>
        <div style={{ fontSize: 11, color: T.label, fontFamily: T.mono, letterSpacing: '0.06em' }}>
          BTC/USD — 5-min windows
        </div>
        {loading && (
          <div style={{ fontSize: 10, color: T.purple, fontFamily: T.mono }}>loading…</div>
        )}
      </div>
      <div
        ref={containerRef}
        style={{ width: '100%', height: 320 }}
      />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// 2. Window Timeline
// ═══════════════════════════════════════════════════════════════════════════════
function WindowTimeline({ windows, selectedTs, onSelect }) {
  const displayWindows = useMemo(() => [...(windows || [])].reverse(), [windows]);

  if (!displayWindows.length) {
    return (
      <div style={{ color: T.label, fontSize: 11, fontFamily: T.mono, padding: '12px 0' }}>
        No window data yet.
      </div>
    );
  }

  return (
    <div style={{ overflowX: 'auto', paddingBottom: 8 }}>
      <div style={{ display: 'flex', gap: 6, minWidth: 'max-content', paddingBottom: 4 }}>
        {displayWindows.map((w) => {
          const status = windowStatus(w);
          const ts = w.window_ts;
          const isSelected = selectedTs === ts;
          const time = ts ? new Date(ts).toLocaleTimeString('en-GB', {
            hour: '2-digit', minute: '2-digit',
          }) : '?';

          return (
            <button
              key={ts}
              onClick={() => onSelect(isSelected ? null : ts)}
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: 4,
                padding: '8px 10px',
                borderRadius: 8,
                border: `1px solid ${isSelected ? status.color : T.border}`,
                background: isSelected ? status.bg : T.card,
                cursor: 'pointer',
                fontFamily: T.mono,
                transition: 'all 150ms ease-out',
                boxShadow: isSelected ? `0 0 8px ${status.color}33` : 'none',
                minWidth: 60,
                flexShrink: 0,
              }}
            >
              <div style={{
                fontSize: 10,
                fontWeight: 700,
                color: status.color,
                letterSpacing: '0.04em',
              }}>
                {status.label}
              </div>
              <div style={{ fontSize: 9, color: T.label }}>{time}</div>
              {w.direction && (
                <div style={{
                  fontSize: 9,
                  color: directionColor(w.direction),
                  fontWeight: 600,
                }}>
                  {w.direction === 'UP' ? '▲' : '▼'}
                </div>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// 3. Signal Sources Panel
// ═══════════════════════════════════════════════════════════════════════════════
function SignalSourcesPanel({ window: w }) {
  if (!w) {
    return (
      <div style={{ color: T.label, fontSize: 11, fontFamily: T.mono, textAlign: 'center', padding: 24 }}>
        Select a window from the timeline above
      </div>
    );
  }

  const ts = w.window_ts
    ? new Date(w.window_ts).toLocaleString('en-GB', {
        dateStyle: 'short', timeStyle: 'medium',
      })
    : '?';

  const status = windowStatus(w);

  const sources = [
    {
      label: 'Point (v5.7c)',
      icon: '📍',
      direction: w.direction,
      confidence: w.confidence,
      color: T.purple,
      extra: w.vpin != null ? `VPIN ${w.vpin.toFixed(3)}` : null,
    },
    {
      label: 'TWAP',
      icon: '📊',
      direction: w.twap_direction,
      confidence: w.twap_agreement_score,
      color: T.cyan,
      extra: w.twap_gamma_gate != null
        ? `Gamma gate: ${w.twap_gamma_gate ? '✅' : '❌'}`
        : null,
    },
    {
      label: 'Gamma',
      icon: '⚡',
      direction: null,
      confidence: null,
      color: T.warning,
      extra: [
        w.gamma_up_price != null ? `↑ $${w.gamma_up_price.toFixed(0)}` : null,
        w.gamma_down_price != null ? `↓ $${w.gamma_down_price.toFixed(0)}` : null,
      ].filter(Boolean).join('  ') || null,
    },
    {
      label: 'TimesFM',
      icon: '🔮',
      direction: w.timesfm_direction,
      confidence: w.timesfm_confidence,
      color: '#e879f9',
      extra: w.timesfm_predicted_close != null
        ? `Pred close: $${w.timesfm_predicted_close.toFixed(0)}`
        : null,
    },
  ];

  return (
    <div style={{ fontFamily: T.mono }}>
      {/* Window header */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: 14,
        flexWrap: 'wrap',
        gap: 8,
      }}>
        <div>
          <div style={{ fontSize: 10, color: T.label, marginBottom: 2 }}>{ts}</div>
          <div style={{ fontSize: 12, color: '#fff' }}>
            {w.asset || 'BTC'} · {w.timeframe || '5m'} ·{' '}
            <span style={{ color: status.color, fontWeight: 700 }}>{status.label}</span>
          </div>
        </div>
        {w.skip_reason && (
          <div style={{
            padding: '4px 10px',
            borderRadius: 6,
            background: 'rgba(255,255,255,0.04)',
            border: '1px solid rgba(255,255,255,0.1)',
            color: T.label2,
            fontSize: 10,
          }}>
            Skip: {w.skip_reason}
          </div>
        )}
      </div>

      {/* Signal grid */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(2, 1fr)',
        gap: 10,
      }}>
        {sources.map((src) => (
          <div
            key={src.label}
            style={{
              padding: '12px 14px',
              borderRadius: 10,
              background: 'rgba(0,0,0,0.25)',
              border: `1px solid ${src.color}30`,
              position: 'relative',
              overflow: 'hidden',
            }}
          >
            {/* Top accent line */}
            <div style={{
              position: 'absolute',
              top: 0, left: 0, right: 0, height: 2,
              background: src.color,
              opacity: 0.5,
            }} />

            <div style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: 8,
            }}>
              <div style={{ fontSize: 10, color: src.color, fontWeight: 600, letterSpacing: '0.06em' }}>
                {src.icon} {src.label}
              </div>
              {src.direction && (
                <div style={{
                  fontSize: 12,
                  fontWeight: 700,
                  color: directionColor(src.direction),
                }}>
                  {src.direction === 'UP' ? '▲ UP' : '▼ DOWN'}
                </div>
              )}
            </div>

            {src.confidence != null && (
              <div style={{ marginBottom: 6 }}>
                {confidenceBar(src.confidence, src.color)}
              </div>
            )}

            {src.extra && (
              <div style={{ fontSize: 10, color: T.label, marginTop: 4 }}>
                {src.extra}
              </div>
            )}

            {/* TimesFM agreement badge */}
            {src.label === 'TimesFM' && w.timesfm_agreement != null && (
              <div style={{
                marginTop: 6,
                display: 'inline-flex',
                alignItems: 'center',
                gap: 4,
                padding: '2px 8px',
                borderRadius: 4,
                background: w.timesfm_agreement ? 'rgba(74,222,128,0.1)' : 'rgba(248,113,113,0.1)',
                border: `1px solid ${w.timesfm_agreement ? 'rgba(74,222,128,0.3)' : 'rgba(248,113,113,0.3)'}`,
                fontSize: 9,
                color: w.timesfm_agreement ? T.profit : T.loss,
                fontWeight: 700,
                letterSpacing: '0.06em',
              }}>
                {w.timesfm_agreement ? '✓ AGREE' : '✗ DISAGREE'}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Price info */}
      {(w.open_price || w.close_price) && (
        <div style={{
          display: 'flex',
          gap: 12,
          marginTop: 12,
          padding: '10px 14px',
          borderRadius: 8,
          background: 'rgba(255,255,255,0.03)',
          border: `1px solid ${T.border}`,
        }}>
          {w.open_price != null && (
            <div>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>OPEN</div>
              <div style={{ fontSize: 13, color: '#fff', fontWeight: 600 }}>
                ${w.open_price.toLocaleString('en-US', { minimumFractionDigits: 0 })}
              </div>
            </div>
          )}
          {w.close_price != null && (
            <div>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>CLOSE</div>
              <div style={{ fontSize: 13, color: '#fff', fontWeight: 600 }}>
                ${w.close_price.toLocaleString('en-US', { minimumFractionDigits: 0 })}
              </div>
            </div>
          )}
          {w.delta_pct != null && (
            <div>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>Δ%</div>
              <div style={{
                fontSize: 13,
                fontWeight: 700,
                color: w.delta_pct >= 0 ? T.profit : T.loss,
              }}>
                {w.delta_pct >= 0 ? '+' : ''}{(w.delta_pct * 100).toFixed(3)}%
              </div>
            </div>
          )}
          {w.regime && (
            <div>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>REGIME</div>
              <div style={{ fontSize: 11, color: T.warning, fontWeight: 600 }}>{w.regime}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// 4. Agreement Tracker
// ═══════════════════════════════════════════════════════════════════════════════
function AgreementTracker({ stats }) {
  const tfm = stats?.timesfm ?? {};
  const rate = tfm.agreement_rate_pct ?? 0;
  const evaluated = tfm.evaluated ?? 0;
  const agreed = tfm.agreed ?? 0;
  const disagreed = tfm.disagreed ?? 0;

  const color = rate >= 70 ? T.profit : rate >= 50 ? T.warning : T.loss;

  return (
    <div style={{ fontFamily: T.mono }}>
      {/* Big number */}
      <div style={{
        display: 'flex',
        alignItems: 'flex-end',
        gap: 8,
        marginBottom: 14,
      }}>
        <div style={{ fontSize: 40, fontWeight: 700, color, lineHeight: 1 }}>
          {rate.toFixed(1)}%
        </div>
        <div style={{ fontSize: 11, color: T.label, paddingBottom: 6 }}>
          TimesFM agreement
        </div>
      </div>

      {/* Bar */}
      <div style={{
        height: 6,
        background: 'rgba(255,255,255,0.06)',
        borderRadius: 3,
        overflow: 'hidden',
        marginBottom: 14,
      }}>
        <div style={{
          height: '100%',
          width: `${rate}%`,
          background: color,
          borderRadius: 3,
          boxShadow: `0 0 8px ${color}66`,
          transition: 'width 600ms ease-out',
        }} />
      </div>

      {/* Counts */}
      <div style={{ display: 'flex', gap: 12 }}>
        <div style={{
          flex: 1, padding: '8px 10px', borderRadius: 8,
          background: 'rgba(74,222,128,0.06)',
          border: '1px solid rgba(74,222,128,0.15)',
        }}>
          <div style={{ fontSize: 9, color: T.label, marginBottom: 4, letterSpacing: '0.08em' }}>AGREED</div>
          <div style={{ fontSize: 18, fontWeight: 700, color: T.profit }}>{agreed}</div>
        </div>
        <div style={{
          flex: 1, padding: '8px 10px', borderRadius: 8,
          background: 'rgba(248,113,113,0.06)',
          border: '1px solid rgba(248,113,113,0.15)',
        }}>
          <div style={{ fontSize: 9, color: T.label, marginBottom: 4, letterSpacing: '0.08em' }}>DISAGREED</div>
          <div style={{ fontSize: 18, fontWeight: 700, color: T.loss }}>{disagreed}</div>
        </div>
        <div style={{
          flex: 1, padding: '8px 10px', borderRadius: 8,
          background: 'rgba(255,255,255,0.03)',
          border: '1px solid rgba(255,255,255,0.06)',
        }}>
          <div style={{ fontSize: 9, color: T.label, marginBottom: 4, letterSpacing: '0.08em' }}>EVALUATED</div>
          <div style={{ fontSize: 18, fontWeight: 700, color: '#fff' }}>{evaluated}</div>
        </div>
      </div>

      {/* TWAP summary */}
      {stats?.twap && (
        <div style={{
          marginTop: 12,
          padding: '8px 12px',
          borderRadius: 8,
          background: 'rgba(6,182,212,0.05)',
          border: '1px solid rgba(6,182,212,0.15)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}>
          <span style={{ fontSize: 10, color: T.label }}>TWAP gamma gate passed</span>
          <span style={{ fontSize: 13, fontWeight: 700, color: T.cyan }}>
            {stats.twap.gate_passed ?? 0}
          </span>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// 5. Trade Log (uses windows data — windows where trade_placed=true)
// ═══════════════════════════════════════════════════════════════════════════════
function TradeLog({ windows }) {
  const trades = useMemo(
    () => (windows || []).filter(w => w.trade_placed),
    [windows]
  );

  if (!trades.length) {
    return (
      <div style={{ color: T.label, fontSize: 11, fontFamily: T.mono, padding: '12px 0' }}>
        No trades placed in this dataset.
      </div>
    );
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{
        width: '100%',
        borderCollapse: 'collapse',
        fontFamily: T.mono,
        fontSize: 11,
        minWidth: 480,
      }}>
        <thead>
          <tr style={{ borderBottom: `1px solid ${T.border}` }}>
            {['Time', 'Dir', 'Entry', 'Conf', 'TsFM', 'Δ%', 'Result'].map(h => (
              <th key={h} style={{
                padding: '6px 10px',
                textAlign: 'left',
                color: T.label,
                fontWeight: 600,
                fontSize: 9,
                letterSpacing: '0.08em',
                whiteSpace: 'nowrap',
              }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {trades.map((w) => {
            const status = windowStatus(w);
            const time = w.window_ts
              ? new Date(w.window_ts).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
              : '—';
            const conf = w.confidence != null ? `${Math.round(w.confidence * 100)}%` : '—';
            const delta = w.delta_pct != null
              ? `${w.delta_pct >= 0 ? '+' : ''}${(w.delta_pct * 100).toFixed(3)}%`
              : '—';
            const entry = w.open_price != null
              ? `$${w.open_price.toLocaleString('en-US', { maximumFractionDigits: 0 })}`
              : '—';
            const tsfmAgreement = w.timesfm_agreement == null
              ? '—'
              : w.timesfm_agreement ? '✓' : '✗';

            return (
              <tr
                key={w.window_ts}
                style={{
                  borderBottom: `1px solid rgba(255,255,255,0.03)`,
                  transition: 'background 120ms',
                }}
                onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.03)')}
                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
              >
                <td style={{ padding: '7px 10px', color: T.label }}>{time}</td>
                <td style={{ padding: '7px 10px', color: directionColor(w.direction), fontWeight: 700 }}>
                  {w.direction === 'UP' ? '▲' : w.direction === 'DOWN' ? '▼' : '—'}
                </td>
                <td style={{ padding: '7px 10px', color: '#fff' }}>{entry}</td>
                <td style={{ padding: '7px 10px', color: T.purple }}>{conf}</td>
                <td style={{
                  padding: '7px 10px',
                  color: w.timesfm_agreement === true
                    ? T.profit
                    : w.timesfm_agreement === false
                    ? T.loss
                    : T.label,
                  fontWeight: 700,
                }}>
                  {tsfmAgreement}
                </td>
                <td style={{
                  padding: '7px 10px',
                  color: w.delta_pct == null ? T.label : w.delta_pct >= 0 ? T.profit : T.loss,
                }}>
                  {delta}
                </td>
                <td style={{ padding: '7px 10px' }}>
                  <span style={{
                    padding: '2px 8px',
                    borderRadius: 4,
                    background: status.bg,
                    color: status.color,
                    fontSize: 9,
                    fontWeight: 700,
                    letterSpacing: '0.04em',
                  }}>
                    {status.label}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN PAGE
// ═══════════════════════════════════════════════════════════════════════════════
export default function V58Monitor() {
  const api = useApi();

  const [windows, setWindows] = useState([]);
  const [stats, setStats] = useState(null);
  const [candles, setCandles] = useState([]);
  const [selectedTs, setSelectedTs] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [wsStatus, setWsStatus] = useState('OFFLINE');

  // Derived: the selected window object
  const selectedWindow = useMemo(
    () => windows.find(w => w.window_ts === selectedTs) ?? windows[0] ?? null,
    [windows, selectedTs]
  );

  // Latest (most recent) window = first in the array (API returns newest-first)
  const latestWindow = windows[0] ?? null;

  // ── Fetch ───────────────────────────────────────────────────────────────────
  const fetchAll = useCallback(async () => {
    try {
      const [windowsRes, statsRes, priceRes] = await Promise.allSettled([
        api('GET', '/v58/windows?limit=50'),
        api('GET', '/v58/stats?days=7'),
        api('GET', '/v58/price-history?minutes=60'),
      ]);

      if (windowsRes.status === 'fulfilled') {
        const data = windowsRes.value?.data?.windows ?? [];
        setWindows(data);
      }

      if (statsRes.status === 'fulfilled') {
        setStats(statsRes.value?.data ?? null);
      }

      if (priceRes.status === 'fulfilled') {
        const rawCandles = priceRes.value?.data?.candles ?? [];
        // lightweight-charts needs { time, open, high, low, close } with numeric time
        setCandles(rawCandles.filter(c => c.time && c.open && c.close));
      }

      setLastRefresh(new Date());
    } catch (err) {
      console.error('[V58Monitor] fetch error:', err);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 15000); // refresh every 15s
    return () => clearInterval(id);
  }, [fetchAll]);

  // ── Live WebSocket ──────────────────────────────────────────────────────────
  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${window.location.host}/ws/live`;
    let ws = null;
    let reconnectId = null;

    function connect() {
      try {
        ws = new WebSocket(wsUrl);

        ws.onopen = () => setWsStatus('LIVE');
        ws.onclose = () => {
          setWsStatus('RECONNECTING');
          reconnectId = setTimeout(connect, 5000);
        };
        ws.onerror = () => {
          setWsStatus('ERROR');
        };
        ws.onmessage = (evt) => {
          try {
            const msg = JSON.parse(evt.data);
            // Refresh data when a new signal arrives
            if (['signal', 'trade', 'window'].includes(msg?.type)) {
              fetchAll();
            }
          } catch (_) {}
        };
      } catch (_) {
        setWsStatus('ERROR');
      }
    }

    connect();
    return () => {
      if (reconnectId) clearTimeout(reconnectId);
      if (ws) ws.close();
    };
  }, [fetchAll]);

  // ── Stat summaries ──────────────────────────────────────────────────────────
  const tradedCount = stats?.trades_placed ?? 0;
  const skippedCount = stats?.windows_skipped ?? 0;
  const totalCount = stats?.total_windows ?? 0;
  const tradeRate = stats?.trade_rate_pct ?? 0;
  const agreementRate = stats?.timesfm?.agreement_rate_pct ?? 0;

  return (
    <div style={{
      background: T.bg,
      minHeight: '100vh',
      fontFamily: T.mono,
      color: '#fff',
      padding: '0 0 40px',
    }}>
      {/* Inject pulse animations */}
      <style>{`
        @keyframes v58pulse { 0%,100%{opacity:1} 50%{opacity:0.35} }
        @media (max-width: 768px) {
          .v58-grid-2 { grid-template-columns: 1fr !important; }
          .v58-grid-3 { grid-template-columns: 1fr !important; }
          .v58-signal-grid { grid-template-columns: 1fr !important; }
        }
      `}</style>

      {/* ── Header bar ─────────────────────────────────────────────────── */}
      <div style={{
        background: 'rgba(255,255,255,0.018)',
        borderBottom: `1px solid ${T.border}`,
        padding: '10px 20px',
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        flexWrap: 'wrap',
      }}>
        <span style={{
          color: T.purple,
          fontSize: 13,
          fontWeight: 700,
          letterSpacing: '0.08em',
          marginRight: 4,
        }}>
          ◈ v5.8 MONITOR
        </span>

        {/* Stats pills */}
        {[
          { label: 'Total', value: totalCount, color: '#fff' },
          { label: 'Traded', value: tradedCount, color: T.cyan },
          { label: 'Skipped', value: skippedCount, color: T.label2 },
          { label: 'Trade Rate', value: `${tradeRate}%`, color: T.warning },
          { label: 'TsFM Agreement', value: `${agreementRate.toFixed(1)}%`, color: agreementRate >= 70 ? T.profit : agreementRate >= 50 ? T.warning : T.loss },
        ].map(({ label, value, color }) => (
          <div key={label} style={{
            background: T.card,
            border: `1px solid ${T.border}`,
            borderRadius: 6,
            padding: '4px 12px',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}>
            <span style={{ fontSize: 9, color: T.label, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              {label}
            </span>
            <span style={{ fontSize: 12, fontWeight: 700, color }}>{value}</span>
          </div>
        ))}

        {/* WS status dot */}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{
            width: 7, height: 7, borderRadius: '50%',
            background: wsStatus === 'LIVE' ? T.profit : wsStatus === 'RECONNECTING' ? T.warning : T.loss,
            boxShadow: wsStatus === 'LIVE' ? `0 0 6px ${T.profit}` : 'none',
            animation: wsStatus === 'LIVE' ? 'v58pulse 2s infinite' : 'none',
            display: 'inline-block',
          }} />
          <span style={{ fontSize: 10, color: T.label }}>{wsStatus}</span>
        </div>

        {lastRefresh && (
          <span style={{ fontSize: 9, color: T.label }}>
            {lastRefresh.toLocaleTimeString('en-GB')}
          </span>
        )}

        {loading && (
          <span style={{ fontSize: 10, color: T.purple }}>loading…</span>
        )}
      </div>

      {/* ── Body ───────────────────────────────────────────────────────── */}
      <div style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: 20 }}>

        {/* § LIVE BTC PRICE CHART */}
        <section>
          <SectionHeader>LIVE BTC PRICE</SectionHeader>
          <PriceChart candles={candles} loading={loading} />
        </section>

        {/* § WINDOW TIMELINE */}
        <section>
          <SectionHeader>WINDOW TIMELINE — last {windows.length} windows</SectionHeader>
          <div style={{
            background: T.card,
            border: `1px solid ${T.border}`,
            borderRadius: 12,
            padding: '16px',
          }}>
            <WindowTimeline
              windows={windows}
              selectedTs={selectedTs ?? latestWindow?.window_ts}
              onSelect={setSelectedTs}
            />
          </div>
        </section>

        {/* § SIGNAL SOURCES + COUNTDOWN */}
        <section>
          <SectionHeader>SIGNAL SOURCES + COUNTDOWN</SectionHeader>
          <div
            className="v58-grid-2"
            style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}
          >
            {/* Signal sources */}
            <div style={{
              background: T.card,
              border: `1px solid ${T.border}`,
              borderRadius: 12,
              padding: '16px',
            }}>
              <SignalSourcesPanel window={selectedWindow} />
            </div>

            {/* Countdown */}
            <div style={{
              background: T.card,
              border: `1px solid ${T.border}`,
              borderRadius: 12,
              padding: '20px',
              display: 'flex',
              flexDirection: 'column',
            }}>
              <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 14 }}>
                § CURRENT WINDOW COUNTDOWN
              </div>
              <CountdownTimer
                windowTs={latestWindow?.window_ts}
              />
            </div>
          </div>
        </section>

        {/* § AGREEMENT + STATS */}
        <section>
          <SectionHeader>v5.8 AGREEMENT TRACKER</SectionHeader>
          <div
            className="v58-grid-2"
            style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}
          >
            {/* Agreement panel */}
            <div style={{
              background: T.card,
              border: `1px solid ${T.border}`,
              borderRadius: 12,
              padding: '20px',
            }}>
              <AgreementTracker stats={stats} />
            </div>

            {/* Stats grid */}
            <div
              className="v58-grid-2"
              style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}
            >
              <StatCard
                label="Total Windows"
                value={totalCount}
                color="#fff"
                icon="🪟"
              />
              <StatCard
                label="Trades Placed"
                value={tradedCount}
                color={T.cyan}
                icon="🎯"
                sub={`${tradeRate}% rate`}
              />
              <StatCard
                label="Direction UP"
                value={stats?.direction?.up ?? 0}
                color={T.profit}
                icon="▲"
              />
              <StatCard
                label="Direction DOWN"
                value={stats?.direction?.down ?? 0}
                color={T.loss}
                icon="▼"
              />
              <StatCard
                label="Avg Confidence"
                value={stats?.confidence?.avg != null
                  ? `${Math.round(stats.confidence.avg * 100)}%`
                  : '—'}
                color={T.purple}
                icon="📊"
              />
              <StatCard
                label="TWAP Gate OK"
                value={stats?.twap?.gate_passed ?? 0}
                color={T.cyan}
                icon="✅"
              />
            </div>
          </div>
        </section>

        {/* § TRADE LOG */}
        <section>
          <SectionHeader>TRADE LOG — windows where trade was placed</SectionHeader>
          <div style={{
            background: T.card,
            border: `1px solid ${T.border}`,
            borderRadius: 12,
            padding: '16px',
          }}>
            <TradeLog windows={windows} />
          </div>
        </section>

      </div>
    </div>
  );
}
