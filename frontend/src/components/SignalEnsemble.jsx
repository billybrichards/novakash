/**
 * SignalEnsemble.jsx — Signal ensemble panel for Indicators page.
 *
 * Shows all 5 signals (TimesFM, Gamma, TWAP-Delta, CoinGlass, VPIN),
 * their directions, confidence bars, weights, and the aggregated
 * weighted-score with trade/skip recommendation.
 *
 * Conflict detection highlights when signals disagree.
 */

import React from 'react';

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

// ── Signal Card ───────────────────────────────────────────────────────────────

function SignalCard({ signal, isConflicting }) {
  const isUp = signal.direction === 'UP';
  const isNeutral = signal.direction === 'NEUTRAL';
  const dirColor = isNeutral ? T.textSec : isUp ? T.profit : T.loss;
  const confPct = signal.confidence * 100;
  const weightPct = signal.weight * 100;

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${isConflicting ? 'rgba(248,113,113,0.25)' : T.border}`,
      borderRadius: 10,
      padding: '14px',
      transition: 'border-color 300ms',
      position: 'relative',
      overflow: 'hidden',
    }}>
      {/* Conflict indicator stripe */}
      {isConflicting && (
        <div style={{
          position: 'absolute',
          top: 0,
          left: 0,
          width: 3,
          height: '100%',
          background: T.loss,
          opacity: 0.6,
        }} />
      )}

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <span style={{ fontSize: 16 }}>{signal.icon}</span>
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: T.text, fontFamily: T.mono }}>
              {signal.name}
            </div>
            <div style={{ fontSize: 10, color: T.textMut, fontFamily: "'Inter', sans-serif" }}>
              {signal.source}
            </div>
          </div>
        </div>

        {/* Direction badge */}
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 4,
          padding: '3px 10px',
          borderRadius: 20,
          background: isNeutral ? 'rgba(255,255,255,0.04)' : `${dirColor}15`,
          border: `1px solid ${dirColor}44`,
          fontSize: 12,
          fontFamily: T.mono,
          fontWeight: 700,
          color: dirColor,
        }}>
          <span>{isNeutral ? '—' : isUp ? '↑' : '↓'}</span>
          <span>{signal.direction}</span>
        </div>
      </div>

      {/* Confidence bar */}
      <div style={{ marginBottom: 6 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
          <span style={{ fontSize: 10, color: T.textMut, fontFamily: "'Inter', sans-serif" }}>Confidence</span>
          <span style={{ fontSize: 11, color: dirColor, fontFamily: T.mono, fontWeight: 600 }}>
            {confPct.toFixed(1)}%
          </span>
        </div>
        <div style={{ height: 4, background: 'rgba(255,255,255,0.05)', borderRadius: 2, overflow: 'hidden' }}>
          <div style={{
            height: '100%',
            width: `${confPct}%`,
            background: `linear-gradient(90deg, ${signal.color}66, ${signal.color})`,
            borderRadius: 2,
            boxShadow: `0 0 6px ${signal.color}44`,
            transition: 'width 500ms ease-out',
          }} />
        </div>
      </div>

      {/* Weight */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontSize: 10, color: T.textMut, fontFamily: "'Inter', sans-serif" }}>Weight</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {/* Weight mini-bar */}
          <div style={{ width: 40, height: 2, background: 'rgba(255,255,255,0.05)', borderRadius: 1 }}>
            <div style={{
              height: '100%',
              width: `${weightPct / 0.35 * 40}%`,
              maxWidth: '100%',
              background: signal.color,
              borderRadius: 1,
              opacity: 0.6,
            }} />
          </div>
          <span style={{ fontSize: 10, color: T.textSec, fontFamily: T.mono }}>
            {weightPct.toFixed(0)}%
          </span>
        </div>
      </div>
    </div>
  );
}

// ── Weighted Score Bar ────────────────────────────────────────────────────────

function WeightedScoreBar({ aggregate }) {
  const { upScore, downScore, direction, confidence, hasConflict, shouldTrade, recommendation } = aggregate;
  const totalPct = upScore + downScore;
  const upPct = totalPct > 0 ? (upScore / totalPct) * 100 : 50;
  const downPct = 100 - upPct;

  const recColor = shouldTrade
    ? (direction === 'UP' ? T.profit : T.loss)
    : hasConflict
    ? T.loss
    : T.warning;

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${hasConflict ? 'rgba(248,113,113,0.3)' : T.border}`,
      borderRadius: 12,
      padding: '18px',
      marginBottom: 16,
      ...(hasConflict ? { boxShadow: '0 0 20px rgba(248,113,113,0.08)' } : {}),
    }}>
      {/* Title */}
      <div style={{
        fontSize: 10,
        fontFamily: T.mono,
        color: T.textMut,
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
        marginBottom: 14,
      }}>
        Weighted Signal Score
      </div>

      {/* UP/DOWN split bar */}
      <div style={{ display: 'flex', height: 12, borderRadius: 6, overflow: 'hidden', marginBottom: 8 }}>
        <div style={{
          width: `${upPct}%`,
          background: `linear-gradient(90deg, rgba(74,222,128,0.5), #4ade80)`,
          transition: 'width 600ms ease-out',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'flex-start',
          paddingLeft: 8,
        }}>
          {upPct > 20 && (
            <span style={{ fontSize: 9, fontFamily: T.mono, color: 'rgba(0,0,0,0.7)', fontWeight: 700 }}>
              ↑ {upPct.toFixed(0)}%
            </span>
          )}
        </div>
        <div style={{
          width: `${downPct}%`,
          background: `linear-gradient(90deg, #f87171, rgba(248,113,113,0.5))`,
          transition: 'width 600ms ease-out',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'flex-end',
          paddingRight: 8,
        }}>
          {downPct > 20 && (
            <span style={{ fontSize: 9, fontFamily: T.mono, color: 'rgba(0,0,0,0.7)', fontWeight: 700 }}>
              {downPct.toFixed(0)}% ↓
            </span>
          )}
        </div>
      </div>

      {/* Scores row */}
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        fontSize: 11,
        fontFamily: T.mono,
        marginBottom: 14,
        color: T.textSec,
      }}>
        <span style={{ color: T.profit }}>UP: {(upScore * 100).toFixed(1)}%</span>
        <span style={{ color: T.textMut }}>|</span>
        <span style={{ color: T.loss }}>DOWN: {(downScore * 100).toFixed(1)}%</span>
      </div>

      {/* Recommendation box */}
      <div style={{
        padding: '12px 16px',
        borderRadius: 8,
        background: `${recColor}12`,
        border: `1px solid ${recColor}33`,
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      }}>
        <span style={{ fontSize: 18 }}>
          {shouldTrade ? '🎯' : hasConflict ? '⚠️' : '⏸️'}
        </span>
        <div>
          <div style={{ fontSize: 13, fontFamily: T.mono, fontWeight: 700, color: recColor }}>
            {recommendation}
          </div>
          <div style={{ fontSize: 10, color: T.textMut, marginTop: 2, fontFamily: "'Inter', sans-serif" }}>
            {hasConflict
              ? 'Signals disagree — elevated risk'
              : shouldTrade
              ? `Combined confidence ${(confidence * 100).toFixed(0)}% · weight-adjusted`
              : 'Confidence below threshold'
            }
          </div>
        </div>
      </div>

      {/* Conflict alert */}
      {hasConflict && (
        <div style={{
          marginTop: 10,
          padding: '8px 12px',
          borderRadius: 6,
          background: 'rgba(248,113,113,0.06)',
          border: '1px solid rgba(248,113,113,0.2)',
          fontSize: 11,
          fontFamily: T.mono,
          color: T.loss,
          display: 'flex',
          alignItems: 'center',
          gap: 6,
        }}>
          <span>⚡</span>
          <span>CONFLICT DETECTED — signals near 50/50 split</span>
        </div>
      )}
    </div>
  );
}

