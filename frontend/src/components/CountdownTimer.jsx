/**
 * CountdownTimer.jsx — v7 Window Countdown Component
 *
 * Shows:
 * - Progress bar for the current 5-minute window (300s)
 * - T-180/T-120/T-90/T-60 evaluation stage markers
 * - Current stage highlighted with a glow
 * - Seconds remaining displayed prominently
 * - Auto-detects the current live window from system clock
 *
 * Props:
 *   windowTs   {string|Date} — ISO timestamp of the last known window start
 *                              (used as a hint, but we always prefer live clock)
 *   className  {string}      — additional CSS class
 */

import React, { useState, useEffect } from 'react';

// Window duration in seconds
const WINDOW_DURATION = 300;

// Evaluation stage checkpoints (seconds REMAINING until window close)
const STAGES = [
  { key: 't180', label: 'T-180', secondsRemaining: 180, color: '#a855f7' },
  { key: 't120', label: 'T-120', secondsRemaining: 120, color: '#06b6d4' },
  { key: 't90',  label: 'T-90',  secondsRemaining: 90,  color: '#f59e0b' },
  { key: 't60',  label: 'T-60',  secondsRemaining: 60,  color: '#f87171' },
];

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Auto-detect the CURRENT active window from system clock.
 * window_ts = floor(now / 300) * 300  (unix seconds)
 * Returns { windowTs, secondsRemaining, secondsElapsed }
 */
function getCurrentWindowState() {
  const nowSec = Date.now() / 1000;
  const windowTs = Math.floor(nowSec / WINDOW_DURATION) * WINDOW_DURATION;
  const windowEnd = windowTs + WINDOW_DURATION;
  const secondsRemaining = Math.max(0, Math.floor(windowEnd - nowSec));
  const secondsElapsed = WINDOW_DURATION - secondsRemaining;
  return { windowTs, secondsRemaining, secondsElapsed };
}

/**
 * Returns the current active stage based on secondsRemaining.
 */
function getActiveStage(secondsRemaining) {
  if (secondsRemaining <= 0) return null;
  const passed = STAGES.filter(s => secondsRemaining <= s.secondsRemaining);
  if (passed.length === 0) return null;
  return passed.reduce((best, s) =>
    s.secondsRemaining > best.secondsRemaining ? s : best
  );
}

/**
 * Format seconds as MM:SS.
 */
