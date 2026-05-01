// /desk Phase 3 — numeric conviction strip.
//
// Surfaces `conviction_score` (0-1, continuous) from /v4/snapshot. The
// older binary HIGH-everywhere `conviction` enum stratified poorly in
// audit #291 (99.4% of trades flagged HIGH); the numeric score is what
// the engine actually uses for stake sizing.
//
// Layout: a single horizontal bar 0→1 with a needle at the current
// score, plus the raw number and a tier hint to the right.

import React from 'react';
import { T } from '../../../theme/tokens.js';
import { tierForScore, TIER_BREAKS } from '../lib/convictionScore.js';

// Colour mapping per tier label — T.* tokens are runtime so we map here.
const TIER_COLOURS = {
  'NONE':      T.label,
  'LOW':       T.label2,
  'MEDIUM':    '#60a5fa',
  'HIGH':      T.warn,
  'VERY HIGH': T.profit,
};

function tierFor(score) {
  const t = tierForScore(score);
  return { ...t, color: TIER_COLOURS[t.label] || T.label };
}

export default function ConvictionStrip({ fiveMin }) {
  const raw = fiveMin?.conviction_score;
  const score = (typeof raw === 'number' && Number.isFinite(raw)) ? raw : null;
  const enumLabel = fiveMin?.conviction || null;
  const tier = tierFor(score);

  const pctWidth = score == null ? 0 : Math.max(0, Math.min(1, score)) * 100;

  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'baseline',
        marginBottom: 6,
      }}>
        <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em' }}>
          CONVICTION · numeric
        </div>
        <div style={{ fontSize: 11, color: tier.color, fontFamily: T.font }}>
          {score == null ? '—' : score.toFixed(3)}
          {' '}
          <span style={{ color: T.label2, fontSize: 10 }}>{tier.label}</span>
        </div>
      </div>

      {/* Bar */}
      <div style={{
        position: 'relative',
        height: 8,
        background: T.bg,
        border: `1px solid ${T.border}`,
        borderRadius: 2,
      }}>
        <div style={{
          width: `${pctWidth}%`,
          height: '100%',
          background: tier.color,
          opacity: 0.7,
        }} />
        {/* Tier break ticks */}
        {TIER_BREAKS.slice(1).map(t => (
          <div
            key={t.min}
            title={t.label}
            style={{
              position: 'absolute',
              left: `${t.min * 100}%`,
              top: -2,
              bottom: -2,
              width: 1,
              background: T.label,
              opacity: 0.35,
            }}
          />
        ))}
      </div>

      {enumLabel ? (
        <div style={{
          marginTop: 6,
          fontSize: 10,
          color: T.label2,
          fontFamily: T.font,
        }}>
          enum tier: <span style={{ color: T.text }}>{enumLabel}</span>
          {' · '}
          <span title="Audit #291: enum tier was HIGH on 99.4% of 14d trades — numeric score is what to read.">
            see numeric
          </span>
        </div>
      ) : null}
    </div>
  );
}
