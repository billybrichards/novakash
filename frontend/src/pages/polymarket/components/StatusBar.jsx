import React, { useState, useEffect, useRef, useCallback } from 'react';
import { T, fmt } from './theme.js';
import { useApi } from '../../../hooks/useApi.js';

// ── localStorage keys ────────────────────────────────────────────────────────
const LS_CAP_MODE  = 'btc-trader-cap-mode';   // 'dynamic' | 'manual'
const LS_CAP_VALUE = 'btc-trader-cap-value';  // e.g. '0.65'

function readCapMode()  { return localStorage.getItem(LS_CAP_MODE)  || 'dynamic'; }
function readCapValue() { return localStorage.getItem(LS_CAP_VALUE) || '0.65'; }

// ── Cap Mode Toggle component ─────────────────────────────────────────────────
function CapModeControl() {
  const api = useApi();
  const [mode, setMode]   = useState(readCapMode);
  const [value, setValue] = useState(readCapValue);
  const [saved, setSaved] = useState(false);  // brief "SAVED" flash
  const saveTimer = useRef(null);

  // Persist to localStorage whenever either changes
  useEffect(() => { localStorage.setItem(LS_CAP_MODE,  mode);  }, [mode]);
  useEffect(() => { localStorage.setItem(LS_CAP_VALUE, value); }, [value]);

  const persist = useCallback(async (nextMode, nextValue) => {
    // Fire-and-forget: write both keys to the engine config
    try {
      const isDynamic = nextMode === 'dynamic';
      await Promise.all([
        api.post('/v58/config/upsert', {
          service: 'engine', key: 'V10_DYNAMIC_CAP_MODE', value: String(isDynamic),
        }),
        api.post('/v58/config/upsert', {
          service: 'engine', key: 'V10_MANUAL_CAP', value: String(nextValue),
        }),
      ]);
      setSaved(true);
      clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => setSaved(false), 1500);
    } catch (_) {
      // Non-critical: localStorage already updated, engine will pick up on restart
    }
  }, [api]);

  const toggleMode = () => {
    const next = mode === 'dynamic' ? 'manual' : 'dynamic';
    setMode(next);
    persist(next, value);
  };

  const handleValueChange = (e) => {
    const raw = e.target.value;
    setValue(raw);
    const num = parseFloat(raw);
    if (!isNaN(num) && num >= 0.30 && num <= 0.90) {
      persist(mode, num.toFixed(2));
    }
  };

  const isDynamic = mode === 'dynamic';

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      {/* Mode toggle button */}
      <button
        onClick={toggleMode}
        title={isDynamic ? 'Click to switch to Manual cap' : 'Click to switch to Dynamic cap'}
        style={{
          padding: '3px 8px', borderRadius: 3, border: 'none', cursor: 'pointer',
          fontFamily: T.mono, fontSize: 10, fontWeight: 700, letterSpacing: '0.08em',
          background: isDynamic ? 'rgba(6,182,212,0.15)' : 'rgba(168,85,247,0.15)',
          color: isDynamic ? T.cyan : T.purple,
          boxShadow: `0 0 0 1px ${isDynamic ? 'rgba(6,182,212,0.35)' : 'rgba(168,85,247,0.35)'}`,
          transition: 'all 0.15s',
        }}
      >
        {isDynamic ? 'AUTO CAP' : 'MAN CAP'}
      </button>

      {/* Manual value input — only visible in manual mode */}
      {!isDynamic && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
          <span style={{ fontSize: 9, color: T.textMuted }}>$</span>
          <input
            type="number"
            min="0.30" max="0.90" step="0.01"
            value={value}
            onChange={handleValueChange}
            style={{
              width: 46, padding: '2px 4px', borderRadius: 3, textAlign: 'center',
              background: 'rgba(0,0,0,0.35)', border: `1px solid ${T.purple}55`,
              color: T.purple, fontFamily: T.mono, fontSize: 11, fontWeight: 700,
            }}
          />
        </div>
      )}

      {/* Brief "SAVED" flash */}
      {saved && (
        <span style={{ fontSize: 8, color: T.green, letterSpacing: '0.06em' }}>SAVED</span>
      )}
    </div>
  );
}

/**
 * Band 1 — Status Bar (pinned top).
 *
 * Shows: mode badge, bankroll, session W/L, ungated W/L, current window
 * with countdown, feed health dots.
 */

const FEEDS = ['Binance', 'Chainlink', 'Tiingo', 'CoinGlass', 'Gamma', 'CLOB', 'TimesFM'];

function FeedDot({ name, connected }) {
  const color = connected === true ? T.green
    : connected === false ? T.red
    : T.amber;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }} title={`${name}: ${connected === true ? 'OK' : connected === false ? 'DOWN' : 'UNKNOWN'}`}>
      <span style={{
        display: 'inline-block', width: 7, height: 7, borderRadius: '50%',
        background: color, boxShadow: `0 0 4px ${color}66`,
      }} />
      <span style={{ fontSize: 8, color: T.textMuted, letterSpacing: '0.04em' }}>{name}</span>
    </div>
  );
}

