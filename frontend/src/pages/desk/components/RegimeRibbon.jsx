// /desk Phase 2 — regime ribbon (coloured band + confidence + persistence).
//
// Live engine regime labels (confirmed via /v4/snapshot):
//   chop           → blue      (noise / no edge)
//   calm_trend     → green     (directional, trade it)
//   volatile_trend → amber     (directional but wider spreads)
//   risk_off       → red       (cascade / kill mode)
//
// These DIFFER from the spec's CHOP/TREND/CASCADE vocabulary and from the
// FE-onboarding's LOW_VOL/NORMAL/HIGH_VOL/TRENDING. We go with what live
// returns — if the engine renames downstream, one map to update here.
//
// The ribbon renders each regime as a coloured segment; the current one
// is highlighted with a brighter border. Below it we show
// regime_confidence + regime_persistence as small stats.

import React from 'react';
import { T } from '../../../theme/tokens.js';

// Ordered list — we keep a stable left-to-right ordering so operators
// learn the layout rather than the list reshuffling on each regime flip.
const REGIMES = [
  { key: 'chop',           label: 'CHOP',           color: T.cyan },
  { key: 'calm_trend',     label: 'CALM TREND',     color: T.profit },
  { key: 'volatile_trend', label: 'VOL TREND',      color: T.warn },
  { key: 'risk_off',       label: 'RISK-OFF',       color: T.loss },
];

export default function RegimeRibbon({ fiveMin }) {
  const regime = (fiveMin?.regime ?? '').toString().toLowerCase();
  const confidence = num(fiveMin?.regime_confidence);
  const persistence = num(fiveMin?.regime_persistence);
  const transitions = fiveMin?.regime_transition || null;

  const current = REGIMES.find(r => r.key === regime) ?? null;

  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em', marginBottom: 6 }}>
        REGIME
      </div>

      {/* Ribbon */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${REGIMES.length}, 1fr)`,
        gap: 3,
        marginBottom: 6,
      }}>
        {REGIMES.map(r => {
          const isActive = r.key === regime;
          return (
            <div
              key={r.key}
              title={transitions?.[r.key] != null
                ? `${r.label} · next-prob ${(transitions[r.key] * 100).toFixed(0)}%`
                : r.label}
              style={{
                padding: '6px 4px',
                textAlign: 'center',
                fontSize: 10,
                fontFamily: T.font,
                letterSpacing: '0.05em',
                background: isActive ? `${r.color}25` : `${r.color}08`,
                border: `1px solid ${isActive ? r.color : 'transparent'}`,
                color: isActive ? r.color : T.label2,
                fontWeight: isActive ? 600 : 400,
              }}
            >
              {r.label}
            </div>
          );
        })}
      </div>

      {/* Stats row */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1fr 1fr',
        gap: 6,
        fontSize: 10,
        fontFamily: T.font,
      }}>
        <Stat label="current" value={current?.label ?? (regime || '—')} color={current?.color} />
        <Stat label="confidence" value={pct(confidence)} />
        <Stat label="persistence" value={pct(persistence)} />
      </div>
    </div>
  );
}

function Stat({ label, value, color }) {
  return (
    <div>
      <div style={{ color: T.label, fontSize: 9, letterSpacing: '0.05em' }}>{label}</div>
      <div style={{ color: color || T.text, fontWeight: 500 }}>{value ?? '—'}</div>
    </div>
  );
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function pct(v) {
  if (v == null) return '—';
  return `${(v * 100).toFixed(1)}%`;
}
