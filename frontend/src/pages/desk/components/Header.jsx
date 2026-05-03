// /desk header — sticky band with BTC price, window countdown, YES/NO mid,
// engine status chip, regime ribbon.
//
// Regime colours per spec #218 §2 (engine labels from
// engine/signals/regime_classifier.py):
//   LOW_VOL   → blue   (#60a5fa)
//   NORMAL    → gray   (#94a3b8)
//   HIGH_VOL  → amber  (T.warn)
//   TRENDING  → green  (T.profit)
//
// Phase 3 (#365): when the countdown is inside the v9 exit-window
// (T-48s → T-30s) we colour the WINDOW block to make the engine's
// active sell-decision range visible to the operator.

import React from 'react';
import { T } from '../../../theme/tokens.js';
import { fmtTRemaining } from '../hooks/useWindow.js';
import { inExitWindow } from '../lib/exitWindow.js';

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
  targetPrice,                 // canonical priceToBeat (post-PR #464)
  targetPriceSource,           // 'polymarket_canonical' | 'chainlink_polygon_fallback' | 'unknown'
  yesMid,
  noMid,
  systemStatus, // 'LIVE' | 'PAPER' | 'KILLED' | null
  regime,
  vol,
}) {
  // Prefer canonical Polymarket priceToBeat. Falls back to legacy chainlink
  // value when the snapshot is cold or the prop is unset by older callers.
  const displayTarget = targetPrice ?? targetPriceChainlink;
  // Canonical sources — engine's priceToBeat taxonomy (PR #desk-pricetobeat-verify):
  //   'polymarket_html_priceToBeat' — HTML-scraped __NEXT_DATA__ (most precise)
  //   'polymarket_priceToBeat'      — Gamma eventMetadata canonical
  //   'polymarket_canonical'        — hub inference fallback for pre-migration rows
  const CANONICAL_SOURCES = new Set([
    'polymarket_html_priceToBeat',
    'polymarket_priceToBeat',
    'polymarket_canonical',
  ]);
  const isCanonical = CANONICAL_SOURCES.has(targetPriceSource);
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

      <Block label={inExitWindow(secondsRemaining) ? 'WINDOW · EXIT' : 'WINDOW'}>
        <span style={{
          fontSize: 18,
          fontWeight: 500,
          letterSpacing: '0.05em',
          color: inExitWindow(secondsRemaining) ? T.warn : undefined,
        }}>
          {fmtTRemaining(secondsRemaining)}
          {inExitWindow(secondsRemaining) ? (
            <span title="Engine sell window — T-48 to T-30 (#365). If a position is open it may be filled in this range."
                  style={{ marginLeft: 6, fontSize: 10, color: T.warn, letterSpacing: '0.1em' }}>
              ◆ SELL
            </span>
          ) : null}
        </span>
      </Block>

      <Block label="TARGET @ OPEN">
        <span style={{ display: 'inline-flex', alignItems: 'baseline', gap: 6, fontSize: 13 }}>
          <span title={
            isCanonical
              ? `Polymarket priceToBeat — canonical resolution price. Source: ${targetPriceSource || 'polymarket_canonical'}.`
              : targetPriceSource === 'chainlink_polygon_fallback'
                ? 'Chainlink Polygon Aggregator (legacy). Diverges from Polymarket priceToBeat by $3-32 per window — engine still warming up?'
                : targetPriceSource === 'chainlink_polygon_pending'
                  ? 'Chainlink Polygon — oracle sample pending (engine warming up, canonical will sync within 60-90s).'
                  : targetPriceSource === 'binance_fallback'
                    ? 'Binance spot price (last resort fallback). May diverge from Polymarket resolution price.'
                    : 'Window open price — source unknown.'
          }>
            {displayTarget == null ? '—' : fmtUsd(displayTarget)}
          </span>
          <span
            title={
              isCanonical
                ? `Source: ${targetPriceSource || 'polymarket_canonical'} — matches resolution price to the cent.`
                : targetPriceSource === 'chainlink_polygon_fallback'
                  ? 'Source: Chainlink fallback. Engine has not written canonical yet for this window.'
                  : targetPriceSource === 'chainlink_polygon_pending'
                    ? 'Source: Chainlink (oracle sample pending). Will upgrade to CANONICAL within 60-90s.'
                    : 'Source unknown.'
            }
            style={{
              fontSize: 9,
              padding: '1px 5px',
              border: `1px solid ${isCanonical ? T.profit : T.warn}55`,
              color: isCanonical ? T.profit : T.warn,
              borderRadius: 2,
              letterSpacing: '0.08em',
            }}>
            {isCanonical ? 'CANONICAL' : 'APPROX'}
          </span>
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
