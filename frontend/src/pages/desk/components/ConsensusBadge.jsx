// /desk Phase 3 — cross-source consensus badge.
//
// Reads the top-level `consensus` block from /v4/snapshot:
//   safe_to_trade        bool — engine's master gate, blocks all new trades
//                              when oracle sources disagree too much.
//   max_divergence_bps   max bps spread between Chainlink/Tiingo/Binance.
//   agreement_score      0..1, weighted source-agreement metric.
//   sources              { name: { price, age_ms, available } }
//
// One-line strip just below the divergence banner. When safe_to_trade is
// false the strip turns red and pins to the top of the panel column —
// this is the "do nothing" signal even if the model has high conviction.

import React from 'react';
import { T } from '../../../theme/tokens.js';

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export default function ConsensusBadge({ snapshot }) {
  const c = snapshot?.consensus;
  if (!c) {
    // Fail-closed: consensus block missing → show a muted ? chip so the
    // operator knows the gate isn't reporting, rather than silently hiding.
    return (
      <div style={{
        border: `1px solid ${T.border}`,
        background: T.card,
        padding: '6px 10px',
        marginBottom: 10,
        fontFamily: T.font,
        fontSize: 11,
        display: 'flex',
        alignItems: 'center',
        gap: 14,
      }}>
        <span style={{ color: T.label, fontSize: 9, letterSpacing: '0.12em' }}>CONSENSUS</span>
        <Chip label="?" color={T.label} title="Consensus block absent on snapshot — gate status unknown." />
      </div>
    );
  }

  const safe = c.safe_to_trade;
  const safeReason = c.safe_to_trade_reason || c.reason || null;
  const divergence = num(c.max_divergence_bps);
  const agreement = num(c.source_agreement_score ?? c.agreement_score);
  const sources = c.sources || {};
  const srcNames = Object.keys(sources);
  const sourceCount = srcNames.length;
  const availableCount = srcNames.filter(n => sources[n]?.available).length;

  const divergenceColor = divergence == null ? T.label
    : divergence > 30 ? T.loss
    : divergence > 10 ? T.warn
    : T.profit;

  const safeColor = safe === false ? T.loss : safe === true ? T.profit : T.label;

  return (
    <div style={{
      border: `1px solid ${safe === false ? T.loss : T.border}`,
      background: safe === false ? `${T.loss}14` : T.card,
      padding: '6px 10px',
      marginBottom: 10,
      fontFamily: T.font,
      fontSize: 11,
      display: 'flex',
      alignItems: 'center',
      gap: 14,
      flexWrap: 'wrap',
    }}>
      <span style={{ color: T.label, fontSize: 9, letterSpacing: '0.12em' }}>
        CONSENSUS
      </span>

      <Chip
        label={safe === false ? 'UNSAFE' : safe === true ? 'SAFE' : '?'}
        color={safeColor}
        title={safeReason || (safe === false ? 'Cross-source disagreement above threshold.' : undefined)}
      />

      {divergence != null ? (
        <Stat label="max div" value={`${divergence.toFixed(1)} bps`} color={divergenceColor} />
      ) : null}

      {agreement != null ? (
        <Stat label="agreement" value={`${(agreement * 100).toFixed(0)}%`}
              color={agreement >= 0.9 ? T.profit : agreement >= 0.7 ? T.warn : T.loss} />
      ) : null}

      {sourceCount > 0 ? (
        <Stat
          label="sources"
          value={`${availableCount}/${sourceCount}`}
          color={availableCount === sourceCount ? T.profit : T.warn}
          title={srcNames.map(n => {
            const s = sources[n];
            const p = num(s?.price);
            return `${n}: ${s?.available ? `$${p != null ? p.toLocaleString() : '—'} (${s.age_ms}ms)` : 'DOWN'}`;
          }).join('\n')}
        />
      ) : null}

      {safe === false && safeReason ? (
        <span style={{ color: T.loss, fontSize: 10 }}>
          {String(safeReason).slice(0, 80)}
        </span>
      ) : null}
    </div>
  );
}

function Chip({ label, color, title }) {
  return (
    <span title={title} style={{
      padding: '2px 8px',
      border: `1px solid ${color}`,
      color,
      borderRadius: 2,
      fontSize: 10,
      letterSpacing: '0.1em',
      fontWeight: 600,
    }}>{label}</span>
  );
}

function Stat({ label, value, color, title }) {
  return (
    <span title={title} style={{ display: 'inline-flex', gap: 4, alignItems: 'baseline' }}>
      <span style={{ color: T.label, fontSize: 9, letterSpacing: '0.05em' }}>{label}</span>
      <span style={{ color: color || T.text }}>{value}</span>
    </span>
  );
}