function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function CountdownTimer({ windowTs, className = '' }) {
  // Always drive from live clock — windowTs prop is informational only
  const [state, setState] = useState(() => getCurrentWindowState());

  // Tick every second using live clock
  useEffect(() => {
    const tick = () => setState(getCurrentWindowState());
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []); // no dependency on windowTs — we use the clock

  const { secondsRemaining, secondsElapsed } = state;
  const progress = Math.min(1, Math.max(0, secondsElapsed / WINDOW_DURATION));
  const pct = Math.round(progress * 100);
  const activeStage = getActiveStage(secondsRemaining);
  // In a 5-min window, there's always time remaining (it resets every 300s)
  // "between windows" means seconds 0–1 briefly at the boundary
  const isBoundary = secondsRemaining === 0;

  // Status colour
  const barColor = isBoundary
    ? 'rgba(255,255,255,0.2)'
    : activeStage?.color ?? '#4ade80';

  return (
    <div
      className={className}
      style={{
        fontFamily: "'IBM Plex Mono', monospace",
        width: '100%',
      }}
    >
      {/* ── Header row ─────────────────────────────────────────────────── */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: 10,
      }}>
        {/* Big countdown */}
        <div style={{
          fontSize: 28,
          fontWeight: 700,
          color: isBoundary ? 'rgba(255,255,255,0.25)' : barColor,
          letterSpacing: '-0.02em',
          transition: 'color 300ms ease-out',
        }}>
          {isBoundary ? '0:00' : formatTime(secondsRemaining)}
        </div>

        {/* Active stage badge */}
        {activeStage && !isBoundary ? (
          <div style={{
            padding: '4px 10px',
            borderRadius: 6,
            background: `${activeStage.color}18`,
            border: `1px solid ${activeStage.color}55`,
            color: activeStage.color,
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: '0.06em',
            boxShadow: `0 0 10px ${activeStage.color}33`,
            animation: 'stagePulse 2s ease-in-out infinite',
          }}>
            {activeStage.label}
          </div>
        ) : isBoundary ? (
          <div style={{
            padding: '4px 10px',
            borderRadius: 6,
            background: 'rgba(74,222,128,0.08)',
            border: '1px solid rgba(74,222,128,0.2)',
            color: '#4ade80',
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: '0.06em',
          }}>
            NEW WINDOW
          </div>
        ) : (
          <div style={{
            padding: '4px 10px',
            borderRadius: 6,
            background: 'rgba(74,222,128,0.08)',
            border: '1px solid rgba(74,222,128,0.2)',
            color: '#4ade80',
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: '0.06em',
          }}>
            RUNNING
          </div>
        )}
      </div>

      {/* ── Progress bar track ──────────────────────────────────────────── */}
      <div style={{
        position: 'relative',
        height: 8,
        background: 'rgba(255,255,255,0.06)',
        borderRadius: 4,
        overflow: 'visible',
        marginBottom: 28,
      }}>
        {/* Filled bar */}
        <div style={{
          position: 'absolute',
          left: 0,
          top: 0,
          height: '100%',
          width: `${pct}%`,
          background: barColor,
          borderRadius: 4,
          boxShadow: isBoundary ? 'none' : `0 0 8px ${barColor}88`,
          transition: 'width 800ms linear, background 300ms ease-out',
        }} />

        {/* Stage marker pins */}
        {STAGES.map(stage => {
          const positionPct = ((WINDOW_DURATION - stage.secondsRemaining) / WINDOW_DURATION) * 100;
          const isPast = secondsRemaining <= stage.secondsRemaining;
          const isActive = activeStage?.key === stage.key;

          return (
            <div
              key={stage.key}
              style={{
                position: 'absolute',
                left: `${positionPct}%`,
                top: '50%',
                transform: 'translate(-50%, -50%)',
                width: 12,
                height: 12,
                borderRadius: '50%',
                background: isPast ? stage.color : 'rgba(255,255,255,0.12)',
                border: `2px solid ${isPast ? stage.color : 'rgba(255,255,255,0.2)'}`,
                boxShadow: isActive ? `0 0 12px ${stage.color}` : 'none',
                transition: 'all 300ms ease-out',
                zIndex: 2,
              }}
            >
              {/* Label below pin */}
              <div style={{
                position: 'absolute',
                top: '100%',
                left: '50%',
                transform: 'translateX(-50%)',
                marginTop: 5,
                fontSize: 9,
                fontWeight: isActive ? 700 : 400,
                color: isPast ? stage.color : 'rgba(255,255,255,0.25)',
                whiteSpace: 'nowrap',
                letterSpacing: '0.04em',
                transition: 'color 300ms ease-out',
              }}>
                {stage.label}
              </div>
            </div>
          );
        })}
      </div>

      {/* ── Stage grid ─────────────────────────────────────────────────── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(4, 1fr)',
        gap: 6,
        marginTop: 4,
      }}>
        {STAGES.map(stage => {
          const isPast = secondsRemaining <= stage.secondsRemaining;
          const isActive = activeStage?.key === stage.key && !isBoundary;
          return (
            <div
              key={stage.key}
              style={{
                padding: '8px 6px',
                borderRadius: 8,
                background: isActive
                  ? `${stage.color}15`
                  : isPast
                  ? 'rgba(255,255,255,0.04)'
                  : 'rgba(255,255,255,0.02)',
                border: `1px solid ${isActive
                  ? `${stage.color}55`
                  : isPast
                  ? 'rgba(255,255,255,0.08)'
                  : 'rgba(255,255,255,0.04)'}`,
                textAlign: 'center',
                transition: 'all 250ms ease-out',
                boxShadow: isActive ? `0 0 12px ${stage.color}22` : 'none',
              }}
            >
              <div style={{
                fontSize: 11,
                fontWeight: 700,
                color: isActive
                  ? stage.color
                  : isPast
                  ? 'rgba(255,255,255,0.5)'
                  : 'rgba(255,255,255,0.2)',
                marginBottom: 2,
                letterSpacing: '0.04em',
                transition: 'color 300ms ease-out',
              }}>
                {stage.label}
              </div>
              <div style={{
                fontSize: 9,
                color: 'rgba(255,255,255,0.25)',
                letterSpacing: '0.06em',
              }}>
                {isActive ? '● NOW' : isPast ? '✓' : `${stage.secondsRemaining}s left`}
              </div>
            </div>
          );
        })}
      </div>

      {/* Animations */}
      <style>{`
        @keyframes stagePulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.65; }
        }
      `}</style>
    </div>
  );
}
