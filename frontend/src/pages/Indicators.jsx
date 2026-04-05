/**
 * Indicators.jsx — TWAP-Delta + Signal Ensemble screen.
 *
 * Left panel: TWAP-Delta chart with Gamma overlay + agreement indicator
 * Right panel: Signal Ensemble (all 5 signals + weighted score + conflict detection)
 */

import React, { useState, useEffect, useRef } from 'react';
import TWAPChart from '../components/TWAPChart.jsx';
import SignalEnsemble from '../components/SignalEnsemble.jsx';
import {
  generateTWAPDeltaSeries,
  generateSignals,
  generateVPIN,
  generateGammaPrices,
  generateBTCTick,
} from '../lib/mock-data.js';

const T = {
  bg: '#07070c',
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  profit: '#4ade80',
  loss: '#f87171',
  warning: '#f59e0b',
  purple: '#a855f7',
  cyan: '#06b6d4',
  text: 'rgba(255,255,255,0.92)',
  textSec: 'rgba(255,255,255,0.45)',
  textMut: 'rgba(255,255,255,0.25)',
  mono: "'IBM Plex Mono', monospace",
};

// ── Section wrapper ───────────────────────────────────────────────────────────

function Panel({ title, icon, subtitle, children, style = {} }) {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: '100%',
      overflow: 'hidden',
      ...style,
    }}>
      {/* Panel header */}
      <div style={{
        padding: '12px 16px',
        borderBottom: `1px solid ${T.border}`,
        flexShrink: 0,
        display: 'flex',
        alignItems: 'center',
        gap: 8,
      }}>
        <span style={{ fontSize: 16 }}>{icon}</span>
        <div>
          <div style={{ fontSize: 13, fontFamily: T.mono, fontWeight: 700, color: T.text }}>
            {title}
          </div>
          {subtitle && (
            <div style={{ fontSize: 10, color: T.textMut, fontFamily: "'Inter', sans-serif", marginTop: 1 }}>
              {subtitle}
            </div>
          )}
        </div>
      </div>

      {/* Panel body */}
      <div style={{ flex: 1, overflow: 'auto', padding: '16px' }}>
        {children}
      </div>
    </div>
  );
}

// ── VPIN Gauge ────────────────────────────────────────────────────────────────

function VPINGauge({ vpin }) {
  if (!vpin) return null;
  const pct = vpin.value * 100;
  const color = vpin.cascade ? T.loss : vpin.informed ? T.warning : T.profit;

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.border}`,
      borderRadius: 10,
      padding: '12px 16px',
      marginBottom: 16,
    }}>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        marginBottom: 8,
      }}>
        <div style={{ fontSize: 10, fontFamily: T.mono, color: T.textMut, letterSpacing: '0.1em', textTransform: 'uppercase' }}>
          VPIN Reading
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 16, fontFamily: T.mono, fontWeight: 700, color }}>
            {vpin.value.toFixed(4)}
          </span>
          <div style={{
            padding: '2px 8px',
            borderRadius: 12,
            background: `${color}15`,
            border: `1px solid ${color}44`,
            fontSize: 10,
            fontFamily: T.mono,
            fontWeight: 600,
            color,
          }}>
            {vpin.regime}
          </div>
        </div>
      </div>

      {/* Gauge bar */}
      <div style={{ height: 6, background: 'rgba(255,255,255,0.05)', borderRadius: 3, position: 'relative', overflow: 'visible' }}>
        {/* Threshold marks */}
        <div style={{
          position: 'absolute',
          left: '55%',
          top: -2,
          width: 1,
          height: 10,
          background: T.warning,
          opacity: 0.6,
        }} />
        <div style={{
          position: 'absolute',
          left: '70%',
          top: -2,
          width: 1,
          height: 10,
          background: T.loss,
          opacity: 0.6,
        }} />

        {/* Fill */}
        <div style={{
          height: '100%',
          width: `${pct}%`,
          background: `linear-gradient(90deg, rgba(74,222,128,0.6), ${color})`,
          borderRadius: 3,
          transition: 'width 500ms ease-out',
          boxShadow: `0 0 8px ${color}44`,
        }} />
      </div>

      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        marginTop: 4,
        fontSize: 9,
        fontFamily: T.mono,
        color: T.textMut,
      }}>
        <span>0.0 Quiet</span>
        <span style={{ color: T.warning }}>0.55 Informed</span>
        <span style={{ color: T.loss }}>0.70 Cascade</span>
        <span>1.0</span>
      </div>
    </div>
  );
}

// ── Live BTC Price ticker ─────────────────────────────────────────────────────

function LivePricePill({ price }) {
  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 6,
      padding: '4px 12px',
      borderRadius: 20,
      background: 'rgba(255,255,255,0.03)',
      border: `1px solid ${T.border}`,
      fontSize: 12,
      fontFamily: T.mono,
      color: T.text,
    }}>
      <span style={{
        width: 5,
        height: 5,
        borderRadius: '50%',
        background: T.profit,
        boxShadow: `0 0 5px ${T.profit}`,
        animation: 'pulseDot 2s ease-in-out infinite',
      }} />
      <span style={{ color: T.textMut, fontSize: 10 }}>BTC</span>
      <span style={{ fontWeight: 700 }}>
        {price ? `$${price.toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'}
      </span>
    </div>
  );
}

