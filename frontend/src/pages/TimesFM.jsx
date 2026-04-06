/**
 * TimesFM.jsx — TimesFM v2.2 Calibrated Probability Monitor
 *
 * Connects to the real TimesFM v2.2 API via nginx proxy at /timesfm/.
 * Endpoints:
 *   GET /timesfm/v2/probability?asset=BTC&seconds_to_close=N  — calibrated P(UP)
 *   GET /timesfm/v2/health                                     — model status
 *   GET /timesfm/forecast                                      — v1 frozen forecast
 *
 * No JWT auth — raw fetch() only. Polls every 5 seconds.
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';

// ─── Theme tokens ─────────────────────────────────────────────────────────────
const T = {
  bg:          '#07070c',
  card:        'rgba(255,255,255,0.015)',
  border:      'rgba(255,255,255,0.06)',
  borderBright:'rgba(255,255,255,0.1)',
  profit:      '#4ade80',
  loss:        '#f87171',
  warning:     '#f59e0b',
  purple:      '#a855f7',
  cyan:        '#06b6d4',
  text:        'rgba(255,255,255,0.92)',
  textSec:     'rgba(255,255,255,0.45)',
  textMut:     'rgba(255,255,255,0.25)',
  mono:        "'IBM Plex Mono', monospace",
};

// ─── Inject font (once) ──────────────────────────────────────────────────────
if (!document.getElementById('ibm-plex-mono-font')) {
  const link = document.createElement('link');
  link.id = 'ibm-plex-mono-font';
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap';
  document.head.appendChild(link);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function secondsToClose() {
  const now = Math.floor(Date.now() / 1000);
  return 300 - (now % 300);
}

function formatCountdown(secs) {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function confidenceColor(pUp) {
  if (pUp == null) return T.textMut;
  if (pUp >= 0.7 || pUp <= 0.3) return T.profit;
  if (pUp >= 0.4 && pUp <= 0.6) return T.loss;
  return T.warning;
}

function directionFromP(pUp) {
  if (pUp == null) return null;
  return pUp >= 0.5 ? 'UP' : 'DOWN';
}

function stalenessColor(ms) {
  if (ms == null) return T.textMut;
  if (ms < 10000) return T.profit;
  if (ms < 30000) return T.warning;
  return T.loss;
}

function stalenessLabel(ms) {
  if (ms == null) return '--';
  if (ms < 1000) return '<1s';
  return `${(ms / 1000).toFixed(1)}s`;
}

// ─── Status Badge ─────────────────────────────────────────────────────────────

function StatusBadge({ label, color, pulse = false }) {
  return (
    <div style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: 6,
      padding: '3px 10px',
      borderRadius: 20,
      background: `${color}12`,
      border: `1px solid ${color}33`,
      fontSize: 11,
      fontFamily: T.mono,
      fontWeight: 600,
      color,
    }}>
      {pulse && (
        <span style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          background: color,
          boxShadow: `0 0 6px ${color}`,
          animation: 'pulseDot 2s ease-in-out infinite',
        }} />
      )}
      {label}
    </div>
  );
}

// ─── Card wrapper ─────────────────────────────────────────────────────────────

function Card({ children, style = {} }) {
  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.border}`,
      borderRadius: 8,
      padding: 16,
      ...style,
    }}>
      {children}
    </div>
  );
}

// ─── Section label ────────────────────────────────────────────────────────────

function SectionLabel({ children }) {
  return (
    <div style={{
      fontSize: 9,
      fontFamily: T.mono,
      fontWeight: 600,
      letterSpacing: '0.12em',
      textTransform: 'uppercase',
      color: T.textMut,
      marginBottom: 10,
    }}>
      {children}
    </div>
  );
}

// ─── Stat row ─────────────────────────────────────────────────────────────────

function StatRow({ label, value, color = T.text, sub = null }) {
  return (
    <div style={{
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'center',
      padding: '5px 0',
      borderBottom: `1px solid ${T.border}`,
    }}>
      <span style={{ fontSize: 11, fontFamily: T.mono, color: T.textSec }}>{label}</span>
      <div style={{ textAlign: 'right' }}>
        <span style={{ fontSize: 13, fontFamily: T.mono, fontWeight: 600, color }}>{value}</span>
        {sub && (
          <div style={{ fontSize: 9, fontFamily: T.mono, color: T.textMut, marginTop: 1 }}>{sub}</div>
        )}
      </div>
    </div>
  );
}

// ─── Freshness indicator ──────────────────────────────────────────────────────

function FreshnessRow({ label, ms }) {
  const col = stalenessColor(ms);
  return (
    <div style={{
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'center',
      padding: '4px 0',
    }}>
      <span style={{ fontSize: 10, fontFamily: T.mono, color: T.textSec }}>{label}</span>
      <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
        <span style={{
          width: 5,
          height: 5,
          borderRadius: '50%',
          background: col,
          boxShadow: `0 0 4px ${col}`,
        }} />
        <span style={{ fontSize: 10, fontFamily: T.mono, color: col }}>{stalenessLabel(ms)}</span>
      </div>
    </div>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function TimesFM() {
  // API state
  const [v2Data, setV2Data] = useState(null);
  const [v1Data, setV1Data] = useState(null);
  const [health, setHealth] = useState(null);
  const [v2Error, setV2Error] = useState(null);
  const [v1Error, setV1Error] = useState(null);
  const [healthError, setHealthError] = useState(null);
  const [countdown, setCountdown] = useState(secondsToClose());
  const [lastFetch, setLastFetch] = useState(null);

  // ── Fetch helpers ───────────────────────────────────────────────────────────

  const fetchV2 = useCallback(async () => {
    try {
      const stc = secondsToClose();
      const res = await fetch(`/timesfm/v2/probability?asset=BTC&seconds_to_close=${stc}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setV2Data(data);
      setV2Error(null);
    } catch (err) {
      setV2Error(err.message);
    }
  }, []);

  const fetchV1 = useCallback(async () => {
    try {
      const res = await fetch('/timesfm/forecast');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setV1Data(data);
      setV1Error(null);
    } catch (err) {
      setV1Error(err.message);
    }
  }, []);

  const fetchHealth = useCallback(async () => {
    try {
      const res = await fetch('/timesfm/v2/health');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setHealth(data);
      setHealthError(null);
    } catch (err) {
      setHealthError(err.message);
    }
  }, []);

  // ── Polling (5s) ────────────────────────────────────────────────────────────

  useEffect(() => {
    // Initial fetch
    fetchV2();
    fetchV1();
    fetchHealth();

    const interval = setInterval(() => {
      fetchV2();
      fetchV1();
      fetchHealth();
      setLastFetch(new Date());
    }, 5000);

    return () => clearInterval(interval);
  }, [fetchV2, fetchV1, fetchHealth]);

  // ── Countdown ticker (1s) ───────────────────────────────────────────────────

  useEffect(() => {
    const tick = setInterval(() => {
      setCountdown(secondsToClose());
    }, 1000);
    return () => clearInterval(tick);
  }, []);

  // ── Derived values ──────────────────────────────────────────────────────────

  const pUp = v2Data?.probability_up ?? v2Data?.p_up ?? null;
  const direction = directionFromP(pUp);
  const confColor = confidenceColor(pUp);
  const modelVersion = v2Data?.model_version ?? health?.model_version ?? 'v2.2';
  const assetsLoaded = health?.assets_loaded ?? health?.assets ?? null;
  const healthOk = health && !healthError;

  const v1Direction = v1Data?.direction ?? null;
  const v1Confidence = v1Data?.confidence ?? null;

  // Feature staleness from v2 response
  const binanceStaleness = v2Data?.feature_staleness?.binance_ms ?? v2Data?.staleness?.binance ?? null;
  const coinglassStaleness = v2Data?.feature_staleness?.coinglass_ms ?? v2Data?.staleness?.coinglass ?? null;
  const timesfmStaleness = v2Data?.feature_staleness?.timesfm_ms ?? v2Data?.staleness?.timesfm ?? null;

  // Agreement check
  const v1v2Agree = direction && v1Direction ? direction === v1Direction : null;

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: 'calc(100vh - 52px)',
      background: T.bg,
      overflow: 'hidden',
    }}>
      {/* ── Header ────────────────────────────────────────────────────────── */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '10px 20px',
        borderBottom: `1px solid ${T.border}`,
        flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 14, fontFamily: T.mono, fontWeight: 700, color: T.text }}>
            TimesFM v2.2
          </span>
          <span style={{ fontSize: 11, fontFamily: T.mono, color: T.textMut }}>
            Calibrated Probability Monitor
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {healthOk ? (
            <StatusBadge label="MODEL ONLINE" color={T.profit} pulse />
          ) : healthError ? (
            <StatusBadge label="MODEL OFFLINE" color={T.loss} />
          ) : (
            <StatusBadge label="CONNECTING..." color={T.textMut} />
          )}

          {assetsLoaded && (
            <span style={{ fontSize: 10, fontFamily: T.mono, color: T.textSec }}>
              {Array.isArray(assetsLoaded) ? assetsLoaded.join(', ') : assetsLoaded}
            </span>
          )}

          <span style={{ fontSize: 11, fontFamily: T.mono, color: T.warning }}>
            {formatCountdown(countdown)}
          </span>
        </div>
      </div>

      {/* ── Body ──────────────────────────────────────────────────────────── */}
      <div style={{
        flex: 1,
        display: 'flex',
        overflow: 'hidden',
        minHeight: 0,
        padding: 16,
        gap: 16,
      }}>
        {/* ── Left: Main prediction + v1/v2 comparison ─────────────────── */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0 }}>

          {/* Main Prediction Card */}
          <Card style={{ flex: 0 }}>
            <SectionLabel>v2.2 Calibrated Prediction — BTC</SectionLabel>

            {v2Error ? (
              <div style={{
                padding: 20,
                textAlign: 'center',
                color: T.loss,
                fontFamily: T.mono,
                fontSize: 13,
              }}>
                Service unavailable: {v2Error}
              </div>
            ) : pUp == null ? (
              <div style={{
                padding: 20,
                textAlign: 'center',
                color: T.textMut,
                fontFamily: T.mono,
                fontSize: 13,
              }}>
                Loading...
              </div>
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
                {/* Direction arrow */}
                <div style={{
                  width: 80,
                  height: 80,
                  borderRadius: 12,
                  background: `${direction === 'UP' ? T.profit : T.loss}10`,
                  border: `2px solid ${direction === 'UP' ? T.profit : T.loss}40`,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0,
                }}>
                  <span style={{
                    fontSize: 40,
                    color: direction === 'UP' ? T.profit : T.loss,
                    lineHeight: 1,
                  }}>
                    {direction === 'UP' ? '\u2191' : '\u2193'}
                  </span>
                </div>

                {/* P(UP) display */}
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                    <span style={{
                      fontSize: 48,
                      fontFamily: T.mono,
                      fontWeight: 700,
                      color: confColor,
                      lineHeight: 1,
                    }}>
                      {(pUp * 100).toFixed(1)}%
                    </span>
                    <span style={{
                      fontSize: 14,
                      fontFamily: T.mono,
                      fontWeight: 600,
                      color: direction === 'UP' ? T.profit : T.loss,
                    }}>
                      P(UP)
                    </span>
                  </div>

                  <div style={{
                    marginTop: 8,
                    fontSize: 11,
                    fontFamily: T.mono,
                    color: T.textSec,
                    display: 'flex',
                    gap: 12,
                    flexWrap: 'wrap',
                  }}>
                    <span>Direction: <span style={{ color: direction === 'UP' ? T.profit : T.loss, fontWeight: 600 }}>{direction}</span></span>
                    <span>Window closes in: <span style={{ color: T.warning }}>{formatCountdown(countdown)}</span></span>
                    <StatusBadge label={modelVersion} color={T.purple} />
                  </div>

                  {/* Confidence bar */}
                  <div style={{ marginTop: 12 }}>
                    <div style={{
                      height: 6,
                      background: 'rgba(255,255,255,0.05)',
                      borderRadius: 3,
                      overflow: 'hidden',
                      position: 'relative',
                    }}>
                      {/* Center marker at 50% */}
                      <div style={{
                        position: 'absolute',
                        left: '50%',
                        top: 0,
                        width: 1,
                        height: '100%',
                        background: 'rgba(255,255,255,0.15)',
                      }} />
                      {/* P(UP) fill from left */}
                      <div style={{
                        height: '100%',
                        width: `${pUp * 100}%`,
                        background: `linear-gradient(90deg, ${T.loss}, ${T.textMut} 40%, ${T.warning} 50%, ${T.profit} 70%, ${T.profit})`,
                        borderRadius: 3,
                        transition: 'width 0.3s ease',
                      }} />
                    </div>
                    <div style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      fontSize: 9,
                      fontFamily: T.mono,
                      color: T.textMut,
                      marginTop: 3,
                    }}>
                      <span>0% (DOWN)</span>
                      <span>50%</span>
                      <span>100% (UP)</span>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* Feature freshness */}
            {v2Data && (
              <div style={{ marginTop: 16, borderTop: `1px solid ${T.border}`, paddingTop: 10 }}>
                <SectionLabel>Feature Freshness</SectionLabel>
                <FreshnessRow label="Binance" ms={binanceStaleness} />
                <FreshnessRow label="CoinGlass" ms={coinglassStaleness} />
                <FreshnessRow label="TimesFM v1" ms={timesfmStaleness} />
              </div>
            )}
          </Card>

          {/* v1 vs v2 Comparison Card */}
          <Card style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
            <SectionLabel>v1 vs v2 Comparison</SectionLabel>

            <div style={{ display: 'flex', gap: 16 }}>
              {/* v1 column */}
              <div style={{
                flex: 1,
                padding: 12,
                borderRadius: 6,
                background: 'rgba(255,255,255,0.02)',
                border: `1px solid ${T.border}`,
              }}>
                <div style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  marginBottom: 10,
                }}>
                  <span style={{ fontSize: 12, fontFamily: T.mono, fontWeight: 600, color: T.textSec }}>v1 Frozen</span>
                  <StatusBadge label="ECE ~0.40" color={T.loss} />
                </div>

                {v1Error ? (
                  <div style={{ fontSize: 11, fontFamily: T.mono, color: T.loss }}>Offline: {v1Error}</div>
                ) : v1Data ? (
                  <>
                    <div style={{
                      fontSize: 28,
                      fontFamily: T.mono,
                      fontWeight: 700,
                      color: v1Direction === 'UP' ? T.profit : v1Direction === 'DOWN' ? T.loss : T.textMut,
                      marginBottom: 4,
                    }}>
                      {v1Direction ?? '--'}
                    </div>
                    <div style={{ fontSize: 11, fontFamily: T.mono, color: T.textSec }}>
                      Heuristic conf: {v1Confidence != null ? `${(v1Confidence * 100).toFixed(0)}%` : '--'}
                    </div>
                    <div style={{
                      marginTop: 8,
                      padding: '4px 8px',
                      borderRadius: 4,
                      background: 'rgba(248,113,113,0.08)',
                      border: `1px solid ${T.loss}30`,
                      fontSize: 10,
                      fontFamily: T.mono,
                      color: T.warning,
                    }}>
                      Poorly calibrated — heuristic confidence unreliable
                    </div>
                  </>
                ) : (
                  <div style={{ fontSize: 11, fontFamily: T.mono, color: T.textMut }}>Loading...</div>
                )}
              </div>

              {/* v2 column */}
              <div style={{
                flex: 1,
                padding: 12,
                borderRadius: 6,
                background: 'rgba(168,85,247,0.03)',
                border: `1px solid ${T.purple}20`,
              }}>
                <div style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  marginBottom: 10,
                }}>
                  <span style={{ fontSize: 12, fontFamily: T.mono, fontWeight: 600, color: T.purple }}>v2.2 Calibrated</span>
                  <StatusBadge label="ECE ~0.14" color={T.profit} />
                </div>

                {v2Error ? (
                  <div style={{ fontSize: 11, fontFamily: T.mono, color: T.loss }}>Offline: {v2Error}</div>
                ) : pUp != null ? (
                  <>
                    <div style={{
                      fontSize: 28,
                      fontFamily: T.mono,
                      fontWeight: 700,
                      color: direction === 'UP' ? T.profit : T.loss,
                      marginBottom: 4,
                    }}>
                      {direction} {(pUp * 100).toFixed(1)}%
                    </div>
                    <div style={{ fontSize: 11, fontFamily: T.mono, color: T.textSec }}>
                      Calibrated P(UP) — trusted source
                    </div>
                  </>
                ) : (
                  <div style={{ fontSize: 11, fontFamily: T.mono, color: T.textMut }}>Loading...</div>
                )}
              </div>
            </div>

            {/* Agreement indicator */}
            <div style={{
              marginTop: 12,
              padding: '8px 12px',
              borderRadius: 6,
              background: v1v2Agree == null
                ? 'rgba(255,255,255,0.02)'
                : v1v2Agree
                ? 'rgba(74,222,128,0.06)'
                : 'rgba(248,113,113,0.06)',
              border: `1px solid ${v1v2Agree == null ? T.border : v1v2Agree ? `${T.profit}30` : `${T.loss}30`}`,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 8,
            }}>
              <span style={{
                fontSize: 12,
                fontFamily: T.mono,
                fontWeight: 600,
                color: v1v2Agree == null ? T.textMut : v1v2Agree ? T.profit : T.loss,
              }}>
                {v1v2Agree == null ? 'Waiting for data...' : v1v2Agree ? 'v1 and v2 AGREE' : 'v1 and v2 DISAGREE'}
              </span>
              {v1v2Agree != null && (
                <span style={{ fontSize: 11, fontFamily: T.mono, color: T.textSec }}>
                  v1: {v1Direction ?? '--'} / v2: {direction ?? '--'}
                </span>
              )}
            </div>
          </Card>
        </div>

        {/* ── Right: Stats sidebar ─────────────────────────────────────── */}
        <div style={{ width: 280, flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 16 }}>

          {/* Training Accuracy */}
          <Card>
            <SectionLabel>v2.2 Training Accuracy</SectionLabel>
            <StatRow label="BTC T-30" value="86%" color={T.profit} sub="30s before close" />
            <StatRow label="BTC T-60" value="80%" color={T.profit} sub="60s before close" />
          </Card>

          {/* Calibration */}
          <Card>
            <SectionLabel>Calibration (ECE)</SectionLabel>
            <StatRow
              label="v1 Frozen"
              value="~0.40"
              color={T.loss}
              sub="Poorly calibrated"
            />
            <StatRow
              label="v2.2 Calibrated"
              value="~0.14"
              color={T.profit}
              sub="Well calibrated"
            />
            <div style={{
              marginTop: 8,
              fontSize: 10,
              fontFamily: T.mono,
              color: T.textMut,
              lineHeight: 1.5,
            }}>
              Lower ECE = better calibration. A P(UP) of 0.70 should win ~70% of the time.
            </div>
          </Card>

          {/* Model Info */}
          <Card>
            <SectionLabel>Model Info</SectionLabel>
            <StatRow label="Version" value={modelVersion} color={T.purple} />
            <StatRow
              label="Status"
              value={healthOk ? 'Online' : healthError ? 'Offline' : '...'}
              color={healthOk ? T.profit : T.loss}
            />
            {health?.uptime && (
              <StatRow label="Uptime" value={health.uptime} color={T.textSec} />
            )}
            {lastFetch && (
              <StatRow
                label="Last poll"
                value={lastFetch.toLocaleTimeString()}
                color={T.textSec}
              />
            )}
          </Card>

          {/* Shadow Mode Notice */}
          <Card style={{
            background: 'rgba(168,85,247,0.04)',
            border: `1px solid ${T.purple}25`,
          }}>
            <SectionLabel>Integration Status</SectionLabel>
            <div style={{
              fontSize: 11,
              fontFamily: T.mono,
              color: T.warning,
              lineHeight: 1.6,
            }}>
              v2 is shadow mode -- not wired into engine yet. Predictions are logged but not traded on.
            </div>
            <div style={{
              marginTop: 8,
              fontSize: 10,
              fontFamily: T.mono,
              color: T.textMut,
              lineHeight: 1.5,
            }}>
              v1 frozen forecast remains the engine signal source. v2 calibrated probabilities are displayed here for monitoring and comparison.
            </div>
          </Card>
        </div>
      </div>

      {/* ── Styles ────────────────────────────────────────────────────────── */}
      <style>{`
        @keyframes pulseDot {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
      `}</style>
    </div>
  );
}
