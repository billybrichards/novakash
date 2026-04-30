// /desk Phase 3 — v3 composite sub-signals panel.
//
// Reads `sub_signals` from the 5m timescale block (engine maps it to
// composite_v3 components: elm, cascade, taker, oi, funding, vpin,
// momentum). Sign semantics vary per signal — the producing model
// (external V4 service) defines them. VPIN in particular is a
// magnitude in [0, 1] and never negative. We render all values on a
// bipolar bar for visual comparison, but the sign should not be read
// as universally meaning bullish/bearish without checking the upstream
// model spec for each signal.
//
// Audit #291 finding: vpin + taker were already wired into SignalStack
// but elm/cascade/oi/funding/momentum were not surfaced anywhere on
// /desk despite being in window_snapshots.

import React from 'react';
import { T } from '../../../theme/tokens.js';

// Display order: directional alpha first (elm/momentum/taker), then
// flow/positioning (oi/funding), then microstructure (vpin/cascade).
const SIGNAL_ORDER = [
  { key: 'elm',      label: 'ELM',      hint: 'Extreme Learning Machine — short-horizon directional model.' },
  { key: 'momentum', label: 'MOM',      hint: 'Price-momentum component of the v3 composite.' },
  { key: 'taker',    label: 'TAKER',    hint: 'Taker buy/sell imbalance — aggressive flow direction.' },
  { key: 'oi',       label: 'OI',       hint: 'Open-interest delta — positioning build/unwind.' },
  { key: 'funding',  label: 'FUND',     hint: 'Funding-rate skew — paid-side stress proxy.' },
  { key: 'vpin',     label: 'VPIN',     hint: 'Volume-synchronised informed trading probability.' },
  { key: 'cascade',  label: 'CASCADE',  hint: 'Liquidation-cascade pressure score.' },
];

// Most v3 sub-signals fall in [-1, +1]. We clamp + scale at that range
// for display; scores beyond it are pinned at 100% and flagged.
const SCALE = 1.0;

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function colorFor(v) {
  if (v == null) return T.label;
  if (v > 0.05) return T.profit;
  if (v < -0.05) return T.loss;
  return T.label2;
}

export default function SubSignalsPanel({ fiveMin }) {
  const subs = fiveMin?.sub_signals ?? fiveMin?.v4_sub_signals ?? null;
  const present = !!subs;

  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{
        fontSize: 10,
        color: T.label,
        letterSpacing: '0.1em',
        marginBottom: 8,
      }}>
        SUB-SIGNALS · v3 composite
      </div>

      {!present ? (
        <div style={{ color: T.label, fontSize: 11, fontFamily: T.font }}>
          sub_signals absent on snapshot
        </div>
      ) : (
        <div style={{ fontFamily: T.font }}>
          {SIGNAL_ORDER.map(({ key, label, hint }) => {
            const v = num(subs[key]);
            const pinned = v != null && Math.abs(v) > SCALE;
            const mag = v == null ? 0 : Math.min(1, Math.abs(v) / SCALE);
            const col = colorFor(v);
            const positive = v != null && v >= 0;
            return (
              <div key={key} title={hint} style={{
                display: 'grid',
                gridTemplateColumns: '54px 1fr 54px',
                alignItems: 'center',
                gap: 6,
                padding: '3px 0',
                fontSize: 11,
              }}>
                <span style={{ color: T.label2, fontSize: 10, letterSpacing: '0.05em' }}>{label}</span>
                <Bar mag={mag} positive={positive} color={col} />
                <span style={{
                  color: col,
                  textAlign: 'right',
                  fontSize: 10,
                }}>
                  {v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2)}
                  {pinned ? '*' : ''}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// A bipolar bar — center line is 0, fills right for positive / left for negative.
function Bar({ mag, positive, color }) {
  return (
    <div style={{
      position: 'relative',
      height: 6,
      background: T.bg,
      border: `1px solid ${T.border}`,
      borderRadius: 1,
    }}>
      {/* center marker */}
      <div style={{
        position: 'absolute', left: '50%', top: -1, bottom: -1,
        width: 1, background: T.label, opacity: 0.4,
      }} />
      {/* fill */}
      <div style={{
        position: 'absolute',
        top: 0, bottom: 0,
        left: positive ? '50%' : `${50 - mag * 50}%`,
        width: `${mag * 50}%`,
        background: color,
        opacity: 0.7,
      }} />
    </div>
  );
}