// ── Cascade Regime Banner ──────────────────────────────────────────────────────

function CascadeBanner({ vpin, cascadeSignal }) {
  if (!vpin) return null;
  const regime = vpin.regime;
  const isCascade = regime === 'CASCADE';
  const isInformed = regime === 'INFORMED';
  if (!isCascade && !isInformed) return null;

  const bgColor = isCascade ? 'rgba(248,113,113,0.08)' : 'rgba(245,158,11,0.06)';
  const borderColor = isCascade ? 'rgba(248,113,113,0.4)' : 'rgba(245,158,11,0.3)';
  const accentColor = isCascade ? T.loss : T.warning;
  const icon = isCascade ? '🎯' : '⚠️';
  const label = isCascade ? 'CASCADE BET SIGNAL' : 'INFORMED FLOW DETECTED';

  const direction = cascadeSignal?.direction;
  const dirEmoji = direction === 'down' || direction === 'DOWN' ? '📉' : direction === 'up' || direction === 'UP' ? '📈' : '—';
  const oiDelta = cascadeSignal?.oi_delta_pct;
  const liqVol = cascadeSignal?.liq_volume_usd;

  return (
    <div style={{
      margin: '0 20px', marginTop: 8,
      padding: '10px 16px',
      background: bgColor,
      border: `1px solid ${borderColor}`,
      borderRadius: 10,
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      flexShrink: 0,
      animation: isCascade ? 'cascadePulse 2s ease-in-out infinite' : undefined,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <span style={{ fontSize: 20 }}>{icon}</span>
        <div>
          <div style={{ fontSize: 12, fontFamily: T.mono, fontWeight: 700, color: accentColor, letterSpacing: '0.08em' }}>
            {label}
          </div>
          {direction && (
            <div style={{ fontSize: 13, fontFamily: T.mono, color: T.text, marginTop: 2 }}>
              Direction: {dirEmoji} <span style={{ fontWeight: 700 }}>{(direction || '').toUpperCase()}</span>
            </div>
          )}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 20, alignItems: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 9, fontFamily: T.mono, color: T.textMut, letterSpacing: '0.1em' }}>VPIN</div>
          <div style={{ fontSize: 14, fontFamily: T.mono, fontWeight: 700, color: accentColor }}>{vpin.value.toFixed(4)}</div>
        </div>
        {oiDelta != null && (
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 9, fontFamily: T.mono, color: T.textMut, letterSpacing: '0.1em' }}>OI Δ</div>
            <div style={{ fontSize: 14, fontFamily: T.mono, fontWeight: 700, color: T.text }}>{typeof oiDelta === 'number' ? `${oiDelta.toFixed(2)}%` : oiDelta}</div>
          </div>
        )}
        {liqVol != null && (
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 9, fontFamily: T.mono, color: T.textMut, letterSpacing: '0.1em' }}>LIQ 5m</div>
            <div style={{ fontSize: 14, fontFamily: T.mono, fontWeight: 700, color: T.text }}>{typeof liqVol === 'number' ? `$${(liqVol / 1e6).toFixed(2)}M` : liqVol}</div>
          </div>
        )}
        <div style={{
          padding: '4px 12px', borderRadius: 20,
          background: `${accentColor}15`, border: `1px solid ${accentColor}44`,
          fontSize: 11, fontFamily: T.mono, fontWeight: 700, color: accentColor,
        }}>
          {regime}
        </div>
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function Indicators() {
  const [twapSeries, setTwapSeries] = useState(() => generateTWAPDeltaSeries(60));
  const [signalData, setSignalData] = useState(null);
  const [vpin, setVpin] = useState(null);
  const [cascadeSignal, setCascadeSignal] = useState(null);
  const [btcPrice, setBtcPrice] = useState(67300);
  const tickRef = useRef(null);
  const refreshRef = useRef(null);

  // ── Initial load ────────────────────────────────────────────────────────────
  useEffect(() => {
    const vp = generateVPIN();
    setVpin(vp);
    setSignalData(generateSignals(67300, vp.value));
  }, []);

  // ── Live updates ────────────────────────────────────────────────────────────
  useEffect(() => {
    tickRef.current = setInterval(() => {
      // BTC price tick
      setBtcPrice(prev => {
        const tick = generateBTCTick(prev);
        return tick.price;
      });

      // VPIN update
      setVpin(generateVPIN());

      // Append TWAP data point
      setTwapSeries(prev => {
        const last = prev[prev.length - 1];
        const now = Date.now();
        const shock = (Math.random() - 0.48) * 0.4;
        const newDelta = (last?.delta ?? 0) + shock;
        const gammaShock = (Math.random() - 0.5) * 0.008;
        const newGammaUp = Math.max(0.05, Math.min(0.95, (last?.gammaUp ?? 0.62) + gammaShock));
        const newGammaDown = Math.max(0.05, Math.min(0.95, 0.97 - newGammaUp));
        const len = prev.length;

        const newPoint = {
          time: now,
          timeLabel: new Date(now).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }),
          delta: parseFloat(newDelta.toFixed(4)),
          twap: parseFloat((newDelta / Math.max(1, len)).toFixed(4)),
          gammaUp: parseFloat(newGammaUp.toFixed(4)),
          gammaDown: parseFloat(newGammaDown.toFixed(4)),
        };

        // Keep last 120 points
        return [...prev.slice(-119), newPoint];
      });
    }, 2000);

    // Signal refresh (every 10s) — also fetch cascade signal from API
    refreshRef.current = setInterval(() => {
      setVpin(prev => {
        const vp = generateVPIN();
        setBtcPrice(p => {
          setSignalData(generateSignals(p, vp.value));
          return p;
        });
        return vp;
      });
      // Fetch latest cascade signal from API
      const token = localStorage.getItem('token');
      if (token) {
        fetch('/api/signals/cascade?limit=1', { headers: { Authorization: `Bearer ${token}` } })
          .then(r => r.ok ? r.json() : null)
          .then(data => {
            if (data?.signals?.length > 0) setCascadeSignal(data.signals[0]);
          })
          .catch(() => {});
      }
    }, 10000);

    return () => {
      clearInterval(tickRef.current);
      clearInterval(refreshRef.current);
    };
  }, []);

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: 'calc(100vh - 52px)',
      background: T.bg,
      overflow: 'hidden',
    }}>
      {/* ── Page Header ────────────────────────────────────────────────────── */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '10px 20px',
        borderBottom: `1px solid ${T.border}`,
        flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 16 }}>📊</span>
          <span style={{ fontSize: 14, fontFamily: T.mono, fontWeight: 700, color: T.text }}>
            Signal Indicators
          </span>
          <span style={{ fontSize: 11, color: T.textMut, fontFamily: "'Inter', sans-serif" }}>
            TWAP-Delta · Ensemble · CoinGlass
          </span>
        </div>
        <LivePricePill price={btcPrice} />
      </div>

      {/* ── Cascade / Informed Banner ───────────────────────────────────────── */}
      <CascadeBanner vpin={vpin} cascadeSignal={cascadeSignal} />

      {/* ── Split layout ────────────────────────────────────────────────────── */}
      <div style={{
        flex: 1,
        display: 'flex',
        overflow: 'hidden',
        minHeight: 0,
      }}>
        {/* Left: TWAP-Delta (50%) */}
        <div style={{
          flex: '0 0 50%',
          borderRight: `1px solid ${T.border}`,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}>
          <Panel
            title="TWAP-Delta"
            icon="📈"
            subtitle="Order flow delta vs Gamma token prices — window accumulation"
          >
            {/* VPIN gauge */}
            <VPINGauge vpin={vpin} />

            {/* Chart */}
            <div style={{
              background: T.card,
              border: `1px solid ${T.border}`,
              borderRadius: 10,
              padding: '16px',
            }}>
              <TWAPChart data={twapSeries} height={280} />
            </div>

            {/* Legend */}
            <div style={{
              display: 'flex',
              gap: 16,
              marginTop: 12,
              fontSize: 10,
              fontFamily: T.mono,
              color: T.textMut,
              flexWrap: 'wrap',
            }}>
              {[
                { color: T.profit, label: 'Δ Delta (area)', dashed: false },
                { color: T.warning, label: 'TWAP avg', dashed: true },
                { color: T.profit, label: 'γ UP', dashed: false, opacity: 0.7 },
                { color: T.loss, label: 'γ DOWN', dashed: false, opacity: 0.7 },
              ].map(({ color, label, dashed, opacity = 1 }) => (
                <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <div style={{
                    width: 16,
                    height: 2,
                    background: color,
                    opacity,
                    borderRadius: 1,
                    borderTop: dashed ? `2px dashed ${color}` : undefined,
                  }} />
                  <span>{label}</span>
                </div>
              ))}
            </div>
          </Panel>
        </div>

        {/* Right: Signal Ensemble (50%) */}
        <div style={{
          flex: '0 0 50%',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}>
          <Panel
            title="Signal Ensemble"
            icon="🎯"
            subtitle="All signals combined · weighted score · conflict detection"
          >
            <SignalEnsemble signalData={signalData} />
          </Panel>
        </div>
      </div>

      <style>{`
        @keyframes pulseDot {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
        @keyframes cascadePulse {
          0%, 100% { box-shadow: 0 0 0 0 rgba(248,113,113,0); }
          50% { box-shadow: 0 0 12px 2px rgba(248,113,113,0.15); }
        }
      `}</style>
    </div>
  );
}
