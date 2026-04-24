// /desk — signal stack.
//
// Renders the 5m timescale block from /api/v4/snapshot with the new
// classifier fields (pc/pl/pu) surfaced explicitly. See hub note #226
// for semantics:
//   pu = probability_up (ensemble — what the engine trades on)
//   pc = probability_classifier (new head; may be null on warmup)
//   pl = probability_lgb (LGB head)
//
// VHC banner fires at |pc - 0.5| >= 0.25 (per note #226 — 89–94% dir_acc).
// SOURCE CONFLICT badge fires at |pc - pl| > 0.25.

import React from 'react';
import { T } from '../../../theme/tokens.js';
import {
  convictionTier,
  isClassifierHighConviction,
  isSourceConflict,
  probDisagreement,
  directionFromProbUp,
} from '../lib/conviction.js';

const TIER_COLOURS = {
  NONE:       T.label,
  LOW:        T.label2,
  MEDIUM:     '#60a5fa',
  HIGH:       T.warn,
  VERY_HIGH:  T.profit,
};

export default function SignalStack({ fiveMin, pu, pc, pl, windowDelta }) {
  const tier = convictionTier(pu);
  const dir = directionFromProbUp(pu);
  const conflict = isSourceConflict(pc, pl);
  const vhc = isClassifierHighConviction(pc);
  const disagree = probDisagreement(pc, pl);

  const vpin = num(fiveMin?.vpin ?? fiveMin?.vpin_current);
  const regime = fiveMin?.regime || fiveMin?.regime_label || null;
  const vol = num(fiveMin?.volatility ?? fiveMin?.vol);
  const funding = num(fiveMin?.funding_rate ?? fiveMin?.funding);
  const takerBS = num(fiveMin?.taker_buy_sell ?? fiveMin?.taker_buy_sell_ratio);

  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em', marginBottom: 8 }}>
        SIGNAL STACK · 5m
      </div>

      {vhc ? (
        <Banner colour={T.profit}>⚡ Classifier high conviction — |pc − 0.5| ≥ 0.25</Banner>
      ) : null}
      {conflict ? (
        <Banner colour={T.warn}>⚠ SOURCE CONFLICT — |pc − pl| = {disagree?.toFixed(3) || '—'}</Banner>
      ) : null}

      <Grid>
        <Row label="Consensus (pu)"
             value={fmtProb(pu)}
             right={dir ? <Pill colour={dir === 'UP' ? T.profit : T.loss}>{dir}</Pill> : null} />
        <Row label="Classifier (pc)"
             value={pc == null
               ? <span title="classifier warming up" style={{ color: T.label }}>—</span>
               : fmtProb(pc)} />
        <Row label="LGB (pl)" value={fmtProb(pl)} />
        <Row label="|pc − pl|"
             value={disagree == null ? '—' : disagree.toFixed(3)}
             right={conflict ? <Pill colour={T.warn}>CONFLICT</Pill> : null} />
        <Divider />
        <Row label="Conviction"
             value={<span style={{ color: TIER_COLOURS[tier] }}>{tier}</span>}
             right={pu == null ? null
               : <span style={{ color: T.label2, fontSize: 10 }}>
                   edge {Math.abs(pu - 0.5).toFixed(3)}
                 </span>} />
        <Row label="VPIN"
             value={vpin == null ? '—' : vpin.toFixed(3)}
             right={<VpinBadge v={vpin} />} />
        <Row label="Regime"
             value={regime || '—'}
             right={vol == null ? null
               : <span style={{ color: T.label2, fontSize: 10 }}>{vol.toFixed(3)}</span>} />
        <Row label="Window Δ%"
             value={windowDelta == null ? '—' : fmtPct(windowDelta)} />
        <Row label="Taker buy/sell"
             value={takerBS == null ? '—' : takerBS.toFixed(3)} />
        <Row label="Funding"
             value={funding == null ? '—' : `${(funding * 100).toFixed(4)}%`} />
      </Grid>
    </div>
  );
}

function Grid({ children }) {
  return <div style={{ display: 'grid', gridTemplateColumns: '1fr auto auto', rowGap: 4, columnGap: 8 }}>{children}</div>;
}

function Row({ label, value, right }) {
  return (
    <>
      <div style={{ fontSize: 11, color: T.label2 }}>{label}</div>
      <div style={{ fontSize: 12, textAlign: 'right', color: T.text, minWidth: 60 }}>{value}</div>
      <div style={{ minWidth: 60, textAlign: 'right' }}>{right ?? null}</div>
    </>
  );
}

function Divider() {
  return (
    <div style={{ gridColumn: '1 / span 3', borderTop: `1px solid ${T.border}`, margin: '4px 0' }} />
  );
}

function Banner({ colour, children }) {
  return (
    <div style={{
      border: `1px solid ${colour}`,
      color: colour,
      padding: '4px 8px',
      fontSize: 11,
      marginBottom: 8,
      borderRadius: 2,
    }}>{children}</div>
  );
}

function Pill({ colour, children }) {
  return (
    <span style={{
      border: `1px solid ${colour}`,
      color: colour,
      fontSize: 10,
      padding: '1px 5px',
      borderRadius: 2,
      letterSpacing: '0.1em',
    }}>{children}</span>
  );
}

function VpinBadge({ v }) {
  if (v == null) return null;
  if (v >= 0.70) return <Pill colour={T.loss}>CASCADE</Pill>;
  if (v >= 0.55) return <Pill colour={T.warn}>INFORMED</Pill>;
  return <Pill colour={T.label}>CALM</Pill>;
}

function fmtProb(p) {
  if (p == null) return '—';
  return p.toFixed(3);
}
function fmtPct(d) {
  if (d == null) return '—';
  return `${d >= 0 ? '+' : ''}${(d * 100).toFixed(2)}%`;
}
function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
