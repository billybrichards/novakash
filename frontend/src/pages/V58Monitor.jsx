/**
 * V58Monitor.jsx — v7 BTC Trading Strategy Monitor
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
  // v8.0: Use actual Polymarket outcome when available (from /v58/outcomes trades JOIN)
  if (w.poly_outcome === 'WIN') {
    return { label: 'WIN', color: T.profit, bg: 'rgba(74,222,128,0.12)' };
  }
  if (w.poly_outcome === 'LOSS') {
    return { label: 'LOSS', color: T.loss, bg: 'rgba(248,113,113,0.12)' };
  }
  // Fallback for windows without oracle resolution yet: use directional match
  if (w.delta_pct == null) {
    return { label: 'TRADE', color: T.cyan, bg: 'rgba(6,182,212,0.12)' };
  }
  const dirMatch = (w.direction === 'UP' && w.delta_pct > 0) || (w.direction === 'DOWN' && w.delta_pct < 0);
  if (dirMatch) {
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

          // Prediction direction (v5.7c)
          const prediction = w.direction; // UP or DOWN

          // Actual outcome direction (from open/close prices)
          let actualDirection = null;
          if (w.open_price != null && w.close_price != null) {
            actualDirection = w.close_price > w.open_price ? 'UP' : 'DOWN';
          }

          // Was prediction correct? Use v71_correct (actual trade outcome) if available,
          // otherwise fall back to directional match
          const predCorrect = w.v71_correct !== null && w.v71_correct !== undefined
            ? w.v71_correct
            : (prediction && actualDirection ? prediction === actualDirection : null);

          // Legacy decision (what actually happened)
          const tradeLabel = w.trade_placed ? 'TRADE' : 'SKIP';
          const labelColor = w.trade_placed ? T.cyan : T.label;
          
          // v7.1 Retroactive decision (what would happen with current config)
          const v71Trade = w.v71_would_trade ? 'TRADE' : 'SKIP';
          const v71Color = w.v71_would_trade ? '#a855f7' : T.label2; // purple for v7.1

          return (
            <button
              key={ts}
              onClick={() => onSelect(isSelected ? null : ts)}
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: 3,
                padding: '8px 10px',
                borderRadius: 8,
                border: `1px solid ${isSelected ? status.color : T.border}`,
                background: isSelected ? status.bg : T.card,
                cursor: 'pointer',
                fontFamily: T.mono,
                transition: 'all 150ms ease-out',
                boxShadow: isSelected ? `0 0 8px ${status.color}33` : 'none',
                minWidth: 68,
                flexShrink: 0,
              }}
              title={`Legacy: ${tradeLabel}${w.skip_reason ? ' — ' + w.skip_reason : ''}\nv7.1: ${v71Trade}${w.v71_regime ? ' (' + w.v71_regime + ')' : ''}${w.v71_skip_reason ? ' — ' + w.v71_skip_reason : ''}`}
            >
              {/* Time */}
              <div style={{ fontSize: 10, color: T.label, fontWeight: 600 }}>{time}</div>

              {/* Signal: our prediction direction */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                <span style={{ fontSize: 7, color: T.label2 }}>SIG</span>
                {prediction ? (
                  <span style={{ fontSize: 13, fontWeight: 700, color: directionColor(prediction) }}>
                    {prediction === 'UP' ? '▲' : '▼'}
                  </span>
                ) : (
                  <span style={{ fontSize: 10, color: T.label }}>—</span>
                )}
              </div>

              {/* Outcome: what actually happened — WIN or LOSS */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                <span style={{ fontSize: 7, color: T.label2 }}>OUT</span>
                {w.v71_correct === true ? (
                  <span style={{ fontSize: 11, fontWeight: 800, color: T.profit }}>WIN ✓</span>
                ) : w.v71_correct === false ? (
                  <span style={{ fontSize: 11, fontWeight: 800, color: T.loss }}>LOSS ✗</span>
                ) : actualDirection ? (
                  <span style={{ fontSize: 12, fontWeight: 700, color: T.label2 }}>
                    {actualDirection === 'UP' ? '▲' : '▼'}
                  </span>
                ) : (
                  <span style={{ fontSize: 9, color: T.label }}>…</span>
                )}
              </div>

              {/* v7.1 Decision — clear label */}
              <div style={{
                padding: '2px 5px',
                borderRadius: 3,
                fontSize: 8,
                fontWeight: 700,
                letterSpacing: '0.04em',
                background: w.v71_would_trade 
                  ? (w.v71_correct === true ? 'rgba(74,222,128,0.2)' : w.v71_correct === false ? 'rgba(248,113,113,0.2)' : 'rgba(168,85,247,0.15)')
                  : 'rgba(255,255,255,0.05)',
                color: w.v71_would_trade
                  ? (w.v71_correct === true ? '#22c55e' : w.v71_correct === false ? '#ef4444' : '#a855f7')
                  : T.label,
                border: `1px solid ${w.v71_would_trade ? 'rgba(168,85,247,0.3)' : 'rgba(255,255,255,0.08)'}`,
              }}>
                {w.v71_would_trade 
                  ? (w.v71_correct === true ? '7.1 ✅' : w.v71_correct === false ? '7.1 ❌' : '7.1 📊')
                  : '7.1 ⏭'}
              </div>

              {/* Traded badge — did we actually place? */}
              {w.trade_placed && (
                <div style={{
                  padding: '1px 4px',
                  borderRadius: 3,
                  fontSize: 7,
                  fontWeight: 800,
                  background: 'rgba(34,197,94,0.15)',
                  color: '#22c55e',
                  border: '1px solid rgba(34,197,94,0.3)',
                  letterSpacing: '0.06em',
                }}>
                  💰 TRADED
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
            <span style={{ color: '#666', fontWeight: 600 }}>[LEGACY]</span> Skip: {w.skip_reason}
          </div>
        )}
        {w.v71_skip_reason && (
          <div style={{
            padding: '4px 10px',
            borderRadius: 6,
            background: 'rgba(168,85,247,0.06)',
            border: '1px solid rgba(168,85,247,0.2)',
            color: '#c084fc',
            fontSize: 10,
          }}>
            <span style={{ fontWeight: 600 }}>[v7.1]</span> Skip: {w.v71_skip_reason}
          </div>
        )}
        {w.v71_would_trade && !w.v71_skip_reason && (
          <div style={{
            padding: '4px 10px',
            borderRadius: 6,
            background: 'rgba(168,85,247,0.06)',
            border: '1px solid rgba(168,85,247,0.2)',
            color: '#a855f7',
            fontSize: 10,
            fontWeight: 600,
          }}>
            [v7.1] ✅ TRADE — {w.v71_regime || 'NORMAL'} regime
            {w.v71_correct !== null && w.v71_correct !== undefined && (
              <span style={{ color: w.v71_correct ? '#22c55e' : '#ef4444', marginLeft: 8 }}>
                {w.v71_correct ? '✓ WIN' : '✗ LOSS'}{w.v71_pnl ? ` (${w.v71_pnl > 0 ? '+' : ''}$${w.v71_pnl.toFixed(2)})` : ''}
              </span>
            )}
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
// A. Accuracy Scoreboard
// ═══════════════════════════════════════════════════════════════════════════════

function ProgressRing({ pct, color, size = 56, stroke = 5 }) {
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (pct / 100) * circumference;
  return (
    <svg width={size} height={size} style={{ transform: 'rotate(-90deg)', flexShrink: 0 }}>
      <circle
        cx={size / 2} cy={size / 2} r={radius}
        fill="none" stroke="rgba(255,255,255,0.07)" strokeWidth={stroke}
      />
      <circle
        cx={size / 2} cy={size / 2} r={radius}
        fill="none" stroke={color} strokeWidth={stroke}
        strokeDasharray={circumference}
        strokeDashoffset={offset}
        strokeLinecap="round"
        style={{ transition: 'stroke-dashoffset 600ms ease-out' }}
      />
    </svg>
  );
}

function AccuracyCard({ label, pct, sub, color }) {
  const displayPct = typeof pct === 'number' ? pct : 0;
  const ringColor = displayPct >= 70 ? T.profit : displayPct >= 50 ? T.warning : T.loss;
  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.border}`,
      borderRadius: 12,
      padding: '14px 16px',
      display: 'flex',
      alignItems: 'center',
      gap: 14,
      fontFamily: T.mono,
    }}>
      <div style={{ position: 'relative', width: 56, height: 56, flexShrink: 0 }}>
        <ProgressRing pct={displayPct} color={color ?? ringColor} size={56} stroke={5} />
        <div style={{
          position: 'absolute', inset: 0, display: 'flex',
          alignItems: 'center', justifyContent: 'center',
          fontSize: 11, fontWeight: 700, color: color ?? ringColor,
        }}>
          {displayPct.toFixed(0)}%
        </div>
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 4 }}>
          {label}
        </div>
        <div style={{ fontSize: 20, fontWeight: 700, color: color ?? ringColor, lineHeight: 1 }}>
          {displayPct.toFixed(1)}%
        </div>
        {sub && <div style={{ fontSize: 10, color: T.label, marginTop: 3 }}>{sub}</div>}
      </div>
    </div>
  );
}

function AccuracyScoreboard({ accuracy }) {
  if (!accuracy) {
    return <div style={{ color: T.label, fontSize: 11, fontFamily: T.mono, padding: 16 }}>Loading accuracy data…</div>;
  }

  const pnlColor = accuracy.cumulative_pnl >= 0 ? T.profit : T.loss;
  const streakColor = accuracy.current_streak > 0 ? T.profit : T.label;

  return (
    <div style={{ fontFamily: T.mono }}>
      {/* Main accuracy cards */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
        gap: 10,
        marginBottom: 14,
      }}>
        <AccuracyCard
          label="TimesFM Accuracy"
          pct={accuracy.timesfm_accuracy}
          sub={`${accuracy.windows_analysed} windows`}
          color="#e879f9"
        />
        <AccuracyCard
          label="v5.7c Accuracy"
          pct={accuracy.v57c_accuracy}
          sub="Final signal call"
          color={T.purple}
        />
        <AccuracyCard
          label="v5.8 Accuracy"
          pct={accuracy.v58_accuracy}
          sub={`${accuracy.v58_trades_count} trades`}
          color={T.cyan}
        />
        <AccuracyCard
          label="TWAP Accuracy"
          pct={accuracy.twap_accuracy}
          sub="TWAP direction"
          color={T.cyan}
        />
        <AccuracyCard
          label="v7.1 Win Rate"
          pct={accuracy.v71_accuracy}
          sub={`${accuracy.v71_wins || 0}W / ${accuracy.v71_losses || 0}L — ${accuracy.v71_resolved_count || 0} resolved`}
          color="#a855f7"
        />
      </div>

      {/* v7.1 P&L + Streak row */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(3, 1fr)',
        gap: 10,
        marginTop: 10,
      }}>
        <div style={{
          background: 'rgba(168,85,247,0.06)',
          border: '1px solid rgba(168,85,247,0.2)',
          borderRadius: 10,
          padding: '12px 14px',
        }}>
          <div style={{ fontSize: 9, color: '#c084fc', fontWeight: 600, letterSpacing: '0.08em', marginBottom: 4 }}>v7.1 P&L</div>
          <div style={{ fontSize: 18, fontWeight: 800, color: (accuracy.v71_pnl || 0) >= 0 ? '#22c55e' : '#ef4444', fontFamily: T.mono }}>
            {(accuracy.v71_pnl || 0) >= 0 ? '+' : ''}${(accuracy.v71_pnl || 0).toFixed(2)}
          </div>
          <div style={{ fontSize: 9, color: '#c084fc', marginTop: 2 }}>{accuracy.v71_trades_count || 0} eligible trades</div>
        </div>
        <div style={{
          background: 'rgba(168,85,247,0.06)',
          border: '1px solid rgba(168,85,247,0.2)',
          borderRadius: 10,
          padding: '12px 14px',
        }}>
          <div style={{ fontSize: 9, color: '#c084fc', fontWeight: 600, letterSpacing: '0.08em', marginBottom: 4 }}>v7.1 STREAK</div>
          <div style={{ fontSize: 18, fontWeight: 800, color: '#a855f7', fontFamily: T.mono }}>
            {accuracy.v71_streak || 0} {(accuracy.v71_streak || 0) === 1 ? 'WIN' : 'WINS'}
          </div>
        </div>
      </div>

      {/* Secondary stats row */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(3, 1fr)',
        gap: 10,
      }}>
        {/* Agreement rate */}
        <div style={{
          background: T.card,
          border: `1px solid ${T.border}`,
          borderRadius: 10,
          padding: '12px 14px',
        }}>
          <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 6 }}>
            Agreement Rate
          </div>
          <div style={{ fontSize: 20, fontWeight: 700, color: accuracy.agreement_rate >= 60 ? T.profit : T.warning }}>
            {accuracy.agreement_rate.toFixed(1)}%
          </div>
          <div style={{ fontSize: 10, color: T.label }}>TimesFM ↔ v5.7c</div>
        </div>

        {/* Cumulative P&L */}
        <div style={{
          background: T.card,
          border: `1px solid ${pnlColor}30`,
          borderRadius: 10,
          padding: '12px 14px',
        }}>
          <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 6 }}>
            v5.8 P&L (gated)
          </div>
          <div style={{ fontSize: 20, fontWeight: 700, color: pnlColor }}>
            {accuracy.cumulative_pnl >= 0 ? '+' : ''}${accuracy.cumulative_pnl.toFixed(2)}
          </div>
          <div style={{ fontSize: 10, color: T.label }}>{accuracy.v58_trades_count || 0} trades</div>
        </div>

        {/* Ungated P&L */}
        <div style={{
          background: T.card,
          border: `1px solid ${T.border}`,
          borderRadius: 10,
          padding: '12px 14px',
        }}>
          <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 6 }}>
            Ungated P&L (all signals)
          </div>
          <div style={{ fontSize: 20, fontWeight: 700, color: (accuracy.ungated_pnl || 0) >= 0 ? T.profit : T.loss }}>
            {(accuracy.ungated_pnl || 0) >= 0 ? '+' : ''}${(accuracy.ungated_pnl || 0).toFixed(2)}
          </div>
          <div style={{ fontSize: 10, color: T.label }}>
            {accuracy.ungated_wins || 0}W / {accuracy.ungated_losses || 0}L ({accuracy.ungated_accuracy || 0}%)
          </div>
        </div>

        {/* Gate Value */}
        <div style={{
          background: T.card,
          border: `1px solid ${(accuracy.gate_value || 0) > 0 ? 'rgba(74,222,128,0.2)' : 'rgba(248,113,113,0.2)'}`,
          borderRadius: 10,
          padding: '12px 14px',
        }}>
          <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 6 }}>
            Gate Value
          </div>
          <div style={{ fontSize: 20, fontWeight: 700, color: (accuracy.gate_value || 0) > 0 ? T.loss : T.profit }}>
            {(accuracy.gate_value || 0) > 0 ? 'COST ' : 'SAVED '}${Math.abs(accuracy.gate_value || 0).toFixed(2)}
          </div>
          <div style={{ fontSize: 10, color: T.label }}>
            {(accuracy.gate_value || 0) > 0 ? 'Gate too tight — missed profits' : 'Gate saved you from losses'}
          </div>
        </div>

        {/* Win streak */}
        <div style={{
          background: T.card,
          border: `1px solid ${streakColor}30`,
          borderRadius: 10,
          padding: '12px 14px',
        }}>
          <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 6 }}>
            Current Streak
          </div>
          <div style={{ fontSize: 20, fontWeight: 700, color: streakColor }}>
            {accuracy.current_streak} {accuracy.current_streak === 1 ? 'WIN' : 'WINS'}
          </div>
          <div style={{ fontSize: 10, color: T.label }}>In a row</div>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// B. Outcome History Table
// ═══════════════════════════════════════════════════════════════════════════════

function CheckBadge({ value, na = false }) {
  if (na || value === null || value === undefined) {
    return <span style={{ color: T.label, fontSize: 11 }}>—</span>;
  }
  return (
    <span style={{
      fontSize: 13,
      color: value ? T.profit : T.loss,
    }}>
      {value ? '✅' : '❌'}
    </span>
  );
}

function PnlBadge({ pnl }) {
  if (pnl === null || pnl === undefined) {
    return <span style={{ color: T.label, fontSize: 10 }}>—</span>;
  }
  const color = pnl >= 0 ? T.profit : T.loss;
  return (
    <span style={{ color, fontWeight: 700, fontSize: 10, fontFamily: T.mono }}>
      {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}
    </span>
  );
}

function OutcomeHistoryTable({ outcomes, selectedTs, onSelectWindow }) {
  if (!outcomes?.length) {
    return (
      <div style={{ color: T.label, fontSize: 11, fontFamily: T.mono, padding: '16px 0' }}>
        No outcome data yet. Windows need close_price to calculate outcomes.
      </div>
    );
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{
        width: '100%',
        borderCollapse: 'collapse',
        fontFamily: T.mono,
        fontSize: 10,
        minWidth: 720,
      }}>
        <thead>
          <tr style={{ borderBottom: `1px solid ${T.border}` }}>
            {['Time', 'Open→Close', 'Actual', 'TimesFM', 'TWAP', 'Gamma', 'v5.7c', 'v5.8', 'v7.1', 'If Traded'].map(h => (
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
          {outcomes.map((o) => {
            const isSelected = selectedTs === o.window_ts;
            // Row background
            let rowBg = 'transparent';
            if (o.v58_would_trade) {
              rowBg = o.v58_correct ? 'rgba(74,222,128,0.05)' : 'rgba(248,113,113,0.05)';
            } else {
              rowBg = 'rgba(255,255,255,0.01)';
            }
            if (isSelected) rowBg = 'rgba(168,85,247,0.08)';

            const time = o.window_ts
              ? new Date(o.window_ts).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
              : '—';
            const openClose = (o.open_price && o.close_price)
              ? `$${Math.round(o.open_price).toLocaleString()} → $${Math.round(o.close_price).toLocaleString()}`
              : '—';

            const gammaStr = (o.gamma_up_price && o.gamma_down_price)
              ? `↑$${o.gamma_up_price.toFixed(2)} / ↓$${o.gamma_down_price.toFixed(2)}`
              : null;

            return (
              <tr
                key={o.window_ts}
                onClick={() => onSelectWindow(o.window_ts)}
                style={{
                  background: rowBg,
                  borderBottom: `1px solid rgba(255,255,255,0.025)`,
                  cursor: 'pointer',
                  transition: 'background 120ms',
                  outline: isSelected ? `1px solid ${T.purple}40` : 'none',
                }}
                onMouseEnter={e => {
                  if (!isSelected) e.currentTarget.style.background = 'rgba(255,255,255,0.04)';
                }}
                onMouseLeave={e => {
                  e.currentTarget.style.background = rowBg;
                }}
              >
                <td style={{ padding: '7px 10px', color: T.label, whiteSpace: 'nowrap' }}>{time}</td>
                <td style={{ padding: '7px 10px', color: T.label2, fontSize: 9, whiteSpace: 'nowrap' }}>{openClose}</td>
                <td style={{ padding: '7px 10px' }}>
                  {o.actual_direction ? (
                    <span style={{
                      color: o.actual_direction === 'UP' ? T.profit : T.loss,
                      fontWeight: 700,
                    }}>
                      {o.actual_direction === 'UP' ? '▲ UP' : '▼ DOWN'}
                    </span>
                  ) : <span style={{ color: T.label }}>—</span>}
                </td>
                <td style={{ padding: '7px 10px' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                    <CheckBadge value={o.timesfm_correct} />
                    {o.timesfm_direction && (
                      <span style={{ fontSize: 9, color: directionColor(o.timesfm_direction) }}>
                        {o.timesfm_direction}
                      </span>
                    )}
                  </div>
                </td>
                <td style={{ padding: '7px 10px' }}>
                  <CheckBadge value={o.twap_correct} />
                </td>
                <td style={{ padding: '7px 10px' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                    <CheckBadge value={o.gamma_correct} />
                    {gammaStr && (
                      <span style={{ fontSize: 8, color: T.warning, whiteSpace: 'nowrap' }}>{gammaStr}</span>
                    )}
                  </div>
                </td>
                <td style={{ padding: '7px 10px' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                    <CheckBadge value={o.v57c_correct} />
                    {o.direction && (
                      <span style={{ fontSize: 9, color: directionColor(o.direction) }}>
                        {o.direction}
                      </span>
                    )}
                  </div>
                </td>
                <td style={{ padding: '7px 10px' }}>
                  {o.v58_would_trade ? (
                    <span style={{
                      padding: '2px 7px',
                      borderRadius: 4,
                      background: o.v58_correct ? 'rgba(74,222,128,0.12)' : 'rgba(248,113,113,0.12)',
                      color: o.v58_correct ? T.profit : T.loss,
                      fontSize: 9,
                      fontWeight: 700,
                      letterSpacing: '0.04em',
                    }}>
                      {o.v58_correct ? '✓ WIN' : '✗ LOSS'}
                    </span>
                  ) : (
                    <span
                      title={o.v58_skip_reason || o.skip_reason || 'No trade signal'}
                      style={{
                        padding: '2px 7px',
                        borderRadius: 4,
                        background: o.tfm_v57c_agree === false ? 'rgba(248,113,113,0.08)' : 'rgba(255,255,255,0.04)',
                        color: o.tfm_v57c_agree === false ? T.loss : T.label,
                        fontSize: 9,
                        letterSpacing: '0.04em',
                        cursor: 'help',
                        borderBottom: '1px dotted rgba(255,255,255,0.15)',
                      }}>
                      {o.tfm_v57c_agree === false ? 'DISAGREE' : 'SKIP'}
                    </span>
                  )}
                </td>
                <td style={{ padding: '7px 10px' }}>
                  {o.v71_would_trade ? (
                    <span
                      title={`${o.v71_regime || 'NORMAL'} regime${o.v71_pnl != null ? ` | P&L: $${o.v71_pnl.toFixed(2)}` : ''}`}
                      style={{
                        padding: '2px 7px',
                        borderRadius: 4,
                        background: o.v71_correct ? 'rgba(168,85,247,0.15)' : o.v71_correct === false ? 'rgba(248,113,113,0.12)' : 'rgba(168,85,247,0.08)',
                        color: o.v71_correct ? '#a855f7' : o.v71_correct === false ? T.loss : '#c084fc',
                        fontSize: 9,
                        fontWeight: 700,
                        letterSpacing: '0.04em',
                        cursor: 'help',
                      }}>
                      {o.v71_correct === true ? '✓ WIN' : o.v71_correct === false ? '✗ LOSS' : 'TRADE'}
                    </span>
                  ) : (
                    <span
                      title={o.v71_skip_reason || 'v7.1 skip'}
                      style={{
                        padding: '2px 7px',
                        borderRadius: 4,
                        background: 'rgba(255,255,255,0.04)',
                        color: T.label,
                        fontSize: 9,
                        letterSpacing: '0.04em',
                        cursor: 'help',
                        borderBottom: '1px dotted rgba(168,85,247,0.2)',
                      }}>
                      SKIP
                    </span>
                  )}
                </td>
                <td style={{ padding: '7px 10px' }}>
                  <PnlBadge pnl={o.ungated_pnl != null ? o.ungated_pnl : o.v57c_pnl} />
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
// C. What-If Analysis Panel
// ═══════════════════════════════════════════════════════════════════════════════

function WhatIfAnalysis({ outcome }) {
  if (!outcome) {
    return (
      <div style={{ color: T.label, fontSize: 11, fontFamily: T.mono, textAlign: 'center', padding: 24 }}>
        Click a row in the outcome table to analyse
      </div>
    );
  }

  const gammaUp = outcome.gamma_up_price;
  const gammaDown = outcome.gamma_down_price;

  const scenarios = [
    {
      label: 'TimesFM',
      icon: '🔮',
      direction: outcome.timesfm_direction,
      entryPrice: outcome.timesfm_direction === 'UP' ? gammaUp : gammaDown,
      correct: outcome.timesfm_correct,
      pnl: outcome.timesfm_pnl,
      color: '#e879f9',
    },
    {
      label: 'v5.7c Signal',
      icon: '📍',
      direction: outcome.direction,
      entryPrice: outcome.direction === 'UP' ? gammaUp : gammaDown,
      correct: outcome.v57c_correct,
      pnl: outcome.v57c_pnl,
      color: T.purple,
    },
    {
      label: 'TWAP',
      icon: '📊',
      direction: outcome.twap_direction,
      entryPrice: outcome.twap_direction === 'UP' ? gammaUp : gammaDown,
      correct: outcome.twap_correct,
      pnl: outcome.twap_pnl,
      color: T.cyan,
    },
  ];

  const actualDelta = outcome.delta_pct != null ? (outcome.delta_pct * 100).toFixed(3) : null;

  return (
    <div style={{ fontFamily: T.mono }}>
      {/* Window price summary */}
      <div style={{
        padding: '10px 14px',
        borderRadius: 8,
        background: 'rgba(255,255,255,0.03)',
        border: `1px solid ${T.border}`,
        marginBottom: 14,
        display: 'flex',
        gap: 20,
        flexWrap: 'wrap',
        alignItems: 'center',
      }}>
        {outcome.open_price != null && (
          <div>
            <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>OPEN</div>
            <div style={{ fontSize: 13, color: '#fff', fontWeight: 600 }}>
              ${outcome.open_price.toLocaleString('en-US', { maximumFractionDigits: 0 })}
            </div>
          </div>
        )}
        <div style={{ color: T.label, fontSize: 14 }}>→</div>
        {outcome.close_price != null && (
          <div>
            <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>CLOSE</div>
            <div style={{ fontSize: 13, color: '#fff', fontWeight: 600 }}>
              ${outcome.close_price.toLocaleString('en-US', { maximumFractionDigits: 0 })}
            </div>
          </div>
        )}
        {actualDelta && (
          <div>
            <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>Δ%</div>
            <div style={{
              fontSize: 13, fontWeight: 700,
              color: outcome.delta_pct >= 0 ? T.profit : T.loss,
            }}>
              {outcome.delta_pct >= 0 ? '+' : ''}{actualDelta}%
            </div>
          </div>
        )}
        {outcome.actual_direction && (
          <div style={{ marginLeft: 'auto' }}>
            <span style={{
              padding: '4px 12px',
              borderRadius: 6,
              background: outcome.actual_direction === 'UP' ? 'rgba(74,222,128,0.12)' : 'rgba(248,113,113,0.12)',
              color: outcome.actual_direction === 'UP' ? T.profit : T.loss,
              fontSize: 11,
              fontWeight: 700,
            }}>
              {outcome.actual_direction === 'UP' ? '▲ WENT UP' : '▼ WENT DOWN'}
            </span>
            {outcome.resolution_source && (
              <span style={{
                marginLeft: 6,
                padding: '2px 6px',
                borderRadius: 4,
                background: outcome.resolution_source === 'polymarket' ? 'rgba(99,102,241,0.12)' : 'rgba(156,163,175,0.12)',
                color: outcome.resolution_source === 'polymarket' ? '#6366f1' : '#9ca3af',
                fontSize: 9,
                fontWeight: 600,
              }}>
                {outcome.resolution_source === 'polymarket' ? '⛓ POLYMARKET' : '📊 BINANCE T-60'}
              </span>
            )}
          </div>
        )}
      </div>

      {/* Scenario cards */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 14 }}>
        {scenarios.map((s) => {
          if (!s.direction) return null;
          const side = s.direction === 'UP' ? 'YES' : 'NO';
          const entryStr = s.entryPrice != null ? `@$${s.entryPrice.toFixed(3)}` : '';
          return (
            <div key={s.label} style={{
              padding: '10px 14px',
              borderRadius: 8,
              background: 'rgba(0,0,0,0.2)',
              border: `1px solid ${s.color}25`,
              display: 'flex',
              alignItems: 'center',
              gap: 12,
              flexWrap: 'wrap',
            }}>
              <span style={{ fontSize: 14 }}>{s.icon}</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <span style={{ fontSize: 11, color: s.color, fontWeight: 600 }}>{s.label}</span>
                <span style={{ fontSize: 10, color: T.label, marginLeft: 8 }}>
                  BUY {side} {entryStr} → $4 bet
                </span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                {s.correct !== null && s.correct !== undefined && (
                  <span style={{ fontSize: 12, color: s.correct ? T.profit : T.loss }}>
                    {s.correct ? '✅' : '❌'} {s.correct ? 'WON' : 'LOST'}
                  </span>
                )}
                {s.pnl !== null && s.pnl !== undefined && (
                  <span style={{
                    fontWeight: 700,
                    color: s.pnl >= 0 ? T.profit : T.loss,
                    fontSize: 13,
                  }}>
                    {s.pnl >= 0 ? '+' : ''}${s.pnl.toFixed(2)}
                  </span>
                )}
                {(s.correct === null || s.correct === undefined) && (
                  <span style={{ color: T.label, fontSize: 10 }}>No price data</span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* v5.8 decision */}
      <div style={{
        padding: '12px 14px',
        borderRadius: 10,
        background: outcome.v58_would_trade
          ? outcome.v58_correct ? 'rgba(74,222,128,0.07)' : 'rgba(248,113,113,0.07)'
          : 'rgba(255,255,255,0.03)',
        border: `1px solid ${outcome.v58_would_trade
          ? outcome.v58_correct ? 'rgba(74,222,128,0.25)' : 'rgba(248,113,113,0.25)'
          : T.border}`,
      }}>
        <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', marginBottom: 8, textTransform: 'uppercase' }}>
          v5.8 Decision
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span style={{
            fontSize: 12, fontWeight: 700,
            color: outcome.v58_would_trade ? T.cyan : T.label,
          }}>
            {outcome.v58_would_trade ? '✓ TRADED' : '✗ SKIPPED'}
          </span>
          {outcome.v58_would_trade && (
            <>
              <span style={{ color: T.label, fontSize: 10 }}>
                (TimesFM ↔ v5.7c agreed: {outcome.direction})
              </span>
              {outcome.v58_correct !== null && (
                <span style={{
                  padding: '3px 10px',
                  borderRadius: 5,
                  background: outcome.v58_correct ? 'rgba(74,222,128,0.15)' : 'rgba(248,113,113,0.15)',
                  color: outcome.v58_correct ? T.profit : T.loss,
                  fontSize: 11, fontWeight: 700,
                }}>
                  {outcome.v58_correct ? '▲ CORRECT' : '▼ WRONG'}
                </span>
              )}
              {outcome.v58_pnl !== null && outcome.v58_pnl !== undefined && (
                <span style={{
                  marginLeft: 'auto',
                  fontSize: 15, fontWeight: 700,
                  color: outcome.v58_pnl >= 0 ? T.profit : T.loss,
                }}>
                  {outcome.v58_pnl >= 0 ? '+' : ''}${outcome.v58_pnl.toFixed(2)}
                </span>
              )}
            </>
          )}
          {!outcome.v58_would_trade && (outcome.skip_reason || outcome.v58_skip_reason) && (
            <span style={{ 
              fontSize: 10, 
              color: (outcome.skip_reason || '').includes('CG VETO') ? '#f59e0b' : T.label,
              fontWeight: (outcome.skip_reason || '').includes('CG VETO') ? 600 : 400,
            }}>
              {(outcome.skip_reason || '').includes('CG VETO') ? '🛡️ ' : '⏭ '}
              {outcome.skip_reason || outcome.v58_skip_reason}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// D. Signal Source Cards (enhanced, outcome-aware)
// ═══════════════════════════════════════════════════════════════════════════════

function SignalSourceCards({ outcome }) {
  if (!outcome) {
    return (
      <div style={{ color: T.label, fontSize: 11, fontFamily: T.mono, textAlign: 'center', padding: 24 }}>
        Select a window to see signal detail
      </div>
    );
  }

  const gammaUp = outcome.gamma_up_price;
  const gammaDown = outcome.gamma_down_price;
  const cheaperSide = (gammaUp != null && gammaDown != null)
    ? (gammaUp < gammaDown ? 'UP' : 'DOWN')
    : null;
  const spread = (gammaUp != null && gammaDown != null)
    ? Math.abs(gammaUp - gammaDown).toFixed(3)
    : null;

  const vpinPct = outcome.vpin != null ? (outcome.vpin * 100).toFixed(1) : null;
  const vpinColor = outcome.vpin > 0.5 ? T.loss : outcome.vpin > 0.3 ? T.warning : T.profit;

  const cards = [
    {
      id: 'timesfm',
      icon: '🔮',
      label: 'TimesFM',
      color: '#e879f9',
      correct: outcome.timesfm_correct,
      content: (
        <>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ fontSize: 10, color: '#e879f9', fontWeight: 600 }}>Direction</span>
            {outcome.timesfm_direction ? (
              <span style={{ color: directionColor(outcome.timesfm_direction), fontWeight: 700, fontSize: 12 }}>
                {outcome.timesfm_direction === 'UP' ? '▲ UP' : '▼ DOWN'}
              </span>
            ) : <span style={{ color: T.label }}>—</span>}
          </div>
          {outcome.timesfm_confidence != null && (
            <div style={{ marginBottom: 8 }}>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 4 }}>Confidence</div>
              {confidenceBar(outcome.timesfm_confidence, '#e879f9')}
            </div>
          )}
          {outcome.timesfm_predicted_close != null && (
            <div style={{ fontSize: 10, color: T.label }}>
              Pred close: <span style={{ color: '#e879f9' }}>
                ${outcome.timesfm_predicted_close.toLocaleString('en-US', { maximumFractionDigits: 0 })}
              </span>
              {outcome.close_price != null && (
                <span style={{ color: outcome.timesfm_correct ? T.profit : T.loss, marginLeft: 6 }}>
                  (δ {outcome.close_price > outcome.timesfm_predicted_close ? '+' : ''}
                  ${(outcome.close_price - outcome.timesfm_predicted_close).toFixed(0)})
                </span>
              )}
            </div>
          )}
          {outcome.timesfm_agreement != null && (
            <div style={{
              marginTop: 8,
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              padding: '2px 8px',
              borderRadius: 4,
              background: outcome.timesfm_agreement ? 'rgba(74,222,128,0.1)' : 'rgba(248,113,113,0.1)',
              border: `1px solid ${outcome.timesfm_agreement ? 'rgba(74,222,128,0.3)' : 'rgba(248,113,113,0.3)'}`,
              fontSize: 9, fontWeight: 700,
              color: outcome.timesfm_agreement ? T.profit : T.loss,
            }}>
              {outcome.timesfm_agreement ? '✓ AGREE w/ v5.7c' : '✗ DISAGREE'}
            </div>
          )}
        </>
      ),
    },
    {
      id: 'twap',
      icon: '📊',
      label: 'TWAP',
      color: T.cyan,
      correct: outcome.twap_correct,
      content: (
        <>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ fontSize: 10, color: T.cyan, fontWeight: 600 }}>Direction</span>
            {outcome.twap_direction ? (
              <span style={{ color: directionColor(outcome.twap_direction), fontWeight: 700, fontSize: 12 }}>
                {outcome.twap_direction === 'UP' ? '▲ UP' : '▼ DOWN'}
              </span>
            ) : <span style={{ color: T.label }}>—</span>}
          </div>
          {outcome.twap_agreement_score != null && (
            <div style={{ marginBottom: 8 }}>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 4 }}>Agreement Score</div>
              {confidenceBar(outcome.twap_agreement_score, T.cyan)}
            </div>
          )}
          <div style={{ fontSize: 10, color: T.label }}>
            Gamma Gate: {' '}
            <span style={{ color: outcome.twap_gamma_gate ? T.profit : T.loss, fontWeight: 600 }}>
              {outcome.twap_gamma_gate === true ? '✅ PASSED' : outcome.twap_gamma_gate === false ? '❌ FAILED' : '—'}
            </span>
          </div>
        </>
      ),
    },
    {
      id: 'gamma',
      icon: '⚡',
      label: 'Gamma',
      color: T.warning,
      correct: outcome.gamma_correct,
      content: (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 8 }}>
            <div style={{
              padding: '6px 8px',
              borderRadius: 6,
              background: cheaperSide === 'UP' ? 'rgba(74,222,128,0.1)' : 'rgba(255,255,255,0.04)',
              border: `1px solid ${cheaperSide === 'UP' ? 'rgba(74,222,128,0.25)' : T.border}`,
              textAlign: 'center',
            }}>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>↑ UP</div>
              <div style={{ fontSize: 12, color: T.profit, fontWeight: 700 }}>
                {gammaUp != null ? `$${gammaUp.toFixed(3)}` : '—'}
              </div>
              {cheaperSide === 'UP' && (
                <div style={{ fontSize: 8, color: T.profit, marginTop: 2 }}>CHEAPER</div>
              )}
            </div>
            <div style={{
              padding: '6px 8px',
              borderRadius: 6,
              background: cheaperSide === 'DOWN' ? 'rgba(248,113,113,0.1)' : 'rgba(255,255,255,0.04)',
              border: `1px solid ${cheaperSide === 'DOWN' ? 'rgba(248,113,113,0.25)' : T.border}`,
              textAlign: 'center',
            }}>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>↓ DOWN</div>
              <div style={{ fontSize: 12, color: T.loss, fontWeight: 700 }}>
                {gammaDown != null ? `$${gammaDown.toFixed(3)}` : '—'}
              </div>
              {cheaperSide === 'DOWN' && (
                <div style={{ fontSize: 8, color: T.loss, marginTop: 2 }}>CHEAPER</div>
              )}
            </div>
          </div>
          {spread && (
            <div style={{ fontSize: 10, color: T.label }}>
              Spread: <span style={{ color: T.warning }}>${spread}</span>
              {outcome.gamma_implied_direction && (
                <span style={{ marginLeft: 8, color: T.warning, fontWeight: 600 }}>
                  → mkt favours {outcome.gamma_implied_direction}
                </span>
              )}
            </div>
          )}
        </>
      ),
    },
    {
      id: 'vpin',
      icon: '🌊',
      label: 'VPIN',
      color: vpinColor,
      correct: null, // VPIN is a gate, not directional
      content: (
        <>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 8, marginBottom: 8 }}>
            <div style={{ fontSize: 24, fontWeight: 700, color: vpinColor, lineHeight: 1 }}>
              {vpinPct != null ? vpinPct + '%' : '—'}
            </div>
            {outcome.regime && (
              <div style={{ fontSize: 10, color: T.warning, marginBottom: 3, fontWeight: 600 }}>
                {outcome.regime}
              </div>
            )}
          </div>
          {outcome.vpin != null && (
            <div style={{ marginBottom: 8 }}>
              {confidenceBar(outcome.vpin, vpinColor)}
            </div>
          )}
          <div style={{ fontSize: 10, color: T.label }}>
            Gate (0.5): {' '}
            <span style={{ color: outcome.vpin > 0.5 ? T.loss : T.profit, fontWeight: 600 }}>
              {outcome.vpin != null
                ? outcome.vpin > 0.5 ? '❌ ABOVE (skip)' : '✅ OK'
                : '—'}
            </span>
          </div>
        </>
      ),
    },
  ];

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(2, 1fr)',
      gap: 10,
    }}>
      {cards.map((card) => (
        <div
          key={card.id}
          style={{
            padding: '12px 14px',
            borderRadius: 10,
            background: 'rgba(0,0,0,0.25)',
            border: `1px solid ${card.color}30`,
            position: 'relative',
            overflow: 'hidden',
          }}
        >
          {/* Top accent */}
          <div style={{
            position: 'absolute', top: 0, left: 0, right: 0, height: 2,
            background: card.color, opacity: 0.6,
          }} />

          {/* Header */}
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ fontSize: 14 }}>{card.icon}</span>
              <span style={{ fontSize: 10, color: card.color, fontWeight: 600, letterSpacing: '0.06em', fontFamily: T.mono }}>
                {card.label}
              </span>
            </div>
            {card.correct !== null && card.correct !== undefined && (
              <span style={{ fontSize: 12 }}>
                {card.correct ? '✅' : '❌'}
              </span>
            )}
          </div>

          {/* Content */}
          <div style={{ fontFamily: T.mono }}>
            {card.content}
          </div>
        </div>
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// E. Trade Buttons (Paper + Live)
// ═══════════════════════════════════════════════════════════════════════════════

function TradeButtons({ latestWindow, onTradeSuccess }) {
  const api = useApi();
  const [placing, setPlacing] = useState(null);
  const [lastResult, setLastResult] = useState(null);
  const [error, setError] = useState(null);
  const [showLiveConfirm, setShowLiveConfirm] = useState(false);
  const [livePrices, setLivePrices] = useState(null);
  const [priceAge, setPriceAge] = useState(0);

  // Poll live Gamma prices every 2 seconds
  useEffect(() => {
    let interval;
    const fetchPrices = async () => {
      try {
        const ts = latestWindow?.window_ts ? new Date(latestWindow.window_ts).getTime() : null;
        const r = await api('GET', `/v58/live-prices${ts ? `?window_ts=${ts}` : ''}`);
        if (r?.data) {
          setLivePrices(r.data);
          setPriceAge(0);
        }
      } catch {}
    };
    fetchPrices();
    interval = setInterval(fetchPrices, 2000);
    const ageInterval = setInterval(() => setPriceAge(a => a + 1), 1000);
    return () => { clearInterval(interval); clearInterval(ageInterval); };
  }, [latestWindow?.window_ts]);

  // Use live prices if available, fall back to snapshot
  const gammaUp = livePrices?.up_price ?? latestWindow?.gamma_up_price;
  const gammaDown = livePrices?.down_price ?? latestWindow?.gamma_down_price;
  const currentDirection = latestWindow?.direction;
  const entryPrice = currentDirection === 'UP' ? gammaUp : gammaDown;
  const gammaStr = entryPrice != null ? `@$${entryPrice.toFixed(3)}` : '';
  const upBet = livePrices?.up_bet;
  const downBet = livePrices?.down_bet;
  const activeBet = currentDirection === 'UP' ? upBet : downBet;

  // Get window_ts as unix ms
  const getWindowTs = () => {
    if (!latestWindow?.window_ts) return null;
    const d = new Date(latestWindow.window_ts);
    return d.getTime(); // ms
  };

  const placeTrade = async (mode) => {
    setPlacing(mode);
    setError(null);
    setLastResult(null);
    try {
      const r = await api('POST', '/v58/manual-trade', {
        asset: latestWindow?.asset || 'BTC',
        direction: currentDirection || 'UP',
        mode,
        window_ts: getWindowTs(),
      });
      setLastResult(r?.data ?? null);
      onTradeSuccess?.();
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || 'Trade failed');
    } finally {
      setPlacing(null);
      setShowLiveConfirm(false);
    }
  };

  const disabled = !latestWindow || !currentDirection;

  return (
    <div style={{ fontFamily: T.mono }}>
      {/* Real-time trade preview */}
      <div style={{
        padding: '12px 14px',
        borderRadius: 8,
        background: 'rgba(0,0,0,0.3)',
        border: `1px solid ${livePrices ? 'rgba(74,222,128,0.3)' : T.border}`,
        marginBottom: 14,
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
          <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase' }}>
            ⚡ LIVE TRADE PREVIEW
          </div>
          <div style={{
            fontSize: 9, padding: '2px 6px', borderRadius: 4,
            background: priceAge <= 3 ? 'rgba(74,222,128,0.2)' : 'rgba(251,191,36,0.2)',
            color: priceAge <= 3 ? T.profit : T.warning,
          }}>
            {priceAge <= 3 ? '🟢 LIVE' : `🟡 ${priceAge}s ago`}
          </div>
        </div>

        {/* Direction + Entry */}
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 10 }}>
          <div style={{ minWidth: 70 }}>
            <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>Direction</div>
            <div style={{
              fontSize: 18, fontWeight: 700,
              color: currentDirection ? directionColor(currentDirection) : T.label,
            }}>
              {currentDirection ? (currentDirection === 'UP' ? '▲ UP' : '▼ DOWN') : '—'}
            </div>
          </div>
          <div style={{ minWidth: 70 }}>
            <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>Entry Price</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: '#fff' }}>
              {entryPrice != null ? `$${entryPrice.toFixed(3)}` : '—'}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>Stake</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: T.warning }}>$4.00</div>
          </div>
          {activeBet && (
            <div>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>Shares</div>
              <div style={{ fontSize: 18, fontWeight: 700, color: '#fff' }}>{activeBet.shares}</div>
            </div>
          )}
        </div>

        {/* P&L Preview */}
        {activeBet && (
          <div style={{
            display: 'flex', gap: 12, padding: '8px 10px', borderRadius: 6,
            background: 'rgba(0,0,0,0.3)', border: `1px solid ${T.border}`,
          }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>IF WIN ✅</div>
              <div style={{ fontSize: 16, fontWeight: 700, color: T.profit }}>
                +${activeBet.win_pnl.toFixed(2)}
              </div>
              <div style={{ fontSize: 9, color: T.label }}>
                +{((activeBet.win_pnl / 4) * 100).toFixed(0)}% return
              </div>
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>IF LOSS ❌</div>
              <div style={{ fontSize: 16, fontWeight: 700, color: T.loss }}>
                ${activeBet.loss_pnl.toFixed(2)}
              </div>
              <div style={{ fontSize: 9, color: T.label }}>
                -{activeBet.breakeven_pct}% to break even
              </div>
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>R:R</div>
              <div style={{ fontSize: 16, fontWeight: 700, color: '#fff' }}>
                {Math.abs(activeBet.loss_pnl) > 0
                  ? `1:${(activeBet.win_pnl / Math.abs(activeBet.loss_pnl)).toFixed(1)}`
                  : '—'}
              </div>
            </div>
          </div>
        )}

        {/* Gamma prices */}
        {gammaUp != null && gammaDown != null && (
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <div style={{
              flex: 1, padding: '4px 8px', borderRadius: 4, fontSize: 10, textAlign: 'center',
              background: currentDirection === 'UP' ? 'rgba(74,222,128,0.12)' : 'rgba(0,0,0,0.2)',
              border: currentDirection === 'UP' ? '1px solid rgba(74,222,128,0.3)' : `1px solid ${T.border}`,
              color: currentDirection === 'UP' ? T.profit : T.label,
            }}>
              ▲ UP ${gammaUp.toFixed(3)}
            </div>
            <div style={{
              flex: 1, padding: '4px 8px', borderRadius: 4, fontSize: 10, textAlign: 'center',
              background: currentDirection === 'DOWN' ? 'rgba(248,113,113,0.12)' : 'rgba(0,0,0,0.2)',
              border: currentDirection === 'DOWN' ? '1px solid rgba(248,113,113,0.3)' : `1px solid ${T.border}`,
              color: currentDirection === 'DOWN' ? T.loss : T.label,
            }}>
              ▼ DOWN ${gammaDown.toFixed(3)}
            </div>
            {livePrices?.spread != null && (
              <div style={{
                padding: '4px 8px', borderRadius: 4, fontSize: 10,
                background: 'rgba(0,0,0,0.2)', border: `1px solid ${T.border}`,
                color: T.label, textAlign: 'center',
              }}>
                Spread: {(livePrices.spread * 100).toFixed(1)}%
              </div>
            )}
          </div>
        )}
      </div>

      {/* Buttons */}
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        <button
          disabled={disabled || placing !== null}
          onClick={() => placeTrade('paper')}
          style={{
            flex: 1,
            padding: '12px 16px',
            borderRadius: 8,
            border: '1px solid rgba(74,222,128,0.4)',
            background: disabled ? 'rgba(255,255,255,0.04)' : 'rgba(74,222,128,0.12)',
            color: disabled ? T.label : T.profit,
            fontSize: 12,
            fontWeight: 700,
            cursor: disabled ? 'not-allowed' : 'pointer',
            fontFamily: T.mono,
            transition: 'all 200ms ease-out',
            opacity: disabled ? 0.5 : 1,
          }}
        >
          {placing === 'paper' ? '⏳ Placing…' : `📄 Paper Trade ${gammaStr}`}
        </button>

        {!showLiveConfirm ? (
          <button
            disabled={disabled || placing !== null}
            onClick={() => setShowLiveConfirm(true)}
            style={{
              flex: 1,
              padding: '12px 16px',
              borderRadius: 8,
              border: '1px solid rgba(248,113,113,0.4)',
              background: disabled ? 'rgba(255,255,255,0.04)' : 'rgba(248,113,113,0.1)',
              color: disabled ? T.label : T.loss,
              fontSize: 12,
              fontWeight: 700,
              cursor: disabled ? 'not-allowed' : 'pointer',
              fontFamily: T.mono,
              transition: 'all 200ms ease-out',
              opacity: disabled ? 0.5 : 1,
            }}
          >
            🔴 Live Trade {gammaStr}
          </button>
        ) : (
          <div style={{
            flex: 1,
            display: 'flex',
            gap: 8,
            padding: '8px 12px',
            borderRadius: 8,
            background: 'rgba(248,113,113,0.08)',
            border: '1px solid rgba(248,113,113,0.3)',
            alignItems: 'center',
          }}>
            <span style={{ fontSize: 10, color: T.loss, flex: 1 }}>
              Confirm live trade: {currentDirection} {gammaStr} · $4 stake
            </span>
            <button
              onClick={() => placeTrade('live')}
              disabled={placing !== null}
              style={{
                padding: '6px 12px',
                borderRadius: 6,
                border: '1px solid rgba(248,113,113,0.5)',
                background: 'rgba(248,113,113,0.2)',
                color: T.loss,
                fontSize: 11,
                fontWeight: 700,
                cursor: 'pointer',
                fontFamily: T.mono,
              }}
            >
              {placing === 'live' ? '⏳' : '✓ Confirm'}
            </button>
            <button
              onClick={() => setShowLiveConfirm(false)}
              style={{
                padding: '6px 10px',
                borderRadius: 6,
                border: `1px solid ${T.border}`,
                background: 'transparent',
                color: T.label,
                fontSize: 11,
                cursor: 'pointer',
                fontFamily: T.mono,
              }}
            >
              ✕
            </button>
          </div>
        )}
      </div>

      {/* Result */}
      {lastResult && (
        <div style={{
          marginTop: 12,
          padding: '10px 14px',
          borderRadius: 8,
          background: 'rgba(74,222,128,0.07)',
          border: '1px solid rgba(74,222,128,0.25)',
        }}>
          <div style={{ fontSize: 10, color: T.profit, fontWeight: 700, marginBottom: 4 }}>
            ✅ Trade Placed!
          </div>
          <div style={{ fontSize: 10, color: T.label }}>
            ID: <span style={{ color: '#fff' }}>{lastResult.trade_id}</span>
            {' · '}
            Mode: <span style={{ color: lastResult.mode === 'paper' ? T.cyan : T.loss }}>
              {lastResult.mode.toUpperCase()}
            </span>
            {' · '}
            Entry: <span style={{ color: T.warning }}>${lastResult.entry_price?.toFixed(3)}</span>
            {' · '}
            Status: <span style={{ color: T.profit }}>{lastResult.status}</span>
          </div>
        </div>
      )}

      {/* Error */}
      {error && (
        <div style={{
          marginTop: 12,
          padding: '10px 14px',
          borderRadius: 8,
          background: 'rgba(248,113,113,0.07)',
          border: '1px solid rgba(248,113,113,0.25)',
          fontSize: 10,
          color: T.loss,
        }}>
          ❌ {error}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// F. My Trades Panel
// ═══════════════════════════════════════════════════════════════════════════════

function MyTradesPanel({ trades, totalPnl, loading }) {
  if (loading) {
    return (
      <div style={{ color: T.label, fontSize: 11, fontFamily: T.mono, padding: 16 }}>
        Loading trades…
      </div>
    );
  }

  if (!trades?.length) {
    return (
      <div style={{ color: T.label, fontSize: 11, fontFamily: T.mono, padding: 16 }}>
        No manual trades yet. Use the buttons above to place your first trade.
      </div>
    );
  }

  const pnlColor = totalPnl >= 0 ? T.profit : T.loss;

  return (
    <div style={{ fontFamily: T.mono }}>
      {/* Running total */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: 14,
        padding: '10px 14px',
        borderRadius: 8,
        background: `${pnlColor}08`,
        border: `1px solid ${pnlColor}25`,
      }}>
        <span style={{ fontSize: 10, color: T.label }}>Running Total P&L</span>
        <span style={{ fontSize: 18, fontWeight: 700, color: pnlColor }}>
          {totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}
        </span>
      </div>

      {/* Table */}
      <div style={{ overflowX: 'auto' }}>
        <table style={{
          width: '100%',
          borderCollapse: 'collapse',
          fontSize: 10,
          minWidth: 560,
        }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${T.border}` }}>
              {['Time', 'Direction', 'Entry', 'Mode', 'Status', 'P&L'].map(h => (
                <th key={h} style={{
                  padding: '6px 10px',
                  textAlign: 'left',
                  color: T.label,
                  fontWeight: 600,
                  fontSize: 9,
                  letterSpacing: '0.08em',
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {trades.map(t => {
              const time = t.created_at
                ? new Date(t.created_at).toLocaleString('en-GB', {
                    month: 'short', day: 'numeric',
                    hour: '2-digit', minute: '2-digit',
                  })
                : '—';

              const modeColor = t.mode === 'paper' ? T.cyan : T.loss;
              const statusColor = {
                won: T.profit,
                lost: T.loss,
                open: T.warning,
                pending_live: '#e879f9',
                expired: T.label,
              }[t.status] ?? T.label;

              let rowBg = 'transparent';
              if (t.status === 'won') rowBg = 'rgba(74,222,128,0.04)';
              else if (t.status === 'lost') rowBg = 'rgba(248,113,113,0.04)';

              return (
                <tr
                  key={t.trade_id}
                  style={{
                    background: rowBg,
                    borderBottom: `1px solid rgba(255,255,255,0.025)`,
                  }}
                >
                  <td style={{ padding: '7px 10px', color: T.label }}>{time}</td>
                  <td style={{ padding: '7px 10px' }}>
                    <span style={{
                      color: directionColor(t.direction),
                      fontWeight: 700,
                    }}>
                      {t.direction === 'UP' ? '▲ UP' : '▼ DOWN'}
                    </span>
                  </td>
                  <td style={{ padding: '7px 10px', color: '#fff' }}>
                    ${t.entry_price?.toFixed(3) ?? '—'}
                  </td>
                  <td style={{ padding: '7px 10px' }}>
                    <span style={{
                      padding: '2px 8px',
                      borderRadius: 4,
                      background: `${modeColor}18`,
                      border: `1px solid ${modeColor}30`,
                      color: modeColor,
                      fontSize: 9,
                      fontWeight: 700,
                    }}>
                      {t.mode.toUpperCase()}
                    </span>
                  </td>
                  <td style={{ padding: '7px 10px' }}>
                    <span style={{
                      padding: '2px 8px',
                      borderRadius: 4,
                      background: `${statusColor}10`,
                      color: statusColor,
                      fontSize: 9,
                      fontWeight: 700,
                      letterSpacing: '0.04em',
                    }}>
                      {t.status.toUpperCase()}
                    </span>
                  </td>
                  <td style={{ padding: '7px 10px' }}>
                    {t.pnl_usd != null ? (
                      <span style={{
                        fontWeight: 700,
                        color: t.pnl_usd >= 0 ? T.profit : T.loss,
                      }}>
                        {t.pnl_usd >= 0 ? '+' : ''}${t.pnl_usd.toFixed(2)}
                      </span>
                    ) : (
                      <span style={{ color: T.label }}>pending</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
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
  const [outcomes, setOutcomes] = useState([]);
  const [accuracy, setAccuracy] = useState(null);
  const [gateAnalysis, setGateAnalysis] = useState(null);
  const [selectedOutcomeTs, setSelectedOutcomeTs] = useState(null);
  const [manualTrades, setManualTrades] = useState([]);
  const [manualTotalPnl, setManualTotalPnl] = useState(0);
  const [manualLoading, setManualLoading] = useState(false);

  // Derived: the selected window object
  const selectedWindow = useMemo(
    () => windows.find(w => w.window_ts === selectedTs) ?? windows[0] ?? null,
    [windows, selectedTs]
  );

  // Latest (most recent) window = first in the array (API returns newest-first)
  const latestWindow = windows[0] ?? null;

  // ── Fetch manual trades ─────────────────────────────────────────────────────
  const fetchManualTrades = useCallback(async () => {
    setManualLoading(true);
    try {
      const r = await api('GET', '/v58/manual-trades');
      setManualTrades(r?.data?.trades ?? []);
      setManualTotalPnl(r?.data?.total_pnl ?? 0);
    } catch (_) {}
    finally { setManualLoading(false); }
  }, [api]);

  // ── Fetch ───────────────────────────────────────────────────────────────────
  const fetchAll = useCallback(async () => {
    try {
      const [windowsRes, statsRes, priceRes, outcomesRes, accuracyRes, gateRes] = await Promise.allSettled([
        api('GET', '/v58/windows?limit=50'),
        api('GET', '/v58/stats?days=7'),
        api('GET', '/v58/price-history?minutes=60'),
        api('GET', '/v58/outcomes?limit=100'),
        api('GET', '/v58/accuracy?limit=100'),
        api('GET', '/v58/gate-analysis'),
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

      if (outcomesRes.status === 'fulfilled') {
        const data = outcomesRes.value?.data?.outcomes ?? [];
        setOutcomes(data);
      }

      if (accuracyRes.status === 'fulfilled') {
        setAccuracy(accuracyRes.value?.data ?? null);
      }

      if (gateRes.status === 'fulfilled') {
        setGateAnalysis(gateRes.value?.data ?? null);
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
    fetchManualTrades();
    const id = setInterval(fetchAll, 15000); // refresh every 15s
    const id2 = setInterval(fetchManualTrades, 30000); // refresh trades every 30s
    return () => { clearInterval(id); clearInterval(id2); };
  }, [fetchAll, fetchManualTrades]);

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
        @keyframes v58streakpulse { 0%,100%{box-shadow:0 0 12px rgba(74,222,128,0.4)} 50%{box-shadow:0 0 28px rgba(74,222,128,0.9)} }
        @media (max-width: 768px) {
          .v58-grid-2 { grid-template-columns: 1fr !important; }
          .v58-grid-3 { grid-template-columns: 1fr !important; }
          .v58-signal-grid { grid-template-columns: 1fr !important; }
        }
        @media (prefers-reduced-motion: reduce) {
          .v58-streak-pulse { animation: none !important; }
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
          ◈ v7 MONITOR
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

      {/* ── Win Streak Banner ──────────────────────────────────────────── */}
      {accuracy && (
        <div style={{
          padding: '12px 20px',
          borderBottom: `1px solid ${T.border}`,
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          background: accuracy.current_streak > 5
            ? 'rgba(74,222,128,0.06)'
            : 'transparent',
          transition: 'background 0.3s ease',
        }}>
          <div
            className={accuracy.current_streak > 5 ? 'v58-streak-pulse' : ''}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              background: accuracy.current_streak > 0
                ? 'rgba(74,222,128,0.1)'
                : 'rgba(255,255,255,0.04)',
              border: `1px solid ${accuracy.current_streak > 0 ? 'rgba(74,222,128,0.3)' : T.border}`,
              borderRadius: 10,
              padding: '8px 20px',
              animation: accuracy.current_streak > 5 ? 'v58streakpulse 2s ease-in-out infinite' : 'none',
            }}
          >
            <span style={{ fontSize: 22 }}>
              {accuracy.current_streak > 5 ? '🔥' : accuracy.current_streak > 0 ? '✅' : '—'}
            </span>
            <div>
              <div style={{
                fontSize: 28,
                fontWeight: 700,
                color: accuracy.current_streak > 0 ? T.profit : T.label,
                lineHeight: 1,
                letterSpacing: '-0.02em',
              }}>
                {accuracy.current_streak}
              </div>
              <div style={{ fontSize: 9, color: T.label, textTransform: 'uppercase', letterSpacing: '0.1em', marginTop: 2 }}>
                {accuracy.current_streak === 1 ? 'WIN STREAK' : 'WIN STREAK'}
              </div>
            </div>
          </div>
          {accuracy.current_streak > 5 && (
            <span style={{ fontSize: 11, color: T.profit, letterSpacing: '0.06em', fontWeight: 600 }}>
              🔥 ON FIRE — {accuracy.current_streak} consecutive wins
            </span>
          )}
          {accuracy.current_streak === 0 && (
            <span style={{ fontSize: 11, color: T.label, letterSpacing: '0.06em' }}>
              No active win streak
            </span>
          )}
        </div>
      )}

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
            {/* Legend */}
            <div style={{
              display: 'flex', gap: 16, flexWrap: 'wrap',
              marginTop: 10, padding: '8px 12px',
              background: 'rgba(0,0,0,0.2)', borderRadius: 6,
              fontSize: 9, fontFamily: T.mono, color: T.label2,
            }}>
              <span><strong style={{ color: '#fff' }}>SIG</strong> ▲▼ = Our signal direction at T-60</span>
              <span><strong style={{ color: T.profit }}>WIN ✓</strong> / <strong style={{ color: T.loss }}>LOSS ✗</strong> = Polymarket resolved outcome</span>
              <span><strong style={{ color: T.label2 }}>OUT ▼</strong> = Awaiting oracle (~4min after close)</span>
              <span style={{ color: '#a855f7' }}><strong>7.1 ✅</strong> = v7.1 would trade + won</span>
              <span style={{ color: '#ef4444' }}><strong>7.1 ❌</strong> = v7.1 would trade + lost</span>
              <span style={{ color: '#a855f7' }}><strong>7.1 📊</strong> = v7.1 would trade, pending</span>
              <span><strong>7.1 ⏭</strong> = v7.1 would skip</span>
              <span style={{ color: '#22c55e' }}><strong>💰 TRADED</strong> = Actual trade placed</span>
            </div>
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
              {/* Quick stats row: VPIN / Regime / Direction / Gamma */}
              {latestWindow && (
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 14 }}>
                  {[
                    { label: 'VPIN', value: latestWindow.vpin != null ? latestWindow.vpin.toFixed(3) : '—', color: latestWindow.vpin >= 0.65 ? T.profit : latestWindow.vpin >= 0.45 ? T.warning : T.label },
                    { label: 'REGIME', value: latestWindow.regime ?? '—', color: latestWindow.regime === 'CASCADE' ? T.profit : latestWindow.regime === 'TRANSITION' ? T.warning : T.label2 },
                    { label: 'DIR', value: latestWindow.direction ?? '—', color: latestWindow.direction === 'UP' ? T.profit : latestWindow.direction === 'DOWN' ? T.loss : T.label },
                    { label: '↑ GAMMA', value: latestWindow.gamma_up_price != null ? latestWindow.gamma_up_price.toFixed(3) : '—', color: T.cyan },
                    { label: '↓ GAMMA', value: latestWindow.gamma_down_price != null ? latestWindow.gamma_down_price.toFixed(3) : '—', color: T.purple },
                  ].map(({ label, value, color }) => (
                    <div key={label} style={{
                      background: 'rgba(255,255,255,0.04)',
                      border: `1px solid ${T.border}`,
                      borderRadius: 6,
                      padding: '4px 10px',
                      display: 'flex',
                      flexDirection: 'column',
                      alignItems: 'center',
                      gap: 2,
                    }}>
                      <span style={{ fontSize: 8, color: T.label, textTransform: 'uppercase', letterSpacing: '0.08em' }}>{label}</span>
                      <span style={{ fontSize: 13, fontWeight: 700, color }}>{value}</span>
                    </div>
                  ))}
                </div>
              )}
              <CountdownTimer
                windowTs={latestWindow?.window_ts}
              />
            </div>

            {/* v7.1 Live Decision Panel */}
            <div style={{
              background: 'rgba(168,85,247,0.08)',
              border: `1px solid rgba(168,85,247,0.3)`,
              borderRadius: 12,
              padding: '20px',
              display: 'flex',
              flexDirection: 'column',
            }}>
              <div style={{ fontSize: 9, color: '#a855f7', letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 14, fontWeight: 700 }}>
                § v7.1 LIVE DECISION
              </div>
              {latestWindow ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {/* Main decision */}
                  <div style={{
                    padding: '12px 16px',
                    borderRadius: 8,
                    background: latestWindow.v71_would_trade ? 'rgba(74,222,128,0.12)' : 'rgba(248,113,113,0.12)',
                    border: `1px solid ${latestWindow.v71_would_trade ? 'rgba(74,222,128,0.3)' : 'rgba(248,113,113,0.3)'}`,
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'space-between' }}>
                      <span style={{ fontSize: 10, color: T.label, textTransform: 'uppercase', letterSpacing: '0.08em' }}>Decision</span>
                      <span style={{
                        fontSize: 16,
                        fontWeight: 700,
                        color: latestWindow.v71_would_trade ? T.profit : T.loss,
                        fontFamily: T.mono,
                      }}>
                        {latestWindow.v71_would_trade ? '✅ TRADE' : '🚫 SKIP'}
                      </span>
                    </div>
                    {latestWindow.v71_regime && (
                      <div style={{ fontSize: 9, color: T.label, marginTop: 8, fontFamily: T.mono }}>
                        Regime: <span style={{
                          fontWeight: 700,
                          color: latestWindow.v71_regime === 'CASCADE' ? T.profit : latestWindow.v71_regime === 'TRANSITION' ? T.warning : T.label2,
                        }}>{latestWindow.v71_regime}</span>
                      </div>
                    )}
                  </div>

                  {/* Skip reason if blocked */}
                  {latestWindow.v71_skip_reason && (
                    <div style={{
                      padding: '8px 12px',
                      borderRadius: 6,
                      background: 'rgba(248,113,113,0.08)',
                      border: `1px solid rgba(248,113,113,0.2)`,
                      fontSize: 9,
                      color: T.label,
                      fontFamily: T.mono,
                      lineHeight: 1.4,
                    }}>
                      ⚠️ {latestWindow.v71_skip_reason}
                    </div>
                  )}

                  {/* v7.1 Criteria Breakdown */}
                  <div style={{
                    padding: '8px 12px',
                    borderRadius: 6,
                    background: 'rgba(168,85,247,0.04)',
                    border: '1px solid rgba(168,85,247,0.15)',
                    fontSize: 9,
                    fontFamily: T.mono,
                    lineHeight: 1.6,
                    color: T.label2,
                  }}>
                    <div style={{ fontWeight: 600, color: '#a855f7', marginBottom: 4 }}>v7.1 Criteria</div>
                    <div>VPIN: <span style={{ color: (latestWindow.vpin || 0) >= 0.45 ? '#22c55e' : '#ef4444', fontWeight: 700 }}>
                      {(latestWindow.vpin || 0).toFixed(3)}
                    </span> {(latestWindow.vpin || 0) >= 0.45 ? '✓' : '✗'} gate ≥0.45</div>
                    <div>Delta: <span style={{ color: Math.abs(latestWindow.delta_pct || 0) >= 0.02 ? '#22c55e' : '#ef4444', fontWeight: 700 }}>
                      {Math.abs(latestWindow.delta_pct || 0).toFixed(4)}%
                    </span> {Math.abs(latestWindow.delta_pct || 0) >= 0.02 ? '✓' : '✗'} min ≥0.02%</div>
                    <div>Entry Cap: <span style={{ fontWeight: 700, color: '#c084fc' }}>$0.70</span></div>
                  </div>

                  {/* Comparison: legacy vs v7.1 */}
                  <div style={{
                    display: 'grid',
                    gridTemplateColumns: '1fr 1fr',
                    gap: 8,
                    fontSize: 9,
                  }}>
                    <div style={{
                      padding: '8px',
                      borderRadius: 6,
                      background: 'rgba(255,255,255,0.04)',
                      border: `1px solid ${T.border}`,
                      textAlign: 'center',
                    }}>
                      <div style={{ color: T.label, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>Legacy</div>
                      <div style={{
                        fontWeight: 700,
                        color: latestWindow.trade_placed ? T.cyan : T.label,
                        fontSize: 11,
                      }}>
                        {latestWindow.trade_placed ? 'TRADE' : 'SKIP'}
                      </div>
                    </div>
                    <div style={{
                      padding: '8px',
                      borderRadius: 6,
                      background: 'rgba(168,85,247,0.08)',
                      border: `1px solid rgba(168,85,247,0.3)`,
                      textAlign: 'center',
                    }}>
                      <div style={{ color: '#a855f7', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4, fontWeight: 700 }}>v7.1</div>
                      <div style={{
                        fontWeight: 700,
                        color: latestWindow.v71_would_trade ? T.profit : T.loss,
                        fontSize: 11,
                      }}>
                        {latestWindow.v71_would_trade ? 'TRADE' : 'SKIP'}
                      </div>
                    </div>
                  </div>
                </div>
              ) : (
                <div style={{ color: T.label, fontSize: 10, textAlign: 'center', padding: '20px 0' }}>
                  No window data yet
                </div>
              )}
            </div>
          </div>
        </section>

        {/* § TRADE BUTTONS */}
        <section>
          <SectionHeader>MANUAL TRADING — place a $4 bet</SectionHeader>
          <div
            className="v58-grid-2"
            style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}
          >
            {/* Trade buttons */}
            <div style={{
              background: T.card,
              border: `1px solid ${T.border}`,
              borderRadius: 12,
              padding: '20px',
            }}>
              <TradeButtons
                latestWindow={latestWindow}
                onTradeSuccess={fetchManualTrades}
              />
            </div>

            {/* Current countdown (duplicate for trade context) */}
            <div style={{
              background: T.card,
              border: `1px solid ${T.border}`,
              borderRadius: 12,
              padding: '20px',
              display: 'flex',
              flexDirection: 'column',
            }}>
              <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 14 }}>
                § WINDOW TIMING
              </div>
              <CountdownTimer windowTs={latestWindow?.window_ts} />
            </div>
          </div>
        </section>

        {/* § MY TRADES */}
        <section>
          <SectionHeader>MY TRADES — manual paper & live bets</SectionHeader>
          <div style={{
            background: T.card,
            border: `1px solid ${T.border}`,
            borderRadius: 12,
            padding: '20px',
          }}>
            <MyTradesPanel
              trades={manualTrades}
              totalPnl={manualTotalPnl}
              loading={manualLoading}
            />
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

        {/* § ACCURACY SCOREBOARD */}
        <section>
          <SectionHeader>ACCURACY SCOREBOARD — last 100 windows</SectionHeader>
          <div style={{
            background: T.card,
            border: `1px solid ${T.border}`,
            borderRadius: 12,
            padding: '20px',
          }}>
            <AccuracyScoreboard accuracy={accuracy} />
          </div>
        </section>

        {/* § GATE vs WIN RATE ANALYSIS */}
        {gateAnalysis && gateAnalysis.buckets?.length > 0 && (
          <section>
            <SectionHeader>GATE vs WIN RATE — v7.1 Analysis</SectionHeader>
            <div style={{
              background: 'rgba(168,85,247,0.04)',
              border: '1px solid rgba(168,85,247,0.2)',
              borderRadius: 12,
              padding: '20px',
            }}>
              {/* Bar chart of WR by VPIN bucket */}
              <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', height: 120, marginBottom: 16 }}>
                {gateAnalysis.buckets.map((b) => {
                  const maxH = 100;
                  const barH = Math.max(4, (b.wr_pct / 100) * maxH);
                  const barColor = b.wr_pct >= 75 ? '#22c55e' : b.wr_pct >= 65 ? '#eab308' : '#ef4444';
                  return (
                    <div key={b.vpin_range} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
                      <div style={{ fontSize: 10, fontWeight: 700, color: barColor, fontFamily: T.mono }}>{b.wr_pct}%</div>
                      <div style={{
                        width: '100%',
                        height: barH,
                        background: barColor,
                        borderRadius: '4px 4px 0 0',
                        opacity: 0.8,
                        transition: 'height 300ms ease-out',
                      }} />
                      <div style={{ fontSize: 8, color: T.label, fontFamily: T.mono, textAlign: 'center' }}>{b.vpin_range}</div>
                      <div style={{ fontSize: 8, color: T.label2, fontFamily: T.mono }}>{b.wins}W/{b.losses}L</div>
                    </div>
                  );
                })}
              </div>

              {/* Cumulative gate table */}
              <div style={{ fontSize: 9, color: T.label, marginBottom: 8, fontWeight: 600, letterSpacing: '0.08em' }}>
                CUMULATIVE WR (if gate set at each level)
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: T.mono, fontSize: 10 }}>
                <thead>
                  <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                    {['Gate ≥', 'Trades', 'W/L', 'WR%', 'P&L'].map(h => (
                      <th key={h} style={{ padding: '4px 8px', textAlign: 'left', color: T.label, fontSize: 9, fontWeight: 600 }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {gateAnalysis.cumulative?.map((c) => {
                    const isCurrent = c.gate_at === '0.45-0.55' || c.gate_at === '<0.35' || c.gate_at === '0.35-0.45';
                    const isHighlight = gateAnalysis.best_gate && c.gate_at === gateAnalysis.best_gate.gate_at;
                    return (
                      <tr key={c.gate_at} style={{
                        borderBottom: `1px solid ${T.border}`,
                        background: isHighlight ? 'rgba(168,85,247,0.1)' : 'transparent',
                      }}>
                        <td style={{ padding: '4px 8px', color: isHighlight ? '#a855f7' : '#fff', fontWeight: isHighlight ? 700 : 400 }}>
                          {c.gate_at} {isHighlight && '★'}
                        </td>
                        <td style={{ padding: '4px 8px' }}>{c.total_trades}</td>
                        <td style={{ padding: '4px 8px' }}>{c.wins}/{c.losses}</td>
                        <td style={{ padding: '4px 8px', color: c.wr_pct >= 75 ? '#22c55e' : c.wr_pct >= 65 ? '#eab308' : '#ef4444', fontWeight: 700 }}>
                          {c.wr_pct}%
                        </td>
                        <td style={{ padding: '4px 8px', color: c.pnl >= 0 ? '#22c55e' : '#ef4444' }}>
                          {c.pnl >= 0 ? '+' : ''}${c.pnl.toFixed(2)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              {/* AI Suggestion */}
              {gateAnalysis.suggestion && (
                <div style={{
                  marginTop: 12,
                  padding: '10px 14px',
                  borderRadius: 8,
                  background: 'rgba(168,85,247,0.08)',
                  border: '1px solid rgba(168,85,247,0.25)',
                  fontSize: 10,
                  color: '#c084fc',
                  fontFamily: T.mono,
                  lineHeight: 1.5,
                }}>
                  🤖 <span style={{ fontWeight: 700 }}>AI Gate Suggestion:</span> {gateAnalysis.suggestion}
                </div>
              )}

              {/* Overall stats */}
              <div style={{ marginTop: 10, display: 'flex', gap: 16, fontSize: 9, color: T.label2, fontFamily: T.mono }}>
                <span>Overall: <strong style={{ color: '#a855f7' }}>{gateAnalysis.overall_wr}%</strong> ({gateAnalysis.total_wins}W/{gateAnalysis.total_losses}L)</span>
                <span>P&L: <strong style={{ color: gateAnalysis.total_pnl >= 0 ? '#22c55e' : '#ef4444' }}>{gateAnalysis.total_pnl >= 0 ? '+' : ''}${gateAnalysis.total_pnl.toFixed(2)}</strong></span>
                <span>Current Gate: <strong style={{ color: '#a855f7' }}>≥{gateAnalysis.current_gate}</strong></span>
              </div>
            </div>
          </section>
        )}

        {/* § OUTCOME HISTORY TABLE */}
        <section>
          <SectionHeader>OUTCOME HISTORY — click row to analyse</SectionHeader>
          <div style={{
            background: T.card,
            border: `1px solid ${T.border}`,
            borderRadius: 12,
            padding: '16px',
          }}>
            <OutcomeHistoryTable
              outcomes={outcomes}
              selectedTs={selectedOutcomeTs}
              onSelectWindow={(ts) => setSelectedOutcomeTs(prev => prev === ts ? null : ts)}
            />
          </div>
        </section>

        {/* § WHAT-IF ANALYSIS + SIGNAL SOURCE CARDS */}
        {selectedOutcomeTs && (() => {
          const selOutcome = outcomes.find(o => o.window_ts === selectedOutcomeTs) ?? null;
          return (
            <section>
              <SectionHeader>
                WHAT-IF ANALYSIS —{' '}
                {selectedOutcomeTs
                  ? new Date(selectedOutcomeTs).toLocaleString('en-GB', { dateStyle: 'short', timeStyle: 'medium' })
                  : 'select a window'}
              </SectionHeader>
              <div
                className="v58-grid-2"
                style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}
              >
                {/* What-If panel */}
                <div style={{
                  background: T.card,
                  border: `1px solid ${T.border}`,
                  borderRadius: 12,
                  padding: '20px',
                }}>
                  <div style={{
                    fontSize: 9, color: T.purple,
                    letterSpacing: '0.1em', textTransform: 'uppercase',
                    marginBottom: 14, fontFamily: T.mono, fontWeight: 600,
                  }}>
                    § WHAT IF WE BET $4
                  </div>
                  <WhatIfAnalysis outcome={selOutcome} />
                </div>

                {/* Signal source cards */}
                <div style={{
                  background: T.card,
                  border: `1px solid ${T.border}`,
                  borderRadius: 12,
                  padding: '20px',
                }}>
                  <div style={{
                    fontSize: 9, color: T.purple,
                    letterSpacing: '0.1em', textTransform: 'uppercase',
                    marginBottom: 14, fontFamily: T.mono, fontWeight: 600,
                  }}>
                    § SIGNAL SOURCES
                  </div>
                  <SignalSourceCards outcome={selOutcome} />
                </div>
              </div>
            </section>
          );
        })()}

      </div>
    </div>
  );
}