// ── CoinGlass Data Row ────────────────────────────────────────────────────────

function CoinGlassPanel({ data }) {
  if (!data) return null;
  const fundingColor = data.fundingRate >= 0 ? T.profit : T.loss;
  const oiChangeColor = data.oiChange24h >= 0 ? T.profit : T.loss;

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.border}`,
      borderRadius: 10,
      padding: '14px',
      marginTop: 12,
    }}>
      <div style={{
        fontSize: 10,
        fontFamily: T.mono,
        color: T.textMut,
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
        marginBottom: 10,
      }}>
        CoinGlass Data
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
        {[
          { label: 'Taker Buy Ratio', value: `${(data.takerBuyRatio * 100).toFixed(1)}%`, color: data.takerBuyRatio >= 0.5 ? T.profit : T.loss },
          { label: 'Funding Rate', value: `${(data.fundingRate * 100).toFixed(4)}%`, color: fundingColor },
          { label: 'Open Interest', value: `$${(data.openInterest / 1e9).toFixed(2)}B`, color: T.cyan },
          { label: 'OI Change 24h', value: `${(data.oiChange24h * 100).toFixed(2)}%`, color: oiChangeColor },
        ].map(({ label, value, color }) => (
          <div key={label} style={{
            background: 'rgba(255,255,255,0.02)',
            borderRadius: 6,
            padding: '8px 10px',
          }}>
            <div style={{ fontSize: 9, color: T.textMut, fontFamily: "'Inter', sans-serif", marginBottom: 3 }}>
              {label}
            </div>
            <div style={{ fontSize: 13, fontFamily: T.mono, fontWeight: 600, color }}>
              {value}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Main Export ───────────────────────────────────────────────────────────────

export default function SignalEnsemble({ signalData = null }) {
  if (!signalData) {
    return (
      <div style={{ color: T.textMut, textAlign: 'center', padding: '40px 20px', fontFamily: T.mono }}>
        Loading signal data…
      </div>
    );
  }

  const { signals, aggregate, coinglassData } = signalData;
  const aggDir = aggregate.direction;

  // Which signals conflict with the aggregate direction?
  const conflictingSignals = signals
    .filter(s => s.direction !== 'NEUTRAL' && s.direction !== aggDir)
    .map(s => s.id);

  return (
    <div>
      {/* Weighted Score — top of panel */}
      <WeightedScoreBar aggregate={aggregate} />

      {/* Signal cards grid */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
        gap: 10,
        marginBottom: 4,
      }}>
        {signals.map(signal => (
          <SignalCard
            key={signal.id}
            signal={signal}
            isConflicting={conflictingSignals.includes(signal.id)}
          />
        ))}
      </div>

      {/* CoinGlass data */}
      <CoinGlassPanel data={coinglassData} />
    </div>
  );
}