export default function StatusBar({ hqData, dashStats, accuracy, tradeStats }) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const system = hqData?.system || {};
  const isPaper = system.paper_mode !== false;
  const bankroll = dashStats?.balance ?? system.bankroll ?? 0;

  // Session W/L from v10_stats
  const v10 = hqData?.v10_stats || {};
  const sessionWins = v10.wins ?? 0;
  const sessionLosses = v10.losses ?? 0;
  const sessionTotal = sessionWins + sessionLosses;
  const sessionWR = sessionTotal > 0 ? ((sessionWins / sessionTotal) * 100).toFixed(0) : '\u2014';

  // Ungated W/L from accuracy endpoint
  const accData = accuracy || {};
  const ungatedWins = accData.correct ?? accData.wins ?? 0;
  const ungatedTotal = accData.total ?? 0;
  const ungatedLosses = ungatedTotal - ungatedWins;
  const ungatedWR = ungatedTotal > 0 ? ((ungatedWins / ungatedTotal) * 100).toFixed(0) : '\u2014';

  // Current window + countdown
  const windows = hqData?.windows || [];
  const latestWindow = windows[0] || {};
  const windowTs = latestWindow.window_ts;
  let windowLabel = '\u2014';
  let countdown = '\u2014';
  if (windowTs) {
    const wDate = new Date(typeof windowTs === 'number' ? windowTs * 1000 : windowTs);
    windowLabel = wDate.toISOString().slice(11, 16) + 'Z';
    // Countdown: seconds until window close (window_ts is the close time)
    const closeMs = wDate.getTime();
    const diffSec = Math.max(0, Math.round((closeMs - now) / 1000));
    countdown = diffSec > 0 ? `T-${diffSec}` : 'CLOSED';
  }

  // Feed health from system status
  const feeds = system.feeds || {};
  const feedStatuses = FEEDS.map(name => {
    const key = name.toLowerCase();
    const status = feeds[key];
    if (status === true || status === 'connected' || status === 'ok') return true;
    if (status === false || status === 'disconnected' || status === 'error') return false;
    return null; // unknown
  });

  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      background: T.headerBg, border: `1px solid ${T.cardBorder}`,
      padding: '6px 12px', borderRadius: 3, marginBottom: 6, flexShrink: 0,
      fontFamily: T.mono, flexWrap: 'wrap', gap: 8,
    }}>
      {/* Left cluster */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        {/* Mode badge */}
        <div style={{
          padding: '3px 10px', borderRadius: 3, fontSize: 11, fontWeight: 800,
          letterSpacing: '0.1em',
          background: isPaper ? 'rgba(245,158,11,0.15)' : 'rgba(239,68,68,0.15)',
          border: `1px solid ${isPaper ? 'rgba(245,158,11,0.4)' : 'rgba(239,68,68,0.4)'}`,
          color: isPaper ? T.amber : T.red,
        }}>
          {isPaper ? 'PAPER' : 'LIVE'}
        </div>

        <div style={{ height: 16, width: 1, background: T.cardBorder }} />

        {/* Bankroll */}
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
          <span style={{ fontSize: 8, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Bankroll</span>
          <span style={{ fontSize: 15, fontWeight: 700, color: T.green }}>${fmt(bankroll)}</span>
        </div>

        <div style={{ height: 16, width: 1, background: T.cardBorder }} />

        {/* Session W/L */}
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
          <span style={{ fontSize: 8, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Session</span>
          <span style={{ fontSize: 12, fontWeight: 600, color: T.purple }}>
            {sessionWins}W/{sessionLosses}L = {sessionWR}%
          </span>
        </div>

        {/* Ungated W/L */}
        {ungatedTotal > 0 && (
          <>
            <div style={{ height: 16, width: 1, background: T.cardBorder }} />
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
              <span style={{ fontSize: 8, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Ungated</span>
              <span style={{ fontSize: 12, fontWeight: 600, color: T.cyan }}>
                {ungatedWins}W/{ungatedLosses}L = {ungatedWR}%
              </span>
            </div>
          </>
        )}
      </div>

      {/* Right cluster */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        {/* Current window + countdown */}
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
          <span style={{ fontSize: 8, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Window</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: T.text }}>{windowLabel}</span>
            <span style={{
              fontSize: 11, fontWeight: 700,
              color: countdown === 'CLOSED' ? T.textMuted : T.amber,
              animation: countdown !== 'CLOSED' && countdown !== '\u2014' ? 'pulse 2s infinite' : 'none',
            }}>{countdown}</span>
          </div>
        </div>

        <div style={{ height: 16, width: 1, background: T.cardBorder }} />

        {/* Cap mode toggle */}
        <CapModeControl />

        <div style={{ height: 16, width: 1, background: T.cardBorder }} />

        {/* Feed health dots */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          {FEEDS.map((name, i) => (
            <FeedDot key={name} name={name} connected={feedStatuses[i]} />
          ))}
        </div>

        <div style={{ height: 16, width: 1, background: T.cardBorder }} />

        {/* System time */}
        <span style={{ fontSize: 11, color: T.textMuted }}>
          {new Date(now).toISOString().slice(11, 19)} UTC
        </span>
      </div>
    </div>
  );
}
