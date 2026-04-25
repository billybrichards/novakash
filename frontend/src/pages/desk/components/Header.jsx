// /desk header — sticky band with BTC price, window countdown, YES/NO mid,
// engine status chip, regime ribbon.
//
// Regime colours per spec #218 §2 (engine labels from
// engine/signals/regime_classifier.py):
//   LOW_VOL   → blue   (#60a5fa)
//   NORMAL    → gray   (#94a3b8)
//   HIGH_VOL  → amber  (T.warn)
//   TRENDING  → green  (T.profit)

import React from 'react';
import { T } from '../../../theme/tokens.js';
import { fmtTRemaining } from '../hooks/useWindow.js';

const REGIME_COLOURS = {
  LOW_VOL:   '#60a5fa',
  NORMAL:    '#94a3b8',
  HIGH_VOL:  '#f59e0b',
  TRENDING:  '#4ade80',
  CASCADE:   '#f87171',
};

export default function Header({
  price,
  priceDelta2s,
  secondsRemaining,
  targetPriceChainlink,
  yesMid,
  noMid,
  systemStatus, // 'LIVE' | 'PAPER' | 'KILLED' | null
  regime,
  vol,
}) {
  const engineChip = systemStatus
    ? <StatusChip mode={systemStatus} />
    : <StatusChip mode="?" />;

  const regimeKey = (regime || '').toUpperCase();
  const regimeColour = REGIME_COLOURS[regimeKey] || T.label;

  return (
    <div style={{
      position: 'sticky', top: 0, zIndex: 5,
      background: T.bg,
      borderBottom: `1px solid ${T.border}`,
      padding: '10px 0',
      display: 'flex', alignItems: 'center', gap: 20,
      minHeight: 60,
      fontFamily: T.font,
    }}>
      <Block label="BTC">
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
          <span style={{ fontSize: 18, fontWeight: 500 }}>
            {price == null ? '—' : fmtUsd(price)}
          </span>
          <Delta2s delta={priceDelta2s} />
        </div>
      </Block>

      <Block label="WINDOW">
        <span style={{ fontSize: 18, fontWeight: 500, letterSpacing: '0.05em' }}>
          {fmtTRemaining(secondsRemaining)}
        </span>
      </Block>

      <Block label="TARGET @ OPEN">
        <span style={{ fontSize: 13 }}>
          {targetPriceChainlink == null ? '—' : fmtUsd(targetPriceChainlink)}
        </span>
      </Block>

      {(yesMid != null || noMid != null) ? (
        <Block label="YES / NO">
          <span style={{ fontSize: 13 }}>
            {fmtProb(yesMid)} / {fmtProb(noMid)}
          </span>
        </Block>
      ) : null}

      <div style={{ flex: 1 }} />

      <div style={{
        display: 'inline-flex', alignItems: 'center', gap: 6,
        padding: '4px 8px',
        border: `1px solid ${regimeColour}`,
        borderRadius: 2,
        color: regimeColour,
        fontSize: 10,
        letterSpacing: '0.1em',
      }}>
        <span>{regimeKey || '—'}</span>
        {vol != null ? (
          <span style={{ color: T.label2, fontWeight: 400 }}>{vol.toFixed(3)}</span>
        ) : null}
      </div>

      {engineChip}
    </div>
  );
}

function Block({ label, children }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.12em' }}>{label}</div>
      <div>{children}</div>
    </div>
  );
}

function Delta2s({ delta }) {
  if (delta == null || Number.isNaN(delta)) return null;
  const up = delta >= 0;
  return (
    <span style={{
      fontSize: 11,
      color: up ? T.profit : T.loss,
    }}>{up ? '+' : ''}{delta.toFixed(2)}</span>
  );
}

function StatusChip({ mode }) {
  const m = {
    LIVE:   { c: T.profit, label: 'LIVE'   },
    PAPER:  { c: T.warn,   label: 'PAPER'  },
    KILLED: { c: T.loss,   label: 'KILLED' },
    '?':    { c: T.label,  label: '?'      },
  }[mode] || { c: T.label, label: String(mode) };
  return (
    <span style={{
      display: 'inline-block',
      fontSize: 10,
      letterSpacing: '0.15em',
      padding: '3px 8px',
      border: `1px solid ${m.c}`,
      color: m.c,
      borderRadius: 2,
    }}>{m.label}</span>
  );
}

function fmtUsd(n) {
  if (n == null) return '—';
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function fmtProb(p) {
  if (p == null) return '—';
  return p.toFixed(3);
}
