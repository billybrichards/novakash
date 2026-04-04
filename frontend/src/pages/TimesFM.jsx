/**
 * TimesFM.jsx — Main TimesFM Forecast Screen
 *
 * Full-screen BTC trading terminal:
 * - Left (60%): Candlestick chart with forecast overlay
 * - Right (40%): Forecast panel (direction, confidence, history, model status)
 * - Bottom bar: Window info, gamma prices, VPIN, regime
 *
 * All data is mock until the TimesFM backend connects.
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import ForecastChart from '../components/ForecastChart.jsx';
import ForecastPanel from '../components/ForecastPanel.jsx';
import {
  generateBTCCandles,
  generateBTCTick,
  generateForecast,
  generateForecastLine,
  generateGammaPrices,
  generateVPIN,
  getWindowInfo,
  generateForecastHistory,
} from '../lib/mock-data.js';
import { createTimesFMClient, formatWSStatus, WS_STATUS } from '../lib/websocket.js';

const T = {
  bg: '#07070c',
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  borderBright: 'rgba(255,255,255,0.1)',
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

// ── Bottom Bar Sub-components ─────────────────────────────────────────────────

function BottomStat({ label, value, color = T.text, dot = false, dotColor }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span style={{ fontSize: 9, color: T.textMut, fontFamily: T.mono, letterSpacing: '0.1em', textTransform: 'uppercase' }}>
        {label}
      </span>
      <span style={{
        fontSize: 13,
        fontFamily: T.mono,
        fontWeight: 600,
        color,
        display: 'flex',
        alignItems: 'center',
        gap: 5,
      }}>
        {dot && (
          <span style={{
            width: 5,
            height: 5,
            borderRadius: '50%',
            background: dotColor || color,
            boxShadow: `0 0 5px ${dotColor || color}`,
            display: 'inline-block',
            animation: 'pulseDot 2s ease-in-out infinite',
          }} />
        )}
        {value}
      </span>
    </div>
  );
}

function Divider() {
  return (
    <div style={{ width: 1, height: 30, background: T.border, alignSelf: 'center', flexShrink: 0 }} />
  );
}

// ── Regime Badge ──────────────────────────────────────────────────────────────

function RegimeBadge({ regime }) {
  const map = {
    QUIET: { color: T.textSec, bg: 'rgba(255,255,255,0.04)', label: 'QUIET' },
    NORMAL: { color: T.cyan, bg: 'rgba(6,182,212,0.08)', label: 'NORMAL' },
    INFORMED: { color: T.warning, bg: 'rgba(245,158,11,0.08)', label: 'INFORMED' },
    CASCADE: { color: T.loss, bg: 'rgba(248,113,113,0.08)', label: 'CASCADE ⚡' },
  };
  const s = map[regime] || map.NORMAL;

  return (
    <div style={{
      padding: '2px 10px',
      borderRadius: 20,
      background: s.bg,
      border: `1px solid ${s.color}44`,
      fontSize: 11,
      fontFamily: T.mono,
      fontWeight: 700,
      color: s.color,
      letterSpacing: '0.06em',
    }}>
      {s.label}
    </div>
  );
}

// ── Page Header ───────────────────────────────────────────────────────────────

function PageHeader({ wsStatus, windowInfo }) {
  const ws = formatWSStatus(wsStatus);

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '10px 20px',
      borderBottom: `1px solid ${T.border}`,
      flexShrink: 0,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span style={{ fontSize: 16 }}>🔮</span>
        <span style={{ fontSize: 14, fontFamily: T.mono, fontWeight: 700, color: T.text }}>
          TimesFM Forecast
        </span>
        <span style={{ fontSize: 11, color: T.textMut, fontFamily: "'Inter', sans-serif" }}>
          BTC/USD · 1H Window
        </span>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        {/* WS status pill */}
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 5,
          padding: '3px 10px',
          borderRadius: 20,
          background: `${ws.color}12`,
          border: `1px solid ${ws.color}33`,
          fontSize: 10,
          fontFamily: T.mono,
          fontWeight: 600,
          color: ws.color,
        }}>
          {ws.dot && (
            <span style={{
              width: 5,
              height: 5,
              borderRadius: '50%',
              background: ws.color,
              boxShadow: `0 0 5px ${ws.color}`,
              animation: 'pulseDot 2s ease-in-out infinite',
            }} />
          )}
          {ws.label}
        </div>

        {windowInfo && (
          <span style={{ fontSize: 11, fontFamily: T.mono, color: T.textMut }}>
            Window closes in{' '}
            <span style={{ color: T.warning }}>{windowInfo.remainingStr}</span>
          </span>
        )}
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function TimesFM() {
  // ── State ───────────────────────────────────────────────────────────────────
  const [candles, setCandles] = useState(() => generateBTCCandles(72));
  const [btcPrice, setBtcPrice] = useState(null);
  const [forecast, setForecast] = useState(null);
  const [forecastLine, setForecastLine] = useState([]);
  const [forecastHistory, setForecastHistory] = useState(() => generateForecastHistory(10));
  const [gamma, setGamma] = useState(null);
  const [vpin, setVpin] = useState(null);
  const [windowInfo, setWindowInfo] = useState(null);
  const [wsStatus, setWsStatus] = useState(WS_STATUS.DISCONNECTED);

  const wsClientRef = useRef(null);
  const tickIntervalRef = useRef(null);
  const windowIntervalRef = useRef(null);
  const forecastIntervalRef = useRef(null);

  // ── Init mock data ──────────────────────────────────────────────────────────
  useEffect(() => {
    const initCandles = generateBTCCandles(72);
    const lastCandle = initCandles[initCandles.length - 1];
    const price = lastCandle?.close ?? 67300;

    setCandles(initCandles);
    setBtcPrice(price);

    const win = getWindowInfo();
    setWindowInfo(win);

    const fc = generateForecast(price);
    setForecast(fc);

    const fcLine = generateForecastLine(
      win.windowOpenUnix,
      win.windowCloseUnix,
      fc.predictedClose
    );
    setForecastLine(fcLine);

    setGamma(generateGammaPrices(fc.direction, fc.confidence));
    setVpin(generateVPIN());

    return () => {};
  }, []);

  // ── Live tick simulation ────────────────────────────────────────────────────
  useEffect(() => {
    tickIntervalRef.current = setInterval(() => {
      setBtcPrice(prev => {
        const tick = generateBTCTick(prev ?? 67300);
        const now = Math.floor(Date.now() / 1000);

        // Append or update last candle
        setCandles(prevCandles => {
          if (!prevCandles.length) return prevCandles;
          const last = prevCandles[prevCandles.length - 1];
          const candleTime = Math.floor(now / 3600) * 3600;

          if (last.time === candleTime) {
            // Update existing candle
            const updated = {
              ...last,
              close: tick.price,
              high: Math.max(last.high, tick.price),
              low: Math.min(last.low, tick.price),
            };
            return [...prevCandles.slice(0, -1), updated];
          } else {
            // New candle
            const newCandle = {
              time: candleTime,
              open: tick.price,
              high: tick.price,
              low: tick.price,
              close: tick.price,
              volume: 100,
            };
            return [...prevCandles.slice(-100), newCandle];
          }
        });

        return tick.price;
      });

      // Update VPIN
      setVpin(generateVPIN());
    }, 3000);

    return () => clearInterval(tickIntervalRef.current);
  }, []);

  // ── Window + forecast refresh ───────────────────────────────────────────────
  useEffect(() => {
    windowIntervalRef.current = setInterval(() => {
      setWindowInfo(getWindowInfo());
    }, 1000);

    forecastIntervalRef.current = setInterval(() => {
      setBtcPrice(prev => {
        const price = prev ?? 67300;
        const win = getWindowInfo();
        const fc = generateForecast(price);
        const fcLine = generateForecastLine(
          win.windowOpenUnix,
          win.windowCloseUnix,
          fc.predictedClose
        );
        setForecast(fc);
        setForecastLine(fcLine);
        setGamma(generateGammaPrices(fc.direction, fc.confidence));
        return price;
      });
    }, 15000);

    return () => {
      clearInterval(windowIntervalRef.current);
      clearInterval(forecastIntervalRef.current);
    };
  }, []);

  // ── WebSocket (TimesFM backend) ─────────────────────────────────────────────
  useEffect(() => {
    const client = createTimesFMClient({
      onStatus: setWsStatus,
      onForecast: (payload) => {
        if (payload?.direction) setForecast(payload);
      },
      onError: (err) => {
        // Silently fall back to mock data
        console.debug('[TimesFM WS] Using mock data:', err.message);
      },
    });

    client.connect();
    wsClientRef.current = client;

    return () => {
      client.disconnect();
    };
  }, []);

  // ── Derived ─────────────────────────────────────────────────────────────────
  const wpinRegime = vpin?.regime ?? 'NORMAL';
  const progressPct = windowInfo?.progressPct ?? 0;

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: 'calc(100vh - 52px)',
      background: T.bg,
      overflow: 'hidden',
    }}>
      {/* ── Header ────────────────────────────────────────────────────────── */}
      <PageHeader wsStatus={wsStatus} windowInfo={windowInfo} />

      {/* ── Main content: chart + panel ──────────────────────────────────── */}
      <div style={{
        flex: 1,
        display: 'flex',
        overflow: 'hidden',
        minHeight: 0,
      }}>
        {/* Left: Chart (60%) */}
        <div style={{
          flex: '0 0 60%',
          display: 'flex',
          flexDirection: 'column',
          padding: '12px',
          borderRight: `1px solid ${T.border}`,
          minWidth: 0,
          overflow: 'hidden',
        }}>
          {/* Chart header */}
          <div style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 8,
            flexShrink: 0,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ fontSize: 11, fontFamily: T.mono, color: T.textSec }}>₿ BTC/USD</span>
              <span style={{ fontSize: 11, fontFamily: T.mono, color: T.textMut }}>1H</span>
              {btcPrice && (
                <span style={{
                  fontSize: 16,
                  fontFamily: T.mono,
                  fontWeight: 700,
                  color: T.text,
                }}>
                  ${btcPrice.toLocaleString('en-US', { maximumFractionDigits: 0 })}
                </span>
              )}
            </div>

            {/* Legend */}
            <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
              {[
                { color: T.profit, label: 'Forecast' },
                { color: T.purple, label: 'Confidence Band' },
                { color: T.cyan, label: 'Window Open' },
              ].map(({ color, label }) => (
                <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <div style={{ width: 16, height: 2, background: color, borderRadius: 1, opacity: 0.7 }} />
                  <span style={{ fontSize: 10, color: T.textMut, fontFamily: "'Inter', sans-serif" }}>{label}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Chart */}
          <div style={{ flex: 1, minHeight: 0 }}>
            <ForecastChart
              candles={candles}
              forecastLine={forecastLine}
              quantiles={forecast?.quantiles}
              windowInfo={windowInfo ? {
                openPrice: forecast?.windowOpenPrice,
                openUnix: windowInfo.windowOpenUnix,
                closeUnix: windowInfo.windowCloseUnix,
              } : null}
              forecast={forecast}
              height="100%"
            />
          </div>

          {/* Window progress bar */}
          {windowInfo && (
            <div style={{ marginTop: 8, flexShrink: 0 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4, fontSize: 10, fontFamily: T.mono, color: T.textMut }}>
                <span>Window Progress</span>
                <span>{progressPct.toFixed(1)}% · {windowInfo.remainingStr} remaining</span>
              </div>
              <div style={{ height: 3, background: 'rgba(255,255,255,0.05)', borderRadius: 2, overflow: 'hidden' }}>
                <div style={{
                  height: '100%',
                  width: `${progressPct}%`,
                  background: `linear-gradient(90deg, ${T.purple}, ${T.cyan})`,
                  borderRadius: 2,
                  transition: 'width 1s linear',
                  boxShadow: `0 0 8px ${T.purple}44`,
                }} />
              </div>
            </div>
          )}
        </div>

        {/* Right: Forecast panel (40%) */}
        <div style={{
          flex: '0 0 40%',
          padding: '12px',
          overflowY: 'auto',
          minWidth: 0,
        }}>
          <ForecastPanel
            forecast={forecast}
            btcPrice={btcPrice}
            forecastHistory={forecastHistory}
            wsStatus={wsStatus}
          />
        </div>
      </div>

      {/* ── Bottom Status Bar ────────────────────────────────────────────── */}
      <div style={{
        flexShrink: 0,
        borderTop: `1px solid ${T.border}`,
        background: 'rgba(0,0,0,0.25)',
        padding: '8px 20px',
        display: 'flex',
        alignItems: 'center',
        gap: 20,
        flexWrap: 'wrap',
        overflowX: 'auto',
      }}>
        <BottomStat label="Asset" value="BTC/USD" color={T.text} />
        <Divider />
        <BottomStat label="Timeframe" value="1H Window" color={T.textSec} />
        <Divider />
        {windowInfo && (
          <>
            <BottomStat
              label="Window Opens"
              value={new Date(windowInfo.windowOpenTs).toLocaleTimeString()}
              color={T.textSec}
            />
            <Divider />
            <BottomStat
              label="Window Closes"
              value={new Date(windowInfo.windowCloseTs).toLocaleTimeString()}
              color={T.warning}
            />
            <Divider />
          </>
        )}
        {gamma && (
          <>
            <BottomStat
              label="γ UP Token"
              value={`$${gamma.up.toFixed(4)}`}
              color={T.profit}
              dot
              dotColor={T.profit}
            />
            <Divider />
            <BottomStat
              label="γ DOWN Token"
              value={`$${gamma.down.toFixed(4)}`}
              color={T.loss}
            />
            <Divider />
          </>
        )}
        {vpin && (
          <>
            <BottomStat
              label="VPIN"
              value={vpin.value.toFixed(4)}
              color={vpin.cascade ? T.loss : vpin.informed ? T.warning : T.textSec}
            />
            <Divider />
          </>
        )}
        {vpin && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <span style={{ fontSize: 9, color: T.textMut, fontFamily: T.mono, letterSpacing: '0.1em', textTransform: 'uppercase' }}>
              Regime
            </span>
            <RegimeBadge regime={wpinRegime} />
          </div>
        )}
      </div>

      {/* Styles */}
      <style>{`
        @keyframes pulseDot {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }

        @media (max-width: 768px) {
          /* Stack chart above panel on mobile */
        }
      `}</style>
    </div>
  );
}
