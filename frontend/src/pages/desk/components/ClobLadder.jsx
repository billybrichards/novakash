// /desk Phase 2 — CLOB 5-level ladder for YES + NO + implied-vs-model divergence line.
//
// Layout: two stacked half-panels (YES on top, NO below), each a 5-level
// ladder with asks (red) above the spread and bids (green) below. Inside
// quote (best bid / best ask) is highlighted with a brighter border.
//
// Below the ladder: one-line divergence strip showing implied P(UP) from
// YES mid, model P(UP) from snapshot, and the +/-pt gap with a direction
// chevron. Colour coding:
//   |gap| < 0.05   → T.label  (noise)
//   0.05–0.10      → T.warn
//   > 0.10         → T.profit/loss depending on model direction
//
// If the CLOB proxy is unavailable (HUB_ALLOW_CLOB_FETCH not set on this
// deployment) — render a small offline chip, NOT a red error box. The
// feature is expected-off on the default AWS hub.

import React from 'react';
import { T } from '../../../theme/tokens.js';
import { computeImpliedDivergence } from '../lib/divergence.js';

// Parent-supplied book (to avoid duplicate fetches when DivergenceBanner
// also reads implied_p_up). Pass the output of `useClobBook` directly.
export default function ClobLadder({ book, unavailable, error, modelPUp }) {

  if (unavailable) {
    return (
      <Panel title="CLOB 5-Level Book" subtitle="YES · NO · inside quote">
        <div style={{ color: T.label, fontSize: 10 }}>
          CLOB feed offline on this hub (set HUB_ALLOW_CLOB_FETCH to enable).
        </div>
      </Panel>
    );
  }

  if (error && !book) {
    return (
      <Panel title="CLOB 5-Level Book">
        <div style={{ color: T.loss, fontSize: 10 }}>{error}</div>
      </Panel>
    );
  }

  if (!book) {
    return (
      <Panel title="CLOB 5-Level Book">
        <div style={{ color: T.label, fontSize: 10 }}>Loading book…</div>
      </Panel>
    );
  }

  const divergence = computeImpliedDivergence(book.implied_p_up, modelPUp);
  const divColor = divergenceColor(divergence);

  return (
    <Panel title="CLOB 5-Level Book" subtitle="YES · NO · inside quote · implied P(UP) from mid">
      <Side label="YES" side={book.yes} />
      <div style={{ height: 6 }} />
      <Side label="NO" side={book.no} />

      <div style={{
        marginTop: 8,
        paddingTop: 8,
        borderTop: `1px dashed ${T.border}`,
        display: 'grid',
        gridTemplateColumns: '1fr 1fr 1fr',
        gap: 6,
        fontSize: 10,
        fontFamily: T.font,
      }}>
        <Stat label="implied P(UP)" value={fmtProb(book.implied_p_up)} />
        <Stat label="model P(UP)" value={fmtProb(modelPUp)} />
        <div>
          <div style={{ color: T.label, fontSize: 9, letterSpacing: '0.05em' }}>divergence</div>
          <div style={{ color: divColor, fontWeight: 500 }}>
            {divergence == null
              ? '—'
              : `${divergence > 0 ? '+' : ''}${(divergence * 100).toFixed(1)}pt ${divArrow(divergence)}`}
          </div>
        </div>
      </div>

      {/* Secondary metrics row */}
      <div style={{
        marginTop: 4,
        display: 'grid',
        gridTemplateColumns: '1fr 1fr',
        gap: 6,
        fontSize: 10,
        fontFamily: T.font,
      }}>
        <Stat label="spread (YES)" value={book.spread != null ? fmtProb(book.spread) : '—'} />
        <Stat label="imbalance" value={book.imbalance != null ? fmtBipolar(book.imbalance) : '—'} />
      </div>
    </Panel>
  );
}

// ── Presentational bits ─────────────────────────────────────────────────────

function Side({ label, side }) {
  const bids = side?.bids ?? [];
  const asks = side?.asks ?? [];
  // asks are rendered in reverse so "best" (lowest ask) sits closest to the spread.
  const asksDescending = asks.slice().reverse();
  const bestBidPx = bids[0]?.price;
  const bestAskPx = asks[0]?.price;

  const maxSize = Math.max(
    1e-6,
    ...bids.map(b => b.size || 0),
    ...asks.map(a => a.size || 0),
  );

  return (
    <div style={{ border: `1px solid ${T.border}`, padding: 6 }}>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        fontSize: 9,
        color: T.label,
        letterSpacing: '0.1em',
        marginBottom: 4,
      }}>
        <span>{label}</span>
        <span>price · size</span>
      </div>
      {/* Asks (top, red) */}
      {asksDescending.map((a, i) => (
        <Row
          key={`ask-${i}`}
          price={a.price}
          size={a.size}
          color={T.loss}
          highlight={a.price === bestAskPx}
          widthFrac={(a.size || 0) / maxSize}
        />
      ))}
      {/* Spread divider */}
      <div style={{
        height: 1,
        background: T.borderStrong,
        margin: '3px 0',
      }} />
      {/* Bids (bottom, green) */}
      {bids.map((b, i) => (
        <Row
          key={`bid-${i}`}
          price={b.price}
          size={b.size}
          color={T.profit}
          highlight={b.price === bestBidPx}
          widthFrac={(b.size || 0) / maxSize}
        />
      ))}
      {bids.length === 0 && asks.length === 0 ? (
        <div style={{ color: T.label, fontSize: 10 }}>Empty book</div>
      ) : null}
    </div>
  );
}

function Row({ price, size, color, highlight, widthFrac }) {
  return (
    <div style={{
      position: 'relative',
      display: 'flex',
      justifyContent: 'space-between',
      padding: '1px 4px',
      fontSize: 10,
      fontFamily: T.font,
      border: highlight ? `1px solid ${color}` : '1px solid transparent',
      background: highlight ? `${color}15` : 'transparent',
    }}>
      {/* Depth bar */}
      <div style={{
        position: 'absolute',
        left: 0, top: 0, bottom: 0,
        width: `${Math.min(100, widthFrac * 100)}%`,
        background: `${color}10`,
      }} />
      <span style={{ color, position: 'relative' }}>{fmtProb(price)}</span>
      <span style={{ color: T.label2, position: 'relative' }}>{fmtSize(size)}</span>
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div>
      <div style={{ color: T.label, fontSize: 9, letterSpacing: '0.05em' }}>{label}</div>
      <div style={{ color: T.text, fontWeight: 500 }}>{value ?? '—'}</div>
    </div>
  );
}

function Panel({ title, subtitle, children }) {
  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em', marginBottom: 6 }}>
        {title}
      </div>
      {subtitle ? <div style={{ fontSize: 9, color: T.label2, marginBottom: 6 }}>{subtitle}</div> : null}
      {children}
    </div>
  );
}

// ── Formatters ──────────────────────────────────────────────────────────────

function fmtProb(p) {
  if (p == null || !Number.isFinite(p)) return '—';
  return p.toFixed(3);
}

function fmtSize(s) {
  if (s == null || !Number.isFinite(s)) return '—';
  if (s >= 1_000_000) return `${(s / 1_000_000).toFixed(2)}M`;
  if (s >= 1_000) return `${(s / 1_000).toFixed(1)}k`;
  return s.toFixed(0);
}

function fmtBipolar(v) {
  const pct = (v * 100).toFixed(0);
  return `${v >= 0 ? '+' : ''}${pct}%`;
}

function divergenceColor(v) {
  if (v == null) return T.label;
  const abs = Math.abs(v);
  if (abs < 0.05) return T.label;
  if (abs < 0.10) return T.warn;
  return v > 0 ? T.profit : T.loss;
}

function divArrow(v) {
  if (v == null) return '';
  if (v > 0) return '↑';
  if (v < 0) return '↓';
  return '→';
}
